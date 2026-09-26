"""
Local web app for manually logging the 12-manager draft-mode league's draft
results and ongoing transactions (trades, free-agent adds/drops), and for
viewing real-ownership-aware transfer suggestions.

Single-user, local-only tool - the user is the sole operator/commissioner
who manually enters what happens in the league (see docs/game_rules.md,
"This league's specifics"). Never calls engine.data/fetch_season directly:
only sync_db.py (run separately, manually) talks to the live API, so a
route never blocks on the 6.5s/game rate limit - routes only ever read the
already-synced euroleague.db.

Usage:
    python app.py
"""

from __future__ import annotations

import subprocess
import sys
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from draft_board import build_draft_board, replacement_values, tier_breaks
from engine import ownership
from engine.draft_import import DraftCsvError, distinct_managers, parse_draft_csv, resolve_manager_picks
from engine.db import (
    DB_PATH,
    add_to_watchlist,
    all_known_players,
    fantasy_pool_synced_at,
    get_connection,
    injuries_synced_at,
    known_player_ids,
    latest_game_date,
    load_fantasy_pool,
    load_injuries,
    load_manual_projections,
    load_roster,
    load_rows,
    load_schedule,
    load_watchlist,
    next_unplayed_round,
    remove_from_watchlist,
    roster_synced_at,
    row_count,
)
from engine.fantasy_pool import resolve_pool_rows
from engine.injuries import EXCLUDING_STATUSES, resolve_injury_rows
from engine.lineup import (
    VALID_FORMATIONS,
    Lineup,
    availability_label,
    build_lineup,
    choose_active_squad,
    compute_round_score,
    swap_after_day1,
    team_dates_from_schedule,
)
from engine.projections import Projection, actual_fantasy_score, blend_season_rows, build_projections
from engine.roster import REQUIRED_COUNTS, ActiveSquad, Roster
from engine.dev_draft import randomize_draft
from engine.rosters import merge_roster
from engine.transfers import projected_gain, suggest_transfers

# Edit these once per year as seasons roll over.
CURRENT_SEASON = "E2026"
PRIOR_SEASON = "E2025"
KNOWN_SEASONS = ["E2023", "E2024", "E2025", "E2026"]

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"

app = Flask(__name__)
app.secret_key = "euroleague-fantasy-local-dev"  # local single-user tool, not internet-facing


@app.template_filter("player_initials")
def player_initials(name: str) -> str:
    """'SURNAME, FIRSTNAME' -> 'FS' monogram - the EuroLeague API's /people
    endpoint returns an empty `images` object for every player (confirmed
    live, E2026), so there are no real player photos to show; this is the
    stand-in for the lineup pitch view's court avatars."""
    if not name:
        return "?"
    surname, _, first = name.partition(",")
    surname, first = surname.strip(), first.strip()
    initials = (first[:1] + surname[:1]).upper()
    return initials or name[:2].upper()


@app.template_filter("player_surname")
def player_surname(name: str) -> str:
    """'SURNAME, FIRSTNAME' -> 'Surname' title case, for the pitch view's compact court labels."""
    surname, _, _ = (name or "").partition(",")
    return surname.strip().title() or name


@app.template_filter("player_display_name")
def player_display_name(name: str) -> str:
    """'SURNAME, FIRSTNAME' -> 'Firstname Surname' title case, for the pitch view's bench list."""
    surname, sep, first = (name or "").partition(",")
    if not sep:
        return name.title() if name else ""
    return f"{first.strip().title()} {surname.strip().title()}"


_projection_cache: dict[tuple, tuple[int, dict[str, Projection]]] = {}

# --- Background data sync (sync_db.py / sync_rosters.py triggered from the UI) ---
#
# Both scripts are the only code that talks to the live EuroLeague API (see
# module docstring above) and can take a while (cold box-score fetches are
# paced at ~6.5s/game). Routes must never block on that, so a sync runs as a
# real subprocess (reusing the scripts unchanged, not duplicating their
# logic) in a background thread; this in-memory dict is this server
# process's view of sync state - it resets on restart, which is fine for a
# local single-user tool (the DB itself, and `rosters.synced_at`, are the
# durable record of what actually happened).

_sync_lock = threading.Lock()
_sync_state: dict[str, dict] = {
    "db": {"running": False, "log_path": None, "started_at": None, "finished_at": None, "returncode": None, "seasons": []},
    "rosters": {"running": False, "log_path": None, "started_at": None, "finished_at": None, "returncode": None, "seasons": []},
    "fantasy_pool": {"running": False, "log_path": None, "started_at": None, "finished_at": None, "returncode": None, "seasons": []},
    "injuries": {"running": False, "log_path": None, "started_at": None, "finished_at": None, "returncode": None, "seasons": []},
}


