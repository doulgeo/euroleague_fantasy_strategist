"""
SQLite persistence for the flat player-game row table (see
engine.data.FIELDNAMES for the schema these rows share).

The JSON cache under raw/ (engine.data.EuroleagueClient) stays the source of
truth for raw API responses - this is the normalized, query-ready copy of
the same rows, meant to be what the rest of the app reads from.

Idempotent by design: every write is an upsert keyed on
(season_code, game_code, player_id), so re-running a sync after each
gameweek (or reloading the whole cache) is always safe and never creates
duplicates.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from engine.data import FIELDNAMES

DB_PATH = Path(__file__).parent.parent / "euroleague.db"

_KEY_FIELDS = ("season_code", "game_code", "player_id")
_BOOL_FIELDS = ("is_starter", "played")
_NULLABLE_BOOL_FIELDS = ("team_win",)

_INT_FIELDS = {
    "game_code", "round", "minutes_seconds", "points", "fg2m", "fg2a", "fg3m", "fg3a",
    "ftm", "fta", "oreb", "dreb", "reb", "assists", "steals", "turnovers", "blocks",
    "blocks_against", "fouls_committed", "fouls_drawn", "plus_minus", "pir_official",
    "pir_recomputed", "pir_diff",
}

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS player_game_stats (
    {", ".join(
        f"{f} {'INTEGER' if f in _INT_FIELDS or f in _BOOL_FIELDS or f in _NULLABLE_BOOL_FIELDS else 'TEXT'}"
        for f in FIELDNAMES
    )},
    PRIMARY KEY (season_code, game_code, player_id)
)
"""

_CREATE_ROUND_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_player_game_stats_season_round "
    "ON player_game_stats (season_code, round)"
)

# --- Manual draft/ownership/transaction tracking (engine.ownership) ---
#
# These three tables have nothing to do with the box-score sync above; they
# hold what the user manually logs about the 12-manager league (who drafted
# whom, trades, adds/drops). `ownership` is a materialized "who owns this
# player right now" table kept in sync with `transactions` (the append-only
# log) on every write in engine.ownership - see that module's docstring.

_CREATE_MANAGERS_SQL = """
CREATE TABLE IF NOT EXISTS managers (
    manager_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
)
"""

_CREATE_OWNERSHIP_SQL = """
CREATE TABLE IF NOT EXISTS ownership (
    player_id TEXT PRIMARY KEY,
    manager_id INTEGER NOT NULL REFERENCES managers(manager_id),
    acquired_at TEXT NOT NULL,
    acquired_via TEXT NOT NULL
)
"""

_CREATE_OWNERSHIP_MANAGER_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_ownership_manager ON ownership (manager_id)"
)

_CREATE_TRANSACTIONS_SQL = """
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    round INTEGER,
    type TEXT NOT NULL,
    player_id TEXT NOT NULL,
    from_manager_id INTEGER REFERENCES managers(manager_id),
    to_manager_id INTEGER REFERENCES managers(manager_id),
    group_id TEXT,
    notes TEXT
)
"""

_CREATE_TRANSACTIONS_PLAYER_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_transactions_player ON transactions (player_id)"
)

_CREATE_TRANSACTIONS_GROUP_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_transactions_group ON transactions (group_id)"
)

# --- Current-season club rosters (engine.rosters / sync_rosters.py) ---
#
# Sourced from the v2 /people endpoint (engine.data.EuroleagueClient.list_people)
# - who's on which club's roster *right now*, independent of whether they've
# played a game yet this season. Unlike player_game_stats (an immutable
# historical log, upsert-only), this is current state: sync_rosters.py does
# a full delete-and-reinsert per season each run, so a player who leaves a
# club stops appearing here rather than lingering as a stale row.

_CREATE_ROSTERS_SQL = """
CREATE TABLE IF NOT EXISTS rosters (
    season_code TEXT NOT NULL,
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    position TEXT,
    team TEXT,
    team_name TEXT,
    dorsal TEXT,
    active INTEGER NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (season_code, player_id)
)
"""

_CREATE_ROSTERS_TEAM_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_rosters_team ON rosters (season_code, team)"
)

_ROSTER_COLUMNS = (
    "season_code", "player_id", "player_name", "position", "team", "team_name", "dorsal", "active", "synced_at",
)

