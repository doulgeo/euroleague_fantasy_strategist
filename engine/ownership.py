"""
Manual draft/ownership/transaction tracking for the 12-manager league.

Real fantasy managers own real players; this replaces the POC's "free agent
= pool minus my own sampled roster" stand-in (engine.transfers) with actual
per-manager ownership the user logs by hand (there's no live feed of the
other 11 managers' rosters/trades - see docs/game_rules.md).

Two tables, kept in sync on every write:
  - `ownership`: materialized current state, one row per currently-owned
    player (player_id is the primary key, so a duplicate draft/add attempt
    fails fast with an IntegrityError this module turns into a ValueError).
    Absence of a row means free agent.
  - `transactions`: an append-only log, one row per player movement. A
    1-for-1 trade is two rows sharing a `group_id` so the log stays a
    uniform shape (draft/add/drop/trade all look the same) while a
    transaction-log UI can still group a trade's two legs for display.

Every write below wraps its `ownership` update and `transactions` insert in
one DB transaction (`with conn:`), so the two tables can never drift apart
from a partial write.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone

from engine.roster import TOTAL_ROSTER_SIZE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_manager_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT manager_id FROM managers WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def get_or_create_manager(conn: sqlite3.Connection, name: str) -> tuple[int, bool]:
    """Case-insensitive get-or-create by name. Returns (manager_id, created).

    The draft-import path uses this to build the league's manager list
    straight from whatever distinct names appear in an uploaded CSV, rather
    than requiring managers to be pre-seeded and mapped by hand - however
    many managers the file has (2, 14, whatever) is however many get
    created, matching an existing manager by name (case-insensitively) if
    one's already there.
    """
    name = name.strip()
    row = conn.execute(
        "SELECT manager_id FROM managers WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row:
        return row[0], False
    with conn:
        cur = conn.execute("INSERT INTO managers (name) VALUES (?)", (name,))
    return cur.lastrowid, True


def _natural_sort_key(name: str) -> list:
    """Splits a name into text/number chunks so e.g. "Test2" sorts before
    "Test10" - plain SQL/string ordering treats them character-by-character
    ("1" < "2") and gets this backwards.
    """
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", name)]


def list_managers(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT manager_id, name FROM managers")
    rows = [{"manager_id": r[0], "name": r[1]} for r in cur.fetchall()]
    rows.sort(key=lambda m: _natural_sort_key(m["name"]))
    return rows


def clear_all_ownership(conn: sqlite3.Connection) -> int:
    """Wipes every manager's roster back to free agency - a real DELETE, not
    a drop-by-drop log entry, same dev-reset spirit as engine.dev_draft's
    randomize_draft (which does this same delete before re-drafting).
    Doesn't touch `transactions` - that log stays a record of what actually
    happened, not of current state. Returns the number of players freed.
    """
    cur = conn.execute("DELETE FROM ownership")
    conn.commit()
    return cur.rowcount


def clear_all_managers(conn: sqlite3.Connection) -> int:
    """Full league reset: deletes every row in `managers`, plus `ownership`
    and `transactions` (both reference manager_id, and there's no real
    "history" left to preserve once the managers themselves are gone - unlike
    clear_all_ownership, which keeps `transactions` because the managers
    still exist). Dev/testing reset, same spirit as clear_all_ownership and
    engine.dev_draft.randomize_draft - not something the real league ever
    does mid-season. Re-seed with seed_league.py afterward. Returns the
    number of managers removed.
    """
    with conn:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM ownership")
        cur = conn.execute("DELETE FROM managers")
    return cur.rowcount


def current_owner(conn: sqlite3.Connection, player_id: str) -> int | None:
    row = conn.execute("SELECT manager_id FROM ownership WHERE player_id = ?", (player_id,)).fetchone()
    return row[0] if row else None


def manager_roster_ids(conn: sqlite3.Connection, manager_id: int) -> set[str]:
    cur = conn.execute("SELECT player_id FROM ownership WHERE manager_id = ?", (manager_id,))
    return {r[0] for r in cur.fetchall()}


def _check_roster_not_full(conn: sqlite3.Connection, manager_id: int) -> None:
    size = len(manager_roster_ids(conn, manager_id))
    if size >= TOTAL_ROSTER_SIZE:
        raise ValueError(
            f"Manager {manager_id} already has a full roster "
            f"({size}/{TOTAL_ROSTER_SIZE}) - drop or trade a player first"
        )


def all_owned_ids(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT player_id FROM ownership")
    return {r[0] for r in cur.fetchall()}


def free_agent_ids(conn: sqlite3.Connection, universe_ids: set[str]) -> set[str]:
    return universe_ids - all_owned_ids(conn)


def transaction_history(
    conn: sqlite3.Connection,
    player_id: str | None = None,
    manager_id: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    query = "SELECT * FROM transactions"
    clauses = []
    params: list = []
    if player_id is not None:
        clauses.append("player_id = ?")
        params.append(player_id)
    if manager_id is not None:
        clauses.append("(from_manager_id = ? OR to_manager_id = ?)")
        params.extend([manager_id, manager_id])
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY transaction_id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    cur = conn.execute(query, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _insert_transaction(
    conn: sqlite3.Connection,
    *,
    round_: int | None,
    type_: str,
    player_id: str,
    from_manager_id: int | None,
    to_manager_id: int | None,
    group_id: str | None,
    notes: str | None,
) -> None:
    conn.execute(
        "INSERT INTO transactions "
        "(created_at, round, type, player_id, from_manager_id, to_manager_id, group_id, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_now_iso(), round_, type_, player_id, from_manager_id, to_manager_id, group_id, notes),
    )


def record_draft_pick(
    conn: sqlite3.Connection,
    player_id: str,
    manager_id: int,
    round_: int | None = None,
    notes: str | None = None,
) -> None:
    _check_roster_not_full(conn, manager_id)
    try:
        with conn:
            conn.execute(
                "INSERT INTO ownership (player_id, manager_id, acquired_at, acquired_via) "
                "VALUES (?, ?, ?, 'draft')",
                (player_id, manager_id, _now_iso()),
            )
            _insert_transaction(
                conn, round_=round_, type_="draft", player_id=player_id,
                from_manager_id=None, to_manager_id=manager_id, group_id=None, notes=notes,
            )
    except sqlite3.IntegrityError:
        owner_id = current_owner(conn, player_id)
        raise ValueError(f"Player {player_id!r} is already owned by manager {owner_id}") from None


def record_free_agent_add(
    conn: sqlite3.Connection,
    player_id: str,
    manager_id: int,
    round_: int | None = None,
    notes: str | None = None,
) -> None:
    _check_roster_not_full(conn, manager_id)
    try:
        with conn:
            conn.execute(
                "INSERT INTO ownership (player_id, manager_id, acquired_at, acquired_via) "
                "VALUES (?, ?, ?, 'free_agent_add')",
                (player_id, manager_id, _now_iso()),
            )
            _insert_transaction(
                conn, round_=round_, type_="add", player_id=player_id,
                from_manager_id=None, to_manager_id=manager_id, group_id=None, notes=notes,
            )
    except sqlite3.IntegrityError:
        owner_id = current_owner(conn, player_id)
        raise ValueError(f"Player {player_id!r} is already owned by manager {owner_id}") from None


def record_drop(
    conn: sqlite3.Connection,
    player_id: str,
    round_: int | None = None,
    notes: str | None = None,
) -> None:
    owner_id = current_owner(conn, player_id)
    if owner_id is None:
        raise ValueError(f"Player {player_id!r} is not currently owned - nothing to drop")

    with conn:
        conn.execute("DELETE FROM ownership WHERE player_id = ?", (player_id,))
        _insert_transaction(
            conn, round_=round_, type_="drop", player_id=player_id,
            from_manager_id=owner_id, to_manager_id=None, group_id=None, notes=notes,
        )


def record_trade(
    conn: sqlite3.Connection,
    player_a_id: str,
    manager_a_id: int,
    player_b_id: str,
    manager_b_id: int,
    round_: int | None = None,
    notes: str | None = None,
) -> None:
    """Strictly 1-for-1: player_a (currently owned by manager_a) moves to
    manager_b, and player_b (currently owned by manager_b) moves to
    manager_a. Both players must already be owned by the stated manager -
    use record_free_agent_add for a player coming from the free-agent pool.
    """
    owner_a = current_owner(conn, player_a_id)
    if owner_a != manager_a_id:
        raise ValueError(f"Player {player_a_id!r} is not currently owned by manager {manager_a_id} (owner: {owner_a})")
    owner_b = current_owner(conn, player_b_id)
    if owner_b != manager_b_id:
        raise ValueError(f"Player {player_b_id!r} is not currently owned by manager {manager_b_id} (owner: {owner_b})")

    group_id = uuid.uuid4().hex
    with conn:
        conn.execute(
            "UPDATE ownership SET manager_id = ?, acquired_at = ?, acquired_via = 'trade' WHERE player_id = ?",
            (manager_b_id, _now_iso(), player_a_id),
        )
        conn.execute(
            "UPDATE ownership SET manager_id = ?, acquired_at = ?, acquired_via = 'trade' WHERE player_id = ?",
            (manager_a_id, _now_iso(), player_b_id),
        )
        _insert_transaction(
            conn, round_=round_, type_="trade", player_id=player_a_id,
            from_manager_id=manager_a_id, to_manager_id=manager_b_id, group_id=group_id, notes=notes,
        )
        _insert_transaction(
            conn, round_=round_, type_="trade", player_id=player_b_id,
            from_manager_id=manager_b_id, to_manager_id=manager_a_id, group_id=group_id, notes=notes,
        )