def _run_sync(kind: str, cmds: list[list[str]], log_path: Path) -> None:
    """Runs each command in `cmds` in order, all output appended to one log
    file - a multi-step sync (see sync_rosters_trigger: EuroLeague /people
    then the Fantasy sheet, so "Sync Rosters" is one click for both sources)
    stops at the first failing step, same as a shell `&&` chain, and that
    step's exit code is what's recorded."""
    returncode = 0
    with open(log_path, "w") as f:
        for cmd in cmds:
            f.write(f"$ {' '.join(cmd)}\n")
            f.flush()
            proc = subprocess.run(cmd, cwd=BASE_DIR, stdout=f, stderr=subprocess.STDOUT, text=True)
            if proc.returncode != 0:
                returncode = proc.returncode
                break
    with _sync_lock:
        _sync_state[kind]["running"] = False
        _sync_state[kind]["finished_at"] = datetime.now(timezone.utc).isoformat()
        _sync_state[kind]["returncode"] = returncode
    _projection_cache.clear()  # the DB just changed under us - don't serve a stale pool


def _start_sync(kind: str, cmds: list[list[str]], seasons: list[str]) -> bool:
    """Returns False (does nothing) if a sync of this kind is already running."""
    with _sync_lock:
        if _sync_state[kind]["running"]:
            return False
        LOGS_DIR.mkdir(exist_ok=True)
        log_path = LOGS_DIR / f"sync_{kind}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.log"
        _sync_state[kind].update(
            running=True,
            log_path=str(log_path),
            started_at=datetime.now(timezone.utc).isoformat(),
            returncode=None,
            seasons=seasons,
        )
    thread = threading.Thread(target=_run_sync, args=(kind, cmds, log_path), daemon=True)
    thread.start()
    return True


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = get_connection()
    return g.db


@app.teardown_appcontext
def close_db(exception=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _load_pool_rows(conn: sqlite3.Connection) -> tuple[list[dict], int]:
    """(rows, as_of_round) to build the player pool from right now: the
    prior season blended with whatever of the current season has been
    played so far (see engine.projections.blend_season_rows for why - a
    hard switch to the current season zeroed nearly every projection the
    day after round 1 was synced)."""
    prior_rows = load_rows(conn, season_code=PRIOR_SEASON)
    current_rows = load_rows(conn, season_code=CURRENT_SEASON)
    return blend_season_rows(prior_rows, current_rows)


def get_pool(
    conn: sqlite3.Connection,
) -> tuple[dict[str, Projection], set[str], set[str], set[str], dict[str, dict]]:
    """(projections, new_player_ids, gone_player_ids, estimated_player_ids,
    injury_status) - the projection pool merged with the current roster
    composition, so a transferred player shows their real current team, a
    player with no box-score history yet (new to the league) still appears
    flagged rather than being silently absent, and a player no longer part
    of the current roster composition is flagged rather than looking like a
    normal draftable/tradeable player.

    Roster composition source: the real EuroLeague Fantasy draft pool
    (engine.fantasy_pool, a user-maintained Google Sheet synced via
    sync_fantasy_pool.py into `fantasy_pool`) when it's been synced -
    found (2026-09-15) to be a more reliable source than EuroLeague's own
    /people endpoint, which was significantly incomplete pre-season for
    some clubs (ASVEL, Barcelona). Falls back to /people
    (engine.db `rosters`, via sync_rosters.py) if the Fantasy pool hasn't
    been synced yet - never blocks on missing data.

    injury_status: player_id -> {status, team, round_text, comment,
    severity} from the basketnews injury report (engine.injuries), synced
    via sync_injuries.py. Best-effort/informational (see that module's
    docstring) - callers that care about availability (draft board, manager
    roster, lineup builder) use it for a badge; the lineup builder also
    zeroes an "Out" player's decision value (see the /lineup route)."""
    key = (PRIOR_SEASON, CURRENT_SEASON)
    mtime_ns = DB_PATH.stat().st_mtime_ns

    cached = _projection_cache.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]

    rows, as_of_round = _load_pool_rows(conn)
    projections = build_projections(rows, as_of_round=as_of_round)

    roster_rows = load_roster(conn, CURRENT_SEASON)
    seen = known_player_ids(conn)
    manual = load_manual_projections(conn)
    historical_players = all_known_players(conn)

    fantasy_pool_rows = load_fantasy_pool(conn)
    if fantasy_pool_rows:
        resolved_rows, _diag = resolve_pool_rows(fantasy_pool_rows, roster_rows, historical_players)
        projections, new_ids, gone_ids, estimated_ids = merge_roster(projections, resolved_rows, seen, manual)
    else:
        projections, new_ids, gone_ids, estimated_ids = merge_roster(projections, roster_rows, seen, manual)

    injury_rows = load_injuries(conn)
    resolved_injuries, _inj_diag = resolve_injury_rows(injury_rows, roster_rows, historical_players)
    injury_status = {r["player_id"]: r for r in resolved_injuries}

    result = (projections, new_ids, gone_ids, estimated_ids, injury_status)
    _projection_cache[key] = (mtime_ns, result)
    return result


