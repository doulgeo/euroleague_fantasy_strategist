"""
Dev-only tool: wipe all current ownership and re-draft every seeded manager
a full, valid, exclusive random roster - so there's always a realistic
12-manager league state to develop/test the lineup builder, transfers, etc.
against, without hand-drafting 156 players every time.

NOT for the real draft. Triggered from the app's /sync page behind an
explicit warning (app.py's dev_randomize_draft route) - same spirit as
engine.roster's sample_roster (weighted-random, same helper reused here)
but for real managers/real ownership tracking instead of a throwaway
in-memory Roster.
"""

from __future__ import annotations

import random
import sqlite3

from engine import ownership
from engine.projections import Projection
from engine.roster import REQUIRED_COUNTS, _weighted_sample_without_replacement


def randomize_draft(
    conn: sqlite3.Connection,
    pool: dict[str, Projection],
    manager_ids: list[int],
    rng: random.Random | None = None,
) -> dict[int, int]:
    """Clears `ownership` (a real DELETE, not a drop-by-drop log entry -
    this is a dev reset, not something that happened in the real league) and
    re-drafts every manager in `manager_ids` a full 13-player roster
    (5 Guard/5 Forward/3 Center), weighted-random by projected value within
    each position, exclusive across managers (once picked, gone from the
    pool for the rest of the draft). Each pick is still logged as a normal
    "draft" transaction via engine.ownership, same as a real pick, so the
    history stays honest about what a manager owns and why.

    `pool` should already exclude "gone" players (see engine.rosters -
    they're not really draftable) - callers pass whatever they'd otherwise
    show on the real draft board.

    Returns {manager_id: players_drafted} - always 13 each on success;
    raises RuntimeError if the pool runs out of a position before every
    manager has a full roster (extremely unlikely at real pool sizes, but
    not impossible with a tiny/filtered pool).
    """
    rng = rng or random.Random()

    conn.execute("DELETE FROM ownership")
    conn.commit()

    by_position: dict[str, list[Projection]] = {position: [] for position in REQUIRED_COUNTS}
    for p in pool.values():
        if p.position in by_position:
            by_position[p.position].append(p)

    draft_order = list(manager_ids)
    rng.shuffle(draft_order)

    counts = {manager_id: 0 for manager_id in manager_ids}

    for manager_id in draft_order:
        for position, count in REQUIRED_COUNTS.items():
            candidates = by_position[position]
            if len(candidates) < count:
                raise RuntimeError(
                    f"Ran out of {position}s in the pool ({len(candidates)} left, need {count}) "
                    f"before every manager had a full roster - pool too small for this many managers"
                )

            picks = _weighted_sample_without_replacement(candidates, count, rng)
            picked_ids = {p.player_id for p in picks}
            by_position[position] = [p for p in candidates if p.player_id not in picked_ids]

            for p in picks:
                ownership.record_draft_pick(conn, p.player_id, manager_id, notes="randomized dev draft")
                counts[manager_id] += 1

    return counts
