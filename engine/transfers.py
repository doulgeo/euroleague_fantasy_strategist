"""
Naive transfer suggestions: for each rostered player, is there a clearly
better same-position player not on the roster?

This is a stand-in for the real feature. It treats "everyone in the fetched
player pool not on the roster" as available, which is only true in this POC -
the real app needs actual 12-manager ownership data (who owns whom) before
this can tell you what's genuinely available to pick up.
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


def suggest_transfers(
    roster: Roster,
    pool: dict[str, Projection],
    min_gap: float = MIN_UPGRADE_GAP,
) -> list[TransferSuggestion]:
    rostered_ids = roster.player_ids()
    suggestions: list[TransferSuggestion] = []

    for player in roster.players:
        same_position_free_agents = [
            p for p in pool.values() if p.position == player.position and p.player_id not in rostered_ids
        ]
        if not same_position_free_agents:
            continue

        best_available = max(same_position_free_agents, key=lambda p: p.projected_pir_with_bonus)
        gain = best_available.projected_pir_with_bonus - player.projected_pir_with_bonus

        if gain >= min_gap:
            suggestions.append(
                TransferSuggestion(drop=player, add=best_available, projected_gain=gain)
            )

    suggestions.sort(key=lambda s: s.projected_gain, reverse=True)
    return suggestions
