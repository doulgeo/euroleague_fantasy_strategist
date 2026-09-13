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

import sqlite3

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from draft_board import build_draft_board, replacement_values, tier_breaks
from engine import ownership
from engine.db import DB_PATH, get_connection, load_rows
from engine.projections import Projection, build_projections
from engine.roster import REQUIRED_COUNTS, Roster
from engine.transfers import suggest_transfers

# Edit these once per year as seasons roll over.
CURRENT_SEASON = "E2026"
PRIOR_SEASON = "E2025"

app = Flask(__name__)
app.secret_key = "euroleague-fantasy-local-dev"  # local single-user tool, not internet-facing

_projection_cache: dict[tuple, tuple[int, dict[str, Projection]]] = {}


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


def get_pool(conn: sqlite3.Connection) -> dict[str, Projection]:
    season_code, as_of_round = _resolve_pool_source(conn)
    key = (season_code, as_of_round)
    mtime_ns = DB_PATH.stat().st_mtime_ns

    cached = _projection_cache.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]

    rows = load_rows(conn, season_code=season_code)
    projections = build_projections(rows, as_of_round=as_of_round)
    _projection_cache[key] = (mtime_ns, projections)
    return projections


@app.route("/")
def index():
    conn = get_db()
    owned_count = len(ownership.all_owned_ids(conn))
    manager_count = len(ownership.list_managers(conn))
    return render_template("index.html", owned_count=owned_count, manager_count=manager_count)


@app.route("/draft")
def draft():
    conn = get_db()
    pool = get_pool(conn)
    board = build_draft_board(pool)
    replacement = replacement_values(board)
    managers = ownership.list_managers(conn)
    owned_ids = ownership.all_owned_ids(conn)

    hide_drafted = request.args.get("hide_drafted") == "1"
    position_filter = request.args.get("position")

    positions_out = {}
    for position in ("Guard", "Forward", "Center"):
        if position_filter and position_filter != position:
            continue
        ranked = board.get(position, [])
        breaks = tier_breaks(ranked)
        rows_out = []
        for i, p in enumerate(ranked):
            owner_id = ownership.current_owner(conn, p.player_id) if p.player_id in owned_ids else None
            if hide_drafted and owner_id is not None:
                continue
            rows_out.append({
                "rank": i + 1,
                "player": p,
                "vorp": p.projected_pir_with_bonus - replacement[position],
                "owner_id": owner_id,
                "tier_break_after": i in breaks,
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
    if request.form.get("hide_drafted"):
        redirect_args["hide_drafted"] = request.form["hide_drafted"]
    return redirect(url_for("draft", **redirect_args))


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

    pool = get_pool(conn)
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
    )


@app.route("/transactions", methods=["GET"])
def transactions():
    conn = get_db()
    managers = ownership.list_managers(conn)
    manager_names = {m["manager_id"]: m["name"] for m in managers}
    pool = get_pool(conn)
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
        pool = get_pool(conn)
        player_ids = ownership.manager_roster_ids(conn, selected_manager_id)
        players = [pool[pid] for pid in player_ids if pid in pool]
        unresolved = len(player_ids) - len(players)

        try:
            roster = Roster(players=players)
        except ValueError as e:
            roster_error = f"{e} (drafted so far: {len(player_ids)}/13, {unresolved} not found in current pool)"

        if roster is not None:
            owned_ids = ownership.all_owned_ids(conn)
            suggestions = suggest_transfers(roster, pool, owned_ids=owned_ids)

    return render_template(
        "transfers.html",
        managers=managers,
        selected_manager_id=selected_manager_id,
        roster=roster,
        roster_error=roster_error,
        suggestions=suggestions,
    )


if __name__ == "__main__":
    app.run(debug=True)