@app.route("/")
def index():
    conn = get_db()
    owned_count = len(ownership.all_owned_ids(conn))
    manager_count = len(ownership.list_managers(conn))
    return render_template("index.html", owned_count=owned_count, manager_count=manager_count)


SORT_OPTIONS = [
    ("value", "Projected value"),
    ("vorp", "VORP"),
    ("n", "Games sampled"),
    ("player_name", "Player name"),
    ("team", "Team"),
]

_SORT_KEY_FNS = {
    "value": lambda p, replacement: p.projected_pir_with_bonus,
    "vorp": lambda p, replacement: p.projected_pir_with_bonus - replacement[p.position],
    "n": lambda p, replacement: p.games_sampled,
    "player_name": lambda p, replacement: p.player_name,
    "team": lambda p, replacement: p.team or "",
}


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/sync")
def sync_page():
    conn = get_db()
    season_info = [
        {"season": s, "rows": row_count(conn, s), "latest_game_date": (latest_game_date(conn, s) or "")[:10]}
        for s in KNOWN_SEASONS
    ]
    roster_info = {
        "count": len(load_roster(conn, CURRENT_SEASON)),
        "synced_at": roster_synced_at(conn, CURRENT_SEASON),
    }
    fantasy_pool_info = {
        "count": len(load_fantasy_pool(conn)),
        "synced_at": fantasy_pool_synced_at(conn),
    }
    injuries_info = {
        "count": len(load_injuries(conn)),
        "synced_at": injuries_synced_at(conn),
    }
    manager_count = len(ownership.list_managers(conn))
    owned_count = len(ownership.all_owned_ids(conn))

    with _sync_lock:
        state = {k: dict(v) for k, v in _sync_state.items()}
    logs = {}
    for kind, s in state.items():
        if s.get("log_path") and Path(s["log_path"]).exists():
            logs[kind] = Path(s["log_path"]).read_text()[-4000:]
    running_any = any(s["running"] for s in state.values())

    return render_template(
        "sync.html",
        season_info=season_info,
        roster_info=roster_info,
        fantasy_pool_info=fantasy_pool_info,
        injuries_info=injuries_info,
        state=state,
        logs=logs,
        running_any=running_any,
        current_season=CURRENT_SEASON,
        known_seasons=KNOWN_SEASONS,
        manager_count=manager_count,
        owned_count=owned_count,
    )


@app.route("/dev/randomize-draft", methods=["POST"])
def dev_randomize_draft():
    conn = get_db()
    managers = ownership.list_managers(conn)
    if not managers:
        flash("No managers seeded yet - run seed_league.py first.", "error")
        return redirect(url_for("sync_page"))

    pool, _new_ids, gone_ids, _estimated_ids, _injury_status = get_pool(conn)
    draftable_pool = {pid: p for pid, p in pool.items() if pid not in gone_ids}
    manager_ids = [m["manager_id"] for m in managers]

    try:
        counts = randomize_draft(conn, draftable_pool, manager_ids)
    except RuntimeError as e:
        flash(f"Randomized draft failed: {e}", "error")
        return redirect(url_for("sync_page"))

    flash(f"Randomized a fresh draft: {sum(counts.values())} picks across {len(counts)} managers.", "success")
    return redirect(url_for("sync_page"))


@app.route("/dev/clear-teams", methods=["POST"])
def dev_clear_teams():
    conn = get_db()
    freed = ownership.clear_all_ownership(conn)
    flash(f"Cleared all teams: {freed} player(s) sent back to free agency.", "success")
    return redirect(url_for("sync_page"))


@app.route("/dev/clear-managers", methods=["POST"])
def dev_clear_managers():
    conn = get_db()
    removed = ownership.clear_all_managers(conn)
    flash(
        f"Cleared all managers: {removed} manager(s) deleted, along with all ownership and "
        "transaction history. Re-seed with seed_league.py before drafting.",
        "success",
    )
    return redirect(url_for("sync_page"))


@app.route("/sync/db", methods=["POST"])
def sync_db_trigger():
    seasons = [s for s in KNOWN_SEASONS if request.form.get(f"season_{s}") == "1"]
    if not seasons:
        seasons = [CURRENT_SEASON]
    cmd = [sys.executable, "sync_db.py", "--seasons", *seasons]
    if _start_sync("db", [cmd], seasons):
        flash(f"Started syncing box scores for {', '.join(seasons)} in the background.", "success")
    else:
        flash("A box-score sync is already running - wait for it to finish.", "error")
    return redirect(url_for("sync_page"))


