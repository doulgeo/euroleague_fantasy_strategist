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
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from draft_board import build_draft_board, replacement_values, tier_breaks
from engine import ownership
from engine.draft_import import DraftCsvError, distinct_managers, parse_draft_csv, resolve_manager_picks
from engine.db import (
    DB_PATH,
    all_known_players,
    fantasy_pool_synced_at,
    get_connection,
    known_player_ids,
    latest_game_date,
    load_fantasy_pool,
    load_roster,
    load_rows,
    load_schedule,
    next_unplayed_round,
    roster_synced_at,
    row_count,
)
from engine.fantasy_pool import resolve_pool_rows
from engine.lineup import (
    VALID_FORMATIONS,
    availability_label,
    build_lineup,
    choose_active_squad,
    compute_round_score,
    swap_after_day1,
    team_dates_from_schedule,
)
from engine.projections import Projection, build_projections
from engine.roster import REQUIRED_COUNTS, Roster
from engine.dev_draft import randomize_draft
from engine.rosters import merge_roster
from engine.transfers import suggest_transfers

# Edit these once per year as seasons roll over.
CURRENT_SEASON = "E2026"
PRIOR_SEASON = "E2025"
KNOWN_SEASONS = ["E2023", "E2024", "E2025", "E2026"]

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"

app = Flask(__name__)
app.secret_key = "euroleague-fantasy-local-dev"  # local single-user tool, not internet-facing

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
}


def _run_sync(kind: str, cmd: list[str], log_path: Path) -> None:
    with open(log_path, "w") as f:
        proc = subprocess.run(cmd, cwd=BASE_DIR, stdout=f, stderr=subprocess.STDOUT, text=True)
    with _sync_lock:
        _sync_state[kind]["running"] = False
        _sync_state[kind]["finished_at"] = datetime.now(timezone.utc).isoformat()
        _sync_state[kind]["returncode"] = proc.returncode
    _projection_cache.clear()  # the DB just changed under us - don't serve a stale pool


def _start_sync(kind: str, cmd: list[str], seasons: list[str]) -> bool:
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
    thread = threading.Thread(target=_run_sync, args=(kind, cmd, log_path), daemon=True)
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


def _resolve_pool_source(conn: sqlite3.Connection) -> tuple[str, int]:
    """(season_code, as_of_round) to build the player pool from right now."""
    current_rows = load_rows(conn, season_code=CURRENT_SEASON)
    if not current_rows:
        rows = load_rows(conn, season_code=PRIOR_SEASON)
        if not rows:
            return PRIOR_SEASON, 1
        max_round = max(r["round"] for r in rows if r.get("round") is not None)
        return PRIOR_SEASON, max_round + 1

    max_round = max(r["round"] for r in current_rows if r.get("round") is not None)
    return CURRENT_SEASON, max_round + 1


def get_pool(conn: sqlite3.Connection) -> tuple[dict[str, Projection], set[str], set[str]]:
    """(projections, new_player_ids, gone_player_ids) - the projection pool
    merged with the current roster composition, so a transferred player
    shows their real current team, a player with no box-score history yet
    (new to the league) still appears flagged rather than being silently
    absent, and a player no longer part of the current roster composition
    is flagged rather than looking like a normal draftable/tradeable
    player.

    Roster composition source: the real EuroLeague Fantasy draft pool
    (engine.fantasy_pool, a user-maintained Google Sheet synced via
    sync_fantasy_pool.py into `fantasy_pool`) when it's been synced -
    found (2026-09-15) to be a more reliable source than EuroLeague's own
    /people endpoint, which was significantly incomplete pre-season for
    some clubs (ASVEL, Barcelona). Falls back to /people
    (engine.db `rosters`, via sync_rosters.py) if the Fantasy pool hasn't
    been synced yet - never blocks on missing data."""
    season_code, as_of_round = _resolve_pool_source(conn)
    key = (season_code, as_of_round)
    mtime_ns = DB_PATH.stat().st_mtime_ns

    cached = _projection_cache.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]

    rows = load_rows(conn, season_code=season_code)
    projections = build_projections(rows, as_of_round=as_of_round)

    roster_rows = load_roster(conn, CURRENT_SEASON)
    seen = known_player_ids(conn)

    fantasy_pool_rows = load_fantasy_pool(conn)
    if fantasy_pool_rows:
        historical_players = all_known_players(conn)
        resolved_rows, _diag = resolve_pool_rows(fantasy_pool_rows, roster_rows, historical_players)
        projections, new_ids, gone_ids = merge_roster(projections, resolved_rows, seen)
    else:
        projections, new_ids, gone_ids = merge_roster(projections, roster_rows, seen)

    result = (projections, new_ids, gone_ids)
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

    pool, _new_ids, gone_ids = get_pool(conn)
    draftable_pool = {pid: p for pid, p in pool.items() if pid not in gone_ids}
    manager_ids = [m["manager_id"] for m in managers]

    try:
        counts = randomize_draft(conn, draftable_pool, manager_ids)
    except RuntimeError as e:
        flash(f"Randomized draft failed: {e}", "error")
        return redirect(url_for("sync_page"))

    flash(f"Randomized a fresh draft: {sum(counts.values())} picks across {len(counts)} managers.", "success")
    return redirect(url_for("sync_page"))


