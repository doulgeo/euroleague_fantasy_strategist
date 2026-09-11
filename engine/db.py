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


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_ROUND_INDEX_SQL)
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