@app.route("/sync/rosters", methods=["POST"])
def sync_rosters_trigger():
    # Two steps, one click: EuroLeague's own /people endpoint first (still
    # needed as the identity/ID backbone resolve_pool_rows matches the
    # sheet against, and as full-coverage fallback for anyone the sheet
    # doesn't list), then the Fantasy sheet (the more current, more
    # complete source for actual team/position composition - see the
    # "Fantasy draft pool" section below). Confirmed with the user
    # 2026-09-22: syncing "rosters" should mean both, not just the
    # EuroLeague side, so composition data doesn't go stale just because a
    # second button wasn't also clicked.
    people_cmd = [sys.executable, "sync_rosters.py", "--season", CURRENT_SEASON]
    sheet_cmd = [sys.executable, "sync_fantasy_pool.py", "--season", CURRENT_SEASON]
    if _start_sync("rosters", [people_cmd, sheet_cmd], [CURRENT_SEASON]):
        flash(f"Started syncing {CURRENT_SEASON} rosters (EuroLeague + Fantasy sheet) in the background.", "success")
    else:
        flash("A roster sync is already running - wait for it to finish.", "error")
    return redirect(url_for("sync_page"))


@app.route("/sync/fantasy-pool", methods=["POST"])
def sync_fantasy_pool_trigger():
    cmd = [sys.executable, "sync_fantasy_pool.py", "--season", CURRENT_SEASON]
    if _start_sync("fantasy_pool", [cmd], [CURRENT_SEASON]):
        flash("Started syncing the Fantasy draft pool in the background.", "success")
    else:
        flash("A Fantasy pool sync is already running - wait for it to finish.", "error")
    return redirect(url_for("sync_page"))


@app.route("/sync/injuries", methods=["POST"])
def sync_injuries_trigger():
    cmd = [sys.executable, "sync_injuries.py", "--season", CURRENT_SEASON]
    if _start_sync("injuries", [cmd], [CURRENT_SEASON]):
        flash("Started syncing the injury report in the background.", "success")
    else:
        flash("An injury report sync is already running - wait for it to finish.", "error")
    return redirect(url_for("sync_page"))


@app.route("/draft")
def draft():
    conn = get_db()
    pool, new_ids, gone_ids, estimated_ids, injury_status = get_pool(conn)
    draftable_pool = {pid: p for pid, p in pool.items() if pid not in gone_ids}
    board = build_draft_board(draftable_pool)
    replacement = replacement_values(board)
    managers = ownership.list_managers(conn)
    owned_ids = ownership.all_owned_ids(conn)
    watchlist_ids = set(load_watchlist(conn).keys())

    hide_drafted = request.args.get("hide_drafted") == "1"
    position_filter = request.args.get("position")
    team_filter = request.args.get("team") or None
    sort_key = request.args.get("sort", "value")
    if sort_key not in _SORT_KEY_FNS:
        sort_key = "value"
    sort_dir = request.args.get("dir", "desc")
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    teams = sorted({p.team for p in draftable_pool.values() if p.team})
    key_fn = _SORT_KEY_FNS[sort_key]
    is_default_sort = sort_key == "value" and sort_dir == "desc"

    positions_out = {}
    for position in ("Guard", "Forward", "Center"):
        if position_filter and position_filter != position:
            continue
        ranked = board.get(position, [])  # already value-sorted desc
        if team_filter:
            ranked = [p for p in ranked if p.team == team_filter]

        breaks = tier_breaks(ranked)  # meaningful only in this natural value order
        tier_break_ids = {ranked[i].player_id for i in breaks}

        display_list = sorted(ranked, key=lambda p: key_fn(p, replacement), reverse=(sort_dir == "desc"))

        rows_out = []
        for i, p in enumerate(display_list):
            owner_id = ownership.current_owner(conn, p.player_id) if p.player_id in owned_ids else None
            if hide_drafted and owner_id is not None:
                continue
            rows_out.append({
                "rank": i + 1,
                "player": p,
                "vorp": p.projected_pir_with_bonus - replacement[position],
                "owner_id": owner_id,
                "tier_break_after": is_default_sort and p.player_id in tier_break_ids,
                "is_new": p.player_id in new_ids,
                "is_estimated": p.player_id in estimated_ids,
                "injury": injury_status.get(p.player_id),
                "is_watched": p.player_id in watchlist_ids,
            })
        positions_out[position] = rows_out

    manager_names = {m["manager_id"]: m["name"] for m in managers}
    return render_template(
        "draft.html",
        positions=positions_out,
        managers=managers,
        manager_names=manager_names,
        hide_drafted=hide_drafted,
        position_filter=position_filter,
        team_filter=team_filter,
        teams=teams,
        sort_key=sort_key,
        sort_dir=sort_dir,
        sort_options=SORT_OPTIONS,
    )