@app.route("/sync/db", methods=["POST"])
def sync_db_trigger():
    seasons = [s for s in KNOWN_SEASONS if request.form.get(f"season_{s}") == "1"]
    if not seasons:
        seasons = [CURRENT_SEASON]
    cmd = [sys.executable, "sync_db.py", "--seasons", *seasons]
    if _start_sync("db", cmd, seasons):
        flash(f"Started syncing box scores for {', '.join(seasons)} in the background.", "success")
    else:
        flash("A box-score sync is already running - wait for it to finish.", "error")
    return redirect(url_for("sync_page"))


@app.route("/sync/rosters", methods=["POST"])
def sync_rosters_trigger():
    cmd = [sys.executable, "sync_rosters.py", "--season", CURRENT_SEASON]
    if _start_sync("rosters", cmd, [CURRENT_SEASON]):
        flash(f"Started syncing {CURRENT_SEASON} rosters in the background.", "success")
    else:
        flash("A roster sync is already running - wait for it to finish.", "error")
    return redirect(url_for("sync_page"))


@app.route("/sync/fantasy-pool", methods=["POST"])
def sync_fantasy_pool_trigger():
    cmd = [sys.executable, "sync_fantasy_pool.py", "--season", CURRENT_SEASON]
    if _start_sync("fantasy_pool", cmd, [CURRENT_SEASON]):
        flash("Started syncing the Fantasy draft pool in the background.", "success")
    else:
        flash("A Fantasy pool sync is already running - wait for it to finish.", "error")
    return redirect(url_for("sync_page"))