# --- Real EuroLeague Fantasy draft pool (engine.fantasy_pool / sync_fantasy_pool.py) ---
#
# A user-maintained Google Sheet (not the EuroLeague API) tracking the real
# game's actual draftable pool - who's currently eligible to draft, per the
# real Fantasy platform, independent of what this project derives on its own
# from box scores + club rosters. Same "replace per sync" pattern as
# `rosters`: no stable ID in the source, so a row here can't be upserted
# against a prior one - a full snapshot replace keeps it honest each run.
# Matching these rows to this project's own player_id (there's no shared ID)
# happens live in engine.fantasy_pool, not at sync time - this table just
# holds the raw synced rows.

_CREATE_FANTASY_POOL_SQL = """
CREATE TABLE IF NOT EXISTS fantasy_pool (
    name TEXT NOT NULL,
    surname TEXT NOT NULL,
    position TEXT NOT NULL,
    team TEXT NOT NULL,
    synced_at TEXT NOT NULL
)
"""

_FANTASY_POOL_COLUMNS = ("name", "surname", "position", "team", "synced_at")

# --- Season schedule (engine.data.normalize_schedule / sync_db.py) ---
#
# Every game in a season, played or not - `player_game_stats` only ever has
# rows for games that have already been played (fetch_season filters to
# `played` before ever writing anything), so it can't answer "who plays
# whom, and when, in a round that hasn't happened yet." This table can -
# it's what makes a lineup recommendation possible *before* a round starts,
# not just a review of one after the fact. Same "replace per scope" pattern
# as `rosters` rather than upsert: real schedules get rescheduled (see
# docs/technical_notes.md's round-33 E2025 note), so a sync must be able to
# make a stale date/matchup disappear, not just add to what's there.

_CREATE_SCHEDULE_SQL = """
CREATE TABLE IF NOT EXISTS schedule (
    season_code TEXT NOT NULL,
    game_code INTEGER NOT NULL,
    round INTEGER,
    game_date TEXT,
    local_team_code TEXT,
    road_team_code TEXT,
    played INTEGER NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (season_code, game_code)
)
"""

_CREATE_SCHEDULE_ROUND_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_schedule_season_round ON schedule (season_code, round)"
)

_SCHEDULE_COLUMNS = (
    "season_code", "game_code", "round", "game_date", "local_team_code", "road_team_code", "played", "synced_at",
)


# --- Manual projection overrides (engine.rosters.merge_roster) ---
#
# A player with zero box-score history anywhere (E2023+) gets a flat 0.0
# placeholder projection from merge_roster - there's no local data to base
# a real one on, e.g. a mid-season NBA transfer with no EuroLeague games at
# all. Rather than inventing a heuristic number for that case, this table
# holds a human-entered (or Claude-researched, via web search on the
# player's recent form/role - see set_manual_projection.py) estimate that
# merge_roster substitutes in place of 0.0 when present. One row per
# player - INSERT OR REPLACE keeps this idempotent to rerun/update.

_CREATE_MANUAL_PROJECTIONS_SQL = """
CREATE TABLE IF NOT EXISTS manual_projections (
    player_id TEXT PRIMARY KEY,
    player_name TEXT NOT NULL,
    projected_pir REAL NOT NULL,
    note TEXT,
    set_at TEXT NOT NULL
)
"""


# --- Watchlist (transfer shortlist) ---
#
# A personal shortlist of players the user is keeping an eye on for future
# transfers - independent of the 12-manager ownership model, since this is
# the user's own planning tool rather than a per-manager feature. One row
# per player - INSERT OR REPLACE keeps this idempotent to rerun/update.

_CREATE_WATCHLIST_SQL = """
CREATE TABLE IF NOT EXISTS watchlist (
    player_id TEXT PRIMARY KEY,
    player_name TEXT NOT NULL,
    note TEXT,
    added_at TEXT NOT NULL
)
"""


# --- Injury report (engine.injuries / sync_injuries.py) ---
#
# Scraped from basketnews.com's EuroLeague injury report - a third-party
# page, not the EuroLeague API, with no stable row ID. Same "replace per
# sync" pattern as `fantasy_pool`: a full snapshot each run, since a
# cleared/no-longer-listed player should stop appearing rather than linger
# with a stale status. Rows are stored raw (as scraped); resolving each one
# to this project's own player_id happens live at read time
# (engine.injuries.resolve_injury_rows), not persisted here - see that
# module's docstring for why.

_CREATE_INJURIES_SQL = """
CREATE TABLE IF NOT EXISTS injuries (
    team_name TEXT NOT NULL,
    team TEXT,
    position TEXT,
    player_name TEXT NOT NULL,
    status TEXT NOT NULL,
    round_text TEXT,
    comment TEXT,
    synced_at TEXT NOT NULL
)
"""