@app.route("/draft/pick", methods=["POST"])
def draft_pick():
    conn = get_db()
    player_id = request.form["player_id"]
    manager_id = int(request.form["manager_id"])
    round_raw = request.form.get("round", "").strip()
    round_ = int(round_raw) if round_raw else None

    try:
        ownership.record_draft_pick(conn, player_id, manager_id, round_=round_)
        flash("Drafted successfully.", "success")
    except ValueError as e:
        flash(str(e), "error")

    redirect_args = {}
    if request.form.get("position"):
        redirect_args["position"] = request.form["position"]
    if request.form.get("team"):
        redirect_args["team"] = request.form["team"]
    if request.form.get("sort"):
        redirect_args["sort"] = request.form["sort"]
    if request.form.get("dir"):
        redirect_args["dir"] = request.form["dir"]
    if request.form.get("hide_drafted"):
        redirect_args["hide_drafted"] = request.form["hide_drafted"]
    return redirect(url_for("draft", **redirect_args))


def _redirect_after_watchlist_change():
    if request.form.get("from") == "watchlist":
        return redirect(url_for("watchlist_page"))

    redirect_args = {}
    if request.form.get("position"):
        redirect_args["position"] = request.form["position"]
    if request.form.get("team"):
        redirect_args["team"] = request.form["team"]
    if request.form.get("sort"):
        redirect_args["sort"] = request.form["sort"]
    if request.form.get("dir"):
        redirect_args["dir"] = request.form["dir"]
    if request.form.get("hide_drafted"):
        redirect_args["hide_drafted"] = request.form["hide_drafted"]
    return redirect(url_for("draft", **redirect_args))


@app.route("/watchlist/add", methods=["POST"])
def watchlist_add():
    conn = get_db()
    add_to_watchlist(conn, request.form["player_id"], request.form["player_name"])
    return _redirect_after_watchlist_change()


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove():
    conn = get_db()
    remove_from_watchlist(conn, request.form["player_id"])
    return _redirect_after_watchlist_change()


@app.route("/watchlist")
def watchlist_page():
    conn = get_db()
    watched = load_watchlist(conn)
    pool, new_ids, gone_ids, estimated_ids, injury_status = get_pool(conn)

    rows = []
    for player_id, entry in watched.items():
        p = pool.get(player_id)
        if p is None:
            continue
        rows.append({
            "player": p,
            "is_new": player_id in new_ids,
            "is_gone": player_id in gone_ids,
            "is_estimated": player_id in estimated_ids,
            "injury": injury_status.get(player_id),
            "note": entry["note"],
            "added_at": entry["added_at"],
        })
    rows.sort(key=lambda r: r["player"].projected_pir_with_bonus, reverse=True)

    return render_template("watchlist.html", rows=rows)


@app.route("/draft/import", methods=["GET"])
def draft_import():
    return render_template("draft_import.html")


@app.route("/draft/import/preview", methods=["POST"])
def draft_import_preview():
    file = request.files.get("csv_file")
    if file is None or file.filename == "":
        flash("Choose a CSV file to upload first.", "error")
        return redirect(url_for("draft_import"))

    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("Could not read that file as text - is it really a CSV export?", "error")
        return redirect(url_for("draft_import"))

    try:
        rows = parse_draft_csv(text)
    except DraftCsvError as e:
        flash(str(e), "error")
        return redirect(url_for("draft_import"))

    csv_managers = distinct_managers(rows)

    return render_template(
        "draft_import_map.html",
        csv_text=text,
        csv_managers=csv_managers,
        total_picks=len(rows),
    )


@app.route("/draft/import/commit", methods=["POST"])
def draft_import_commit():
    conn = get_db()
    text = request.form.get("csv_text", "")
    try:
        rows = parse_draft_csv(text)
    except DraftCsvError as e:
        flash(f"Could not re-read the uploaded data: {e}", "error")
        return redirect(url_for("draft_import"))

    csv_managers = distinct_managers(rows)

    if request.form.get("clear_existing") == "1":
        conn.execute("DELETE FROM ownership")
        conn.commit()

    roster_rows = load_roster(conn, CURRENT_SEASON)
    historical_players = all_known_players(conn)

    drafted = 0
    created = 0
    skipped: list[str] = []
    for cm in csv_managers:
        manager_id, was_created = ownership.get_or_create_manager(conn, cm["manager_name"])
        if was_created:
            created += 1
        resolved, diag = resolve_manager_picks(rows, cm["manager_key"], roster_rows, historical_players)
        skipped.extend(diag.get("skipped", []))
        for r in resolved:
            try:
                ownership.record_draft_pick(conn, r["player_id"], manager_id, notes="csv import")
                drafted += 1
            except ValueError as e:
                skipped.append(f"{r['player_name']}: {e}")

    flash(
        f"Imported {drafted} pick(s) across {len(csv_managers)} manager(s)"
        f" ({created} new manager(s) created)."
        + (f" {len(skipped)} skipped." if skipped else ""),
        "success" if drafted else "error",
    )
    for s in skipped[:20]:
        flash(s, "error")

    return redirect(url_for("draft"))