@app.route("/draft")
def draft():
    conn = get_db()
    pool, new_ids, gone_ids = get_pool(conn)
    draftable_pool = {pid: p for pid, p in pool.items() if pid not in gone_ids}
    board = build_draft_board(draftable_pool)
    replacement = replacement_values(board)
    managers = ownership.list_managers(conn)
    owned_ids = ownership.all_owned_ids(conn)

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

    conn = get_db()
    managers = ownership.list_managers(conn)
    manager_by_lower_name = {m["name"].strip().lower(): m["manager_id"] for m in managers}

    csv_managers = distinct_managers(rows)
    for cm in csv_managers:
        cm["suggested_manager_id"] = manager_by_lower_name.get(cm["manager_name"].strip().lower())

    return render_template(
        "draft_import_map.html",
        csv_text=text,
        csv_managers=csv_managers,
        managers=managers,
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
    mapping: dict[str, int] = {}
    for cm in csv_managers:
        raw = request.form.get(f"manager_for_{cm['manager_key']}", "").strip()
        if raw:
            mapping[cm["manager_key"]] = int(raw)

    if not mapping:
        flash("No CSV managers were mapped to a league manager - nothing imported.", "error")
        return redirect(url_for("draft_import"))

    if request.form.get("clear_existing") == "1":
        conn.execute("DELETE FROM ownership")
        conn.commit()

    roster_rows = load_roster(conn, CURRENT_SEASON)
    historical_players = all_known_players(conn)

    drafted = 0
    skipped: list[str] = []
    for manager_key, manager_id in mapping.items():
        resolved, diag = resolve_manager_picks(rows, manager_key, roster_rows, historical_players)
        skipped.extend(diag.get("skipped", []))
        for r in resolved:
            try:
                ownership.record_draft_pick(conn, r["player_id"], manager_id, notes="csv import")
                drafted += 1
            except ValueError as e:
                skipped.append(f"{r['player_name']}: {e}")

    flash(
        f"Imported {drafted} pick(s) across {len(mapping)} manager(s)."
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

    pool, new_ids, gone_ids = get_pool(conn)
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
    )


@app.route("/transactions", methods=["GET"])
def transactions():
    conn = get_db()
    managers = ownership.list_managers(conn)
    manager_names = {m["manager_id"]: m["name"] for m in managers}
    pool, _new_ids, _gone_ids = get_pool(conn)
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

    roster = None
    roster_error = None
    suggestions = []
    selected_manager_id = None

    if manager_id_raw:
        selected_manager_id = int(manager_id_raw)
        pool, _new_ids, gone_ids = get_pool(conn)
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

    return render_template(
        "transfers.html",
        managers=managers,
        selected_manager_id=selected_manager_id,
        roster=roster,
        roster_error=roster_error,
        suggestions=suggestions,
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

    roster = None
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

    if selected_manager_id and selected_round:
        pool, _new_ids, _gone_ids = get_pool(conn)
        player_ids = ownership.manager_roster_ids(conn, selected_manager_id)
        players = [pool[pid] for pid in player_ids if pid in pool]
        unresolved = len(player_ids) - len(players)

        try:
            roster = Roster(players=players)
        except ValueError as e:
            roster_error = f"{e} (drafted so far: {len(player_ids)}/13, {unresolved} not found in current pool)"

        if roster is not None:
            schedule_rows = load_schedule(conn, CURRENT_SEASON, round_no=selected_round)
            team_dates = team_dates_from_schedule(schedule_rows, selected_round)

            if not team_dates:
                schedule_error = (
                    f"No scheduled games found for round {selected_round} of {CURRENT_SEASON}. "
                    f"Either the schedule hasn't been synced yet (see Sync) or this round genuinely "
                    f"has no games - try a different round."
                )
            else:
                projected_value = lambda pid: pool[pid].projected_pir_with_bonus  # noqa: E731
                try:
                    active_squad = choose_active_squad(roster, team_dates, projected_value, formation=selected_formation)
                    initial = build_lineup(active_squad, team_dates, projected_value, formation=selected_formation)

                    # The real swap decision is made AFTER day 1's games are
                    # actually over, using their real results - not their
                    # pre-round projection (see engine.lineup's 2026-09-16
                    # correction: demoting a day-1 player now genuinely
                    # halves their score, so blindly trusting a projection
                    # that may since have been proven wrong is a real risk,
                    # not just a missed upside). player_game_stats only ever
                    # has played games, so if day 1's box scores have been
                    # synced (see Sync) by the time this page is visited,
                    # use their actual PIR for the swap decision instead of
                    # the projection; day-2 teams always use their
                    # projection since their games haven't happened yet.
                    min_date = min(team_dates.values())
                    day1_teams = {team for team, date in team_dates.items() if date == min_date}
                    day1_actual = {
                        r["player_id"]: r["pir_official"]
                        for r in load_rows(conn, CURRENT_SEASON)
                        if r.get("round") == selected_round and r.get("team") in day1_teams
                    }
                    used_day1_actuals = bool(day1_actual)
                    decision_value = (
                        lambda pid: day1_actual[pid] if pid in day1_actual else projected_value(pid)
                    )  # noqa: E731

                    recommended = swap_after_day1(initial, team_dates, decision_value, formation=selected_formation)
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

    return render_template(
        "lineup.html",
        managers=managers,
        rounds=rounds,
        selected_manager_id=selected_manager_id,
        selected_round=selected_round,
        roster=roster,
        roster_error=roster_error,
        schedule_error=schedule_error,
        initial=initial,
        recommended=recommended,
        excluded=excluded,
        team_dates=team_dates,
        availability_label=availability_label,
        current_season=CURRENT_SEASON,
        used_day1_actuals=used_day1_actuals,
        initial_full_ids=initial_full_ids,
        valid_formations=VALID_FORMATIONS,
        selected_formation=selected_formation,
        no_swap_total=no_swap_total,
        recommended_total=recommended_total,
    )


if __name__ == "__main__":
    app.run(debug=True)
