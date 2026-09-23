"""
Naive transfer suggestions: for each rostered player, is there a clearly
better same-position player not on the roster?

Pass `owned_ids` (e.g. engine.ownership.all_owned_ids(conn)) to treat only
players nobody in the league owns as genuinely available; omitting it falls
back to the original POC behavior (everyone in the pool not on this roster),
which is what poc_run.py still uses since it has no real ownership data.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.projections import Projection
from engine.roster import Roster

MIN_UPGRADE_GAP = 3.0  # projected_pir_with_bonus points before suggesting a swap


@dataclass
class TransferSuggestion:
    drop: Projection
    add: Projection
    projected_gain: float


def projected_gain(drop: Projection, add: Projection) -> float:
    """Projected PIR delta of dropping `drop` for `add` - positive is an upgrade."""
    return add.projected_pir_with_bonus - drop.projected_pir_with_bonus


def suggest_transfers(
    roster: Roster,
    pool: dict[str, Projection],
    owned_ids: set[str] | None = None,
    min_gap: float = MIN_UPGRADE_GAP,
) -> list[TransferSuggestion]:
    excluded_ids = owned_ids if owned_ids is not None else roster.player_ids()

    pool_by_position: dict[str, list[Projection]] = {}
    for p in pool.values():
        if p.player_id not in excluded_ids:
            pool_by_position.setdefault(p.position, []).append(p)
    for candidates in pool_by_position.values():
        candidates.sort(key=lambda p: p.projected_pir_with_bonus, reverse=True)

    suggestions: list[TransferSuggestion] = []

    for player in roster.players:
        candidates = pool_by_position.get(player.position)
        if not candidates:
            continue

        best_available = candidates[0]
        gain = projected_gain(player, best_available)

        if gain >= min_gap:
            suggestions.append(
                TransferSuggestion(drop=player, add=best_available, projected_gain=gain)
            )

    suggestions.sort(key=lambda s: s.projected_gain, reverse=True)
    return suggestions