_INJURY_COLUMNS = ("team_name", "team", "position", "player_name", "status", "round_text", "comment", "synced_at")


# --- Points tracker (engine.tracker / the /tracker pages) ---
#
# The user's REAL per-round lineups, as opposed to /lineup's live-recomputed
# suggestion. One tracked_lineups row per (season, round, manager, kind):
# kind is my_day1 / my_final (what the user actually played, before and
# after the Thu/Fri swap window) or tool_day1 / tool_final (a snapshot of
# what /lineup suggested at the moment the user saved theirs - recomputing
# it later would use projections fed by later rounds, i.e. hindsight).
# tracked_lineup_players snapshots all 13 roster players with their slot,
# so history survives later trades/drops. Re-saving a kind replaces it.
# Scores are NOT stored - always recomputed from player_game_stats, so a
# late box-score sync or a scoring-rule fix flows through automatically.

_CREATE_TRACKED_LINEUPS_SQL = """
CREATE TABLE IF NOT EXISTS tracked_lineups (
    lineup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_code TEXT NOT NULL,
    round INTEGER NOT NULL,
    manager_id INTEGER NOT NULL REFERENCES managers(manager_id),
    kind TEXT NOT NULL,
    formation TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    UNIQUE (season_code, round, manager_id, kind)
)
"""

_CREATE_TRACKED_LINEUP_PLAYERS_SQL = """
CREATE TABLE IF NOT EXISTS tracked_lineup_players (
    lineup_id INTEGER NOT NULL REFERENCES tracked_lineups(lineup_id),
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    team TEXT,
    position TEXT,
    slot TEXT NOT NULL,
    projected_value REAL,
    PRIMARY KEY (lineup_id, player_id)
)
"""

# Per-round extras the user types in: the official in-game total (to
# cross-check this project's own scoring model against the real game).
_CREATE_TRACKED_ROUNDS_SQL = """
CREATE TABLE IF NOT EXISTS tracked_rounds (
    season_code TEXT NOT NULL,
    round INTEGER NOT NULL,
    manager_id INTEGER NOT NULL REFERENCES managers(manager_id),
    official_points REAL,
    notes TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season_code, round, manager_id)
)
"""

# Tiny key/value store for app-level preferences (e.g. my_manager_id - which
# of the 12 managers is the user, for the tracker).
_CREATE_APP_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_ROUND_INDEX_SQL)
    conn.execute(_CREATE_MANAGERS_SQL)
    conn.execute(_CREATE_OWNERSHIP_SQL)
    conn.execute(_CREATE_OWNERSHIP_MANAGER_INDEX_SQL)
    conn.execute(_CREATE_TRANSACTIONS_SQL)
    conn.execute(_CREATE_TRANSACTIONS_PLAYER_INDEX_SQL)
    conn.execute(_CREATE_TRANSACTIONS_GROUP_INDEX_SQL)
    conn.execute(_CREATE_ROSTERS_SQL)
    conn.execute(_CREATE_ROSTERS_TEAM_INDEX_SQL)
    conn.execute(_CREATE_FANTASY_POOL_SQL)
    conn.execute(_CREATE_SCHEDULE_SQL)
    conn.execute(_CREATE_SCHEDULE_ROUND_INDEX_SQL)
    conn.execute(_CREATE_MANUAL_PROJECTIONS_SQL)
    conn.execute(_CREATE_WATCHLIST_SQL)
    conn.execute(_CREATE_INJURIES_SQL)
    conn.execute(_CREATE_TRACKED_LINEUPS_SQL)
    conn.execute(_CREATE_TRACKED_LINEUP_PLAYERS_SQL)
    conn.execute(_CREATE_TRACKED_ROUNDS_SQL)
    conn.execute(_CREATE_APP_SETTINGS_SQL)
    conn.commit()
    return conn


def _prepare_row(row: dict) -> dict:
    prepared = dict(row)
    for f in _BOOL_FIELDS:
        prepared[f] = int(bool(prepared.get(f)))
    for f in _NULLABLE_BOOL_FIELDS:
        v = prepared.get(f)
        prepared[f] = None if v is None else int(bool(v))
    return prepared