@app.route("/managers")
def managers_index():
    conn = get_db()
    managers = ownership.list_managers(conn)
    rows_out = [
        {"manager": m, "roster_size": len(ownership.manager_roster_ids(conn, m["manager_id"]))}
        for m in managers
    ]
    return render_template("managers_index.html", rows=rows_out)


@app.route("/managers/<int:manager_id>")
def manager_roster(manager_id: int):
    conn = get_db()
    managers = {m["manager_id"]: m for m in ownership.list_managers(conn)}
    manager = managers.get(manager_id)
    if manager is None:
        abort(404)

    pool, new_ids, gone_ids, estimated_ids, injury_status = get_pool(conn)
    player_ids = ownership.manager_roster_ids(conn, manager_id)

    by_position: dict[str, list[Projection]] = {position: [] for position in REQUIRED_COUNTS}
    unresolved = 0
    for pid in player_ids:
        p = pool.get(pid)
        if p is None:
            unresolved += 1
            continue
        by_position.setdefault(p.position, []).append(p)
    for players in by_position.values():
        players.sort(key=lambda p: p.projected_pir_with_bonus, reverse=True)

    return render_template(
        "manager_roster.html",
        manager=manager,
        by_position=by_position,
        total=len(player_ids),
        unresolved=unresolved,
        new_ids=new_ids,
        gone_ids=gone_ids,
        estimated_ids=estimated_ids,
        injury_status=injury_status,
    )


@app.route("/transactions", methods=["GET"])
def transactions():
    conn = get_db()
    managers = ownership.list_managers(conn)
    manager_names = {m["manager_id"]: m["name"] for m in managers}
    pool, _new_ids, _gone_ids, _estimated_ids, _injury_status = get_pool(conn)
    history = ownership.transaction_history(conn, limit=200)
    for t in history:
        p = pool.get(t["player_id"])
        t["player_name"] = p.player_name if p else t["player_id"]
    return render_template("transactions.html", managers=managers, manager_names=manager_names, history=history)


