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


def known_player_ids(conn: sqlite3.Connection) -> set[str]:
    """Every player_id with at least one synced box-score row, across all
    seasons in the local DB - used to flag players "new to the league" (no
    EuroLeague history locally; the DB only goes back to E2023, see
    docs/technical_notes.md, so this is scoped to that window, not literally
    every EuroLeague season ever played)."""
    cur = conn.execute("SELECT DISTINCT player_id FROM player_game_stats")
    return {row[0] for row in cur.fetchall()}