def upsert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert or update rows, keyed on (season_code, game_code, player_id)."""
    if not rows:
        return 0

    columns = ", ".join(FIELDNAMES)
    placeholders = ", ".join(f":{f}" for f in FIELDNAMES)
    update_clause = ", ".join(f"{f}=excluded.{f}" for f in FIELDNAMES if f not in _KEY_FIELDS)
    sql = (
        f"INSERT INTO player_game_stats ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(season_code, game_code, player_id) DO UPDATE SET {update_clause}"
    )

    conn.executemany(sql, [_prepare_row(r) for r in rows])
    conn.commit()
    return len(rows)


def load_rows(conn: sqlite3.Connection, season_code: str | None = None) -> list[dict]:
    """Load rows back out in the same flat-dict shape fetch_season produces,
    so engine.projections/roster/lineup can consume them unchanged."""
    query = "SELECT * FROM player_game_stats"
    params: tuple = ()
    if season_code:
        query += " WHERE season_code = ?"
        params = (season_code,)

    cur = conn.execute(query, params)
    cols = [d[0] for d in cur.description]

    rows = []
    for record in cur.fetchall():
        row = dict(zip(cols, record))
        for f in _BOOL_FIELDS:
            row[f] = bool(row[f])
        for f in _NULLABLE_BOOL_FIELDS:
            row[f] = None if row[f] is None else bool(row[f])
        rows.append(row)
    return rows


def row_count(conn: sqlite3.Connection, season_code: str | None = None) -> int:
    query = "SELECT COUNT(*) FROM player_game_stats"
    params: tuple = ()
    if season_code:
        query += " WHERE season_code = ?"
        params = (season_code,)
    return conn.execute(query, params).fetchone()[0]


def replace_roster(conn: sqlite3.Connection, season_code: str, rows: list[dict]) -> int:
    """Full snapshot refresh: `rows` (engine.data.normalize_people output)
    replaces whatever was previously synced for this season, so a player who
    leaves a club's roster stops showing up rather than lingering as a stale
    row (unlike player_game_stats, which only ever upserts)."""
    synced_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM rosters WHERE season_code = ?", (season_code,))
    if rows:
        prepared = [{**r, "active": int(bool(r["active"])), "synced_at": synced_at} for r in rows]
        placeholders = ", ".join(f":{c}" for c in _ROSTER_COLUMNS)
        conn.executemany(
            f"INSERT INTO rosters ({', '.join(_ROSTER_COLUMNS)}) VALUES ({placeholders})",
            prepared,
        )
    conn.commit()
    return len(rows)


def load_roster(conn: sqlite3.Connection, season_code: str) -> list[dict]:
    cur = conn.execute("SELECT * FROM rosters WHERE season_code = ?", (season_code,))
    cols = [d[0] for d in cur.description]
    rows = []
    for record in cur.fetchall():
        row = dict(zip(cols, record))
        row["active"] = bool(row["active"])
        rows.append(row)
    return rows


def latest_game_date(conn: sqlite3.Connection, season_code: str) -> str | None:
    """Most recent played game's date for a season - a freshness proxy for
    the sync-status page (when the app doesn't otherwise track "last synced
    at" for box scores, unlike `rosters`)."""
    row = conn.execute(
        "SELECT MAX(game_date) FROM player_game_stats WHERE season_code = ? AND played = 1", (season_code,)
    ).fetchone()
    return row[0] if row else None


def roster_synced_at(conn: sqlite3.Connection, season_code: str) -> str | None:
    row = conn.execute("SELECT MAX(synced_at) FROM rosters WHERE season_code = ?", (season_code,)).fetchone()
    return row[0] if row else None


def replace_fantasy_pool(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Full snapshot refresh, like replace_roster - the sheet has no stable
    row ID to upsert against, and a player dropped from the real Fantasy
    pool should stop appearing rather than linger as a stale row."""
    synced_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM fantasy_pool")
    if rows:
        prepared = [{**r, "synced_at": synced_at} for r in rows]
        placeholders = ", ".join(f":{c}" for c in _FANTASY_POOL_COLUMNS)
        conn.executemany(
            f"INSERT INTO fantasy_pool ({', '.join(_FANTASY_POOL_COLUMNS)}) VALUES ({placeholders})",
            prepared,
        )
    conn.commit()
    return len(rows)


def load_fantasy_pool(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM fantasy_pool")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, record)) for record in cur.fetchall()]


def fantasy_pool_synced_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(synced_at) FROM fantasy_pool").fetchone()
    return row[0] if row else None


def replace_schedule(conn: sqlite3.Connection, season_code: str, rows: list[dict]) -> int:
    """Full snapshot refresh: `rows` (engine.data.normalize_schedule output)
    replaces whatever was previously synced for this season - a
    rescheduled game must not leave its stale date/matchup lingering
    alongside the new one, so this deletes first rather than upserting."""
    synced_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM schedule WHERE season_code = ?", (season_code,))
    if rows:
        prepared = [{**r, "played": int(bool(r["played"])), "synced_at": synced_at} for r in rows]
        placeholders = ", ".join(f":{c}" for c in _SCHEDULE_COLUMNS)
        conn.executemany(
            f"INSERT INTO schedule ({', '.join(_SCHEDULE_COLUMNS)}) VALUES ({placeholders})",
            prepared,
        )
    conn.commit()
    return len(rows)


def load_schedule(conn: sqlite3.Connection, season_code: str, round_no: int | None = None) -> list[dict]:
    query = "SELECT * FROM schedule WHERE season_code = ?"
    params: tuple = (season_code,)
    if round_no is not None:
        query += " AND round = ?"
        params = (season_code, round_no)

    cur = conn.execute(query, params)
    cols = [d[0] for d in cur.description]
    rows = []
    for record in cur.fetchall():
        row = dict(zip(cols, record))
        row["played"] = bool(row["played"])
        rows.append(row)
    return rows


def next_unplayed_round(conn: sqlite3.Connection, season_code: str) -> int | None:
    """Earliest round in the synced schedule with at least one unplayed
    game - the sensible default round for the live lineup builder, which
    exists specifically to help decide a round that hasn't happened yet."""
    row = conn.execute(
        "SELECT MIN(round) FROM schedule WHERE season_code = ? AND played = 0", (season_code,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def schedule_synced_at(conn: sqlite3.Connection, season_code: str) -> str | None:
    row = conn.execute("SELECT MAX(synced_at) FROM schedule WHERE season_code = ?", (season_code,)).fetchone()
    return row[0] if row else None


def known_player_ids(conn: sqlite3.Connection) -> set[str]:
    """Every player_id with at least one synced box-score row, across all
    seasons in the local DB - used to flag players "new to the league" (no
    EuroLeague history locally; the DB only goes back to E2023, see
    docs/technical_notes.md, so this is scoped to that window, not literally
    every EuroLeague season ever played)."""
    cur = conn.execute("SELECT DISTINCT player_id FROM player_game_stats")
    return {row[0] for row in cur.fetchall()}


def set_manual_projection(conn: sqlite3.Connection, player_id: str, player_name: str, projected_pir: float, note: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO manual_projections (player_id, player_name, projected_pir, note, set_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (player_id, player_name, projected_pir, note, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def clear_manual_projection(conn: sqlite3.Connection, player_id: str) -> bool:
    cur = conn.execute("DELETE FROM manual_projections WHERE player_id = ?", (player_id,))
    conn.commit()
    return cur.rowcount > 0


def load_manual_projections(conn: sqlite3.Connection) -> dict[str, dict]:
    """player_id -> {player_name, projected_pir, note, set_at} for every
    player with a manually-set projection override."""
    cur = conn.execute("SELECT player_id, player_name, projected_pir, note, set_at FROM manual_projections")
    return {
        row[0]: {"player_name": row[1], "projected_pir": row[2], "note": row[3], "set_at": row[4]}
        for row in cur.fetchall()
    }


def add_to_watchlist(conn: sqlite3.Connection, player_id: str, player_name: str, note: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO watchlist (player_id, player_name, note, added_at) "
        "VALUES (?, ?, ?, ?)",
        (player_id, player_name, note, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def remove_from_watchlist(conn: sqlite3.Connection, player_id: str) -> bool:
    cur = conn.execute("DELETE FROM watchlist WHERE player_id = ?", (player_id,))
    conn.commit()
    return cur.rowcount > 0


def load_watchlist(conn: sqlite3.Connection) -> dict[str, dict]:
    """player_id -> {player_name, note, added_at} for every watchlisted player."""
    cur = conn.execute("SELECT player_id, player_name, note, added_at FROM watchlist")
    return {
        row[0]: {"player_name": row[1], "note": row[2], "added_at": row[3]}
        for row in cur.fetchall()
    }


def replace_injuries(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Full snapshot refresh, like replace_fantasy_pool - the page has no
    stable row ID, and a player no longer listed (cleared, or dropped from
    the report entirely) should stop appearing rather than linger as a
    stale row."""
    synced_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM injuries")
    if rows:
        prepared = [{**r, "synced_at": synced_at} for r in rows]
        placeholders = ", ".join(f":{c}" for c in _INJURY_COLUMNS)
        conn.executemany(
            f"INSERT INTO injuries ({', '.join(_INJURY_COLUMNS)}) VALUES ({placeholders})",
            prepared,
        )
    conn.commit()
    return len(rows)


def load_injuries(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM injuries")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, record)) for record in cur.fetchall()]


def injuries_synced_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(synced_at) FROM injuries").fetchone()
    return row[0] if row else None


def all_known_players(conn: sqlite3.Connection) -> list[dict]:
    """Every distinct player_id + player_name this project has ever synced
    box-score data for, across all seasons - a fallback identity lookup for
    engine.fantasy_pool.resolve_pool_rows, for when a player is missing
    from the CURRENT season's `rosters` table (EuroLeague's own /people
    endpoint can be significantly behind for some clubs pre-season - see
    docs/testing_log.md, 2026-09-15) but already has history under a
    known player_id from a prior season."""
    cur = conn.execute("SELECT DISTINCT player_id, player_name FROM player_game_stats")
    return [{"player_id": r[0], "player_name": r[1]} for r in cur.fetchall()]


def save_tracked_lineup(
    conn: sqlite3.Connection,
    season_code: str,
    round_no: int,
    manager_id: int,
    kind: str,
    formation: str,
    players: list[dict],
) -> int:
    """Replace the (season, round, manager, kind) lineup with `players` -
    each {player_id, player_name, team, position, slot, projected_value}.
    One transaction: a failed save never leaves a half-written lineup."""
    with conn:
        old = conn.execute(
            "SELECT lineup_id FROM tracked_lineups WHERE season_code = ? AND round = ? AND manager_id = ? AND kind = ?",
            (season_code, round_no, manager_id, kind),
        ).fetchone()
        if old:
            conn.execute("DELETE FROM tracked_lineup_players WHERE lineup_id = ?", (old[0],))
            conn.execute("DELETE FROM tracked_lineups WHERE lineup_id = ?", (old[0],))
        cur = conn.execute(
            "INSERT INTO tracked_lineups (season_code, round, manager_id, kind, formation, saved_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (season_code, round_no, manager_id, kind, formation, datetime.now(timezone.utc).isoformat()),
        )
        lineup_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO tracked_lineup_players "
            "(lineup_id, player_id, player_name, team, position, slot, projected_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (lineup_id, p["player_id"], p["player_name"], p.get("team"), p.get("position"),
                 p["slot"], p.get("projected_value"))
                for p in players
            ],
        )
    return lineup_id


def load_tracked_lineups(conn: sqlite3.Connection, season_code: str, manager_id: int) -> dict[tuple[int, str], dict]:
    """(round, kind) -> {formation, saved_at, players: [row dicts]} for every
    tracked lineup of one manager in one season."""
    heads = conn.execute(
        "SELECT lineup_id, round, kind, formation, saved_at FROM tracked_lineups "
        "WHERE season_code = ? AND manager_id = ?",
        (season_code, manager_id),
    ).fetchall()
    cols = ("player_id", "player_name", "team", "position", "slot", "projected_value")
    result: dict[tuple[int, str], dict] = {}
    for lineup_id, round_no, kind, formation, saved_at in heads:
        players = conn.execute(
            f"SELECT {', '.join(cols)} FROM tracked_lineup_players WHERE lineup_id = ?",
            (lineup_id,),
        ).fetchall()
        result[(round_no, kind)] = {
            "formation": formation,
            "saved_at": saved_at,
            "players": [dict(zip(cols, p)) for p in players],
        }
    return result


def set_official_points(
    conn: sqlite3.Connection, season_code: str, round_no: int, manager_id: int, official_points: float | None
) -> None:
    conn.execute(
        "INSERT INTO tracked_rounds (season_code, round, manager_id, official_points, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (season_code, round, manager_id) DO UPDATE SET "
        "official_points = excluded.official_points, updated_at = excluded.updated_at",
        (season_code, round_no, manager_id, official_points, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def load_tracked_rounds(conn: sqlite3.Connection, season_code: str, manager_id: int) -> dict[int, dict]:
    """round -> {official_points, notes} for one manager in one season."""
    cur = conn.execute(
        "SELECT round, official_points, notes FROM tracked_rounds WHERE season_code = ? AND manager_id = ?",
        (season_code, manager_id),
    )
    return {row[0]: {"official_points": row[1], "notes": row[2]} for row in cur.fetchall()}


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