@app.route("/transactions/add", methods=["POST"])
def transactions_add():
    conn = get_db()
    player_id = request.form["player_id"].strip()
    manager_id = int(request.form["manager_id"])
    round_raw = request.form.get("round", "").strip()
    round_ = int(round_raw) if round_raw else None
    notes = request.form.get("notes", "").strip() or None

    try:
        ownership.record_free_agent_add(conn, player_id, manager_id, round_=round_, notes=notes)
        flash("Free-agent add recorded.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("transactions"))


@app.route("/transactions/drop", methods=["POST"])
def transactions_drop():
    conn = get_db()
    player_id = request.form["player_id"].strip()
    round_raw = request.form.get("round", "").strip()
    round_ = int(round_raw) if round_raw else None
    notes = request.form.get("notes", "").strip() or None

    try:
        ownership.record_drop(conn, player_id, round_=round_, notes=notes)
        flash("Drop recorded.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("transactions"))


@app.route("/transactions/trade", methods=["POST"])
def transactions_trade():
    conn = get_db()
    player_a_id = request.form["player_a_id"].strip()
    manager_a_id = int(request.form["manager_a_id"])
    player_b_id = request.form["player_b_id"].strip()
    manager_b_id = int(request.form["manager_b_id"])
    round_raw = request.form.get("round", "").strip()
    round_ = int(round_raw) if round_raw else None
    notes = request.form.get("notes", "").strip() or None

    try:
        ownership.record_trade(
            conn, player_a_id, manager_a_id, player_b_id, manager_b_id, round_=round_, notes=notes
        )
        flash("Trade recorded.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("transactions"))


@app.route("/transfers")
def transfers():
    conn = get_db()
    managers = ownership.list_managers(conn)
    manager_id_raw = request.args.get("manager_id")
    watchlist_ids = set(load_watchlist(conn).keys())

    roster = None
    roster_error = None
    suggestions = []
    selected_manager_id = None
    selected_drop = None
    add_candidates = []
    manager_names = {}
    owners = {}
    compare = None

    if manager_id_raw:
        selected_manager_id = int(manager_id_raw)
        pool, _new_ids, gone_ids, _estimated_ids, _injury_status = get_pool(conn)
        player_ids = ownership.manager_roster_ids(conn, selected_manager_id)
        players = [pool[pid] for pid in player_ids if pid in pool]
        unresolved = len(player_ids) - len(players)

        try:
            roster = Roster(players=players)
        except ValueError as e:
            roster_error = f"{e} (drafted so far: {len(player_ids)}/13, {unresolved} not found in current pool)"

        if roster is not None:
            owned_ids = ownership.all_owned_ids(conn)
            # A player no longer part of the current roster composition can
            # still be a DROP candidate (roster.players, built above, keeps
            # them) but should never be suggested as an ADD - they're not
            # really acquirable.
            addable_pool = {pid: p for pid, p in pool.items() if pid not in gone_ids}
            suggestions = suggest_transfers(roster, addable_pool, owned_ids=owned_ids)

            manager_names = {m["manager_id"]: m["name"] for m in managers}
            owners = ownership.owner_map(conn)
            roster_ids = {p.player_id for p in roster.players}

            drop_id = request.args.get("drop_id")
            if drop_id in roster_ids:
                selected_drop = pool[drop_id]
                # Same-position only - a same-position swap is the only kind
                # of transfer this roster model supports. Excludes this
                # manager's own roster (already owned, not "addable") but
                # otherwise includes players owned by other managers too,
                # each annotated with their owner, purely for comparison -
                # not necessarily actually available without a trade.
                add_candidates = sorted(
                    (
                        p for pid, p in addable_pool.items()
                        if p.position == selected_drop.position and pid not in roster_ids
                    ),
                    key=lambda p: p.projected_pir_with_bonus,
                    reverse=True,
                )

                add_id = request.args.get("add_id")
                addable_ids = {p.player_id for p in add_candidates}
                if add_id in addable_ids:
                    compare = {
                        "drop": selected_drop,
                        "add": pool[add_id],
                        "gain": projected_gain(selected_drop, pool[add_id]),
                    }

    return render_template(
        "transfers.html",
        managers=managers,
        selected_manager_id=selected_manager_id,
        roster=roster,
        roster_error=roster_error,
        suggestions=suggestions,
        selected_drop=selected_drop,
        add_candidates=add_candidates,
        owners=owners,
        manager_names=manager_names,
        watchlist_ids=watchlist_ids,
        compare=compare,
    )


@dataclass
class LineupRecommendation:
    """Everything the /lineup page shows for one manager + round - also
    reused by the points tracker (/tracker) to snapshot what the tool
    suggested at the moment the user saved their own real lineup."""

    roster: Roster | None = None
    roster_error: str | None = None
    schedule_error: str | None = None
    active_squad: ActiveSquad | None = None
    initial: Lineup | None = None
    recommended: Lineup | None = None
    excluded: list[Projection] = field(default_factory=list)
    team_dates: dict[str, str] = field(default_factory=dict)
    used_day1_actuals: bool = False
    initial_full_ids: set[str] = field(default_factory=set)
    no_swap_total: float | None = None
    recommended_total: float | None = None
    injury_status: dict[str, dict] = field(default_factory=dict)
    pool: dict[str, Projection] = field(default_factory=dict)


def compute_recommendation(
    conn: sqlite3.Connection,
    manager_id: int,
    round_no: int,
    formation: tuple[int, int, int] | None = None,
) -> LineupRecommendation:
    roster = None
    active_squad = None
    roster_error = None
    schedule_error = None
    initial = None
    recommended = None
    excluded: list[Projection] = []
    team_dates: dict[str, str] = {}
    used_day1_actuals = False
    initial_full_ids: set[str] = set()
    no_swap_total = None
    recommended_total = None

    pool, _new_ids, _gone_ids, _estimated_ids, injury_status = get_pool(conn)
    out_ids = {pid for pid, inj in injury_status.items() if inj["status"] in EXCLUDING_STATUSES}
    player_ids = ownership.manager_roster_ids(conn, manager_id)
    players = [pool[pid] for pid in player_ids if pid in pool]
    unresolved = len(player_ids) - len(players)

    try:
        roster = Roster(players=players)
    except ValueError as e:
        roster_error = f"{e} (drafted so far: {len(player_ids)}/13, {unresolved} not found in current pool)"

    if roster is not None:
        schedule_rows = load_schedule(conn, CURRENT_SEASON, round_no=round_no)
        team_dates = team_dates_from_schedule(schedule_rows, round_no)

        if not team_dates:
            schedule_error = (
                f"No scheduled games found for round {round_no} of {CURRENT_SEASON}. "
                f"Either the schedule hasn't been synced yet (see Sync) or this round genuinely "
                f"has no games - try a different round."
            )
        else:
            # An "Out" player (basketnews injury report - see
            # engine.injuries) is zeroed here rather than hard-excluded:
            # they genuinely can't outscore anyone, so choose_active_squad's
            # brute-force search naturally benches/excludes them on its
            # own merit - including gracefully handling more "Out"
            # players than there are exclusion slots (they'd just fill
            # the least-bad bench spots at a real 0, correctly modeled).
            projected_value = (
                lambda pid: 0.0 if pid in out_ids else pool[pid].projected_pir_with_bonus
            )  # noqa: E731
            try:
                active_squad = choose_active_squad(roster, team_dates, projected_value, formation=formation)
                initial = build_lineup(active_squad, team_dates, projected_value, formation=formation)

                # The real swap decision is made AFTER day 1's games are
                # actually over, using their real results - not their
                # pre-round projection (see engine.lineup's 2026-09-16
                # correction: demoting a day-1 player now genuinely
                # halves their score, so blindly trusting a projection
                # that may since have been proven wrong is a real risk,
                # not just a missed upside). player_game_stats only ever
                # has played games, so if day 1's box scores have been
                # synced (see Sync) by the time this page is visited,
                # use their actual fantasy score (PIR + the real +10%
                # win bonus, now that the real outcome is known - see
                # engine.projections.actual_fantasy_score, confirmed
                # 2026-09-22) for the swap decision instead of the
                # projection; day-2 teams always use their projection
                # (which already includes an *expected* win bonus based
                # on recent win rate) since their games haven't happened
                # yet.
                min_date = min(team_dates.values())
                day1_teams = {team for team, date in team_dates.items() if date == min_date}
                day1_actual = {
                    r["player_id"]: actual_fantasy_score(r["pir_official"], r.get("team_win"))
                    for r in load_rows(conn, CURRENT_SEASON)
                    if r.get("round") == round_no and r.get("team") in day1_teams
                }
                used_day1_actuals = bool(day1_actual)
                decision_value = (
                    lambda pid: day1_actual[pid] if pid in day1_actual else projected_value(pid)
                )  # noqa: E731

                recommended = swap_after_day1(initial, team_dates, decision_value, formation=formation)
                excluded = active_squad.excluded
                initial_full_ids = {p.player_id for p in initial.starters} | {initial.sixth_man.player_id}

                # Total projected score box: what the round is worth
                # under each lineup, using actual PIR for anyone who's
                # already played (day1_actual) and projections for
                # everyone else - the same "best information available
                # right now" values used for the swap decision above.
                score_value = {p.player_id: decision_value(p.player_id) for p in active_squad.active}
                no_swap_total = compute_round_score(initial, score_value)
                recommended_total = compute_round_score(recommended, score_value)
            except RuntimeError as e:
                schedule_error = f"Could not build a lineup for this round: {e}"

    return LineupRecommendation(
        roster=roster,
        roster_error=roster_error,
        schedule_error=schedule_error,
        active_squad=active_squad,
        initial=initial,
        recommended=recommended,
        excluded=excluded,
        team_dates=team_dates,
        used_day1_actuals=used_day1_actuals,
        initial_full_ids=initial_full_ids,
        no_swap_total=no_swap_total,
        recommended_total=recommended_total,
        injury_status=injury_status,
        pool=pool,
    )


@app.route("/lineup")
def lineup():
    conn = get_db()
    managers = ownership.list_managers(conn)

    manager_id_raw = request.args.get("manager_id")
    selected_manager_id = int(manager_id_raw) if manager_id_raw else None

    rounds = sorted({r["round"] for r in load_schedule(conn, CURRENT_SEASON) if r.get("round") is not None})
    default_round = next_unplayed_round(conn, CURRENT_SEASON)
    round_raw = request.args.get("round")
    selected_round = int(round_raw) if round_raw else default_round

    formation_raw = request.args.get("formation")
    try:
        selected_formation = tuple(int(n) for n in formation_raw.split("-")) if formation_raw else None
    except ValueError:
        selected_formation = None
    if selected_formation not in VALID_FORMATIONS:
        selected_formation = None  # "Auto" or a garbled/unrecognized value - fall back to auto-search

    rec = LineupRecommendation()
    if selected_manager_id and selected_round:
        rec = compute_recommendation(conn, selected_manager_id, selected_round, selected_formation)

    return render_template(
        "lineup.html",
        managers=managers,
        rounds=rounds,
        selected_manager_id=selected_manager_id,
        selected_round=selected_round,
        roster=rec.roster,
        roster_error=rec.roster_error,
        schedule_error=rec.schedule_error,
        initial=rec.initial,
        recommended=rec.recommended,
        excluded=rec.excluded,
        team_dates=rec.team_dates,
        availability_label=availability_label,
        current_season=CURRENT_SEASON,
        used_day1_actuals=rec.used_day1_actuals,
        initial_full_ids=rec.initial_full_ids,
        valid_formations=VALID_FORMATIONS,
        selected_formation=selected_formation,
        no_swap_total=rec.no_swap_total,
        recommended_total=rec.recommended_total,
        injury_status=rec.injury_status,
    )


if __name__ == "__main__":
    app.run(debug=True)
