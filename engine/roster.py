"""
Roster model: a 13-player fantasy squad (5 Guard / 5 Forward / 3 Center, no
head coach - draft mode), plus the per-round active-squad selection (10 of
the 13, 3 excluded), plus POC-only samplers for testing the engine without
real draft/ownership data.

Confirmed by the user (superseding an earlier, wrong 10-player/4-4-2 belief
that was based on a misreading of the general rules doc): the real
draft-mode roster is 13 players, and only 10 of them are "active" any given
round.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from engine.projections import Projection

REQUIRED_COUNTS = {"Guard": 5, "Forward": 5, "Center": 3}
TOTAL_ROSTER_SIZE = sum(REQUIRED_COUNTS.values())  # 13

DRAFT_POOL_SIZE = 100

ACTIVE_SQUAD_SIZE = 10
EXCLUDED_COUNT = 3

# An active squad must retain enough of each position to field at least one
# valid starting formation (see engine.lineup.VALID_FORMATIONS: the minimum
# across all three valid formations is 2 Guards, 1 Forward, 1 Center).
MIN_ACTIVE_COUNTS = {"Guard": 2, "Forward": 1, "Center": 1}


@dataclass
class Roster:
    """The full 13-player squad a manager drafted. Doesn't change round to
    round (only via rare trades) - see ActiveSquad for the per-round subset.
    """

    players: list[Projection]

    def __post_init__(self) -> None:
        counts: dict[str, int] = {}
        for p in self.players:
            counts[p.position] = counts.get(p.position, 0) + 1

        if len(self.players) != TOTAL_ROSTER_SIZE:
            raise ValueError(
                f"Roster must have exactly {TOTAL_ROSTER_SIZE} players, got {len(self.players)}"
            )

        for position, required in REQUIRED_COUNTS.items():
            if counts.get(position, 0) != required:
                raise ValueError(
                    f"Roster must have {required} {position}s, got {counts.get(position, 0)} "
                    f"(full breakdown: {counts})"
                )

    def by_position(self, position: str) -> list[Projection]:
        return [p for p in self.players if p.position == position]

    def player_ids(self) -> set[str]:
        return {p.player_id for p in self.players}


@dataclass
class ActiveSquad:
    """The 10 of 13 players selected 'in' for a given round. The other 3 are
    excluded: they score zero and cannot be swapped in during that round at
    all (not the same thing as being on the half-point bench).
    """

    active: list[Projection]
    excluded: list[Projection]

    def __post_init__(self) -> None:
        if len(self.active) != ACTIVE_SQUAD_SIZE:
            raise ValueError(f"Active squad must have exactly {ACTIVE_SQUAD_SIZE} players, got {len(self.active)}")
        if len(self.excluded) != EXCLUDED_COUNT:
            raise ValueError(f"Must exclude exactly {EXCLUDED_COUNT} players, got {len(self.excluded)}")

        counts: dict[str, int] = {}
        for p in self.active:
            counts[p.position] = counts.get(p.position, 0) + 1

        for position, minimum in MIN_ACTIVE_COUNTS.items():
            if counts.get(position, 0) < minimum:
                raise ValueError(
                    f"Active squad has only {counts.get(position, 0)} {position}s, needs at least "
                    f"{minimum} to field any valid starting formation (full breakdown: {counts})"
                )

    def by_position(self, position: str) -> list[Projection]:
        return [p for p in self.active if p.position == position]


def _weighted_sample_without_replacement(
    candidates: list[Projection], k: int, rng: random.Random
) -> list[Projection]:
    if len(candidates) < k:
        raise ValueError(f"Not enough candidates ({len(candidates)}) to sample {k}")

    pool = list(candidates)
    chosen: list[Projection] = []

    for _ in range(k):
        min_val = min(p.projected_pir_with_bonus for p in pool)
        # shift so every weight is strictly positive, even with negative projections
        weights = [p.projected_pir_with_bonus - min_val + 1.0 for p in pool]
        pick = rng.choices(pool, weights=weights, k=1)[0]
        chosen.append(pick)
        pool.remove(pick)

    return chosen


def build_draft_pool(
    projections: dict[str, Projection], pool_size: int = DRAFT_POOL_SIZE
) -> dict[str, Projection]:
    """Restrict the sampling universe to players a real manager would plausibly
    draft, instead of a uniform random slice of the entire league.

    Draft-worthy here means productive (high projected PIR) and heavily used
    (high minutes trend) - a proxy for "team leader" without needing a
    separate usage-rate stat. Team win-rate is deliberately NOT part of this
    ranking: the user's own draft criteria don't include "plays for a winning
    team", even though win-rate does feed the scoring bonus elsewhere
    (engine.projections) - drafting and in-round scoring are different
    questions.

    Ranked within each position separately, in proportion to the 5G/5F/3C
    roster shape, so a scarce position (3 Centers needed) isn't crowded out
    of the pool by a more numerous one - a flat top-100-overall cut would
    likely be Guard-heavy just because there are more high-minute Guards
    league-wide. Within a position, ranks by a 50/50 composite of min-max
    normalized projected PIR and minutes trend (equal weight: a high-minutes
    non-producer and a high-PIR bit-part player are both a bad proxy for
    "team leader" on their own).
    """
    total_slots = sum(REQUIRED_COUNTS.values())
    pool: dict[str, Projection] = {}

    for position, required in REQUIRED_COUNTS.items():
        candidates = [p for p in projections.values() if p.position == position]
        if not candidates:
            continue
        target = max(required, round(pool_size * required / total_slots))

        pirs = [p.projected_pir_with_bonus for p in candidates]
        minutes = [p.minutes_trend_seconds for p in candidates]
        pir_lo, pir_hi = min(pirs), max(pirs)
        min_lo, min_hi = min(minutes), max(minutes)

        def norm(v: float, lo: float, hi: float) -> float:
            return (v - lo) / (hi - lo) if hi > lo else 0.5

        def composite(p: Projection) -> float:
            return (
                0.5 * norm(p.projected_pir_with_bonus, pir_lo, pir_hi)
                + 0.5 * norm(p.minutes_trend_seconds, min_lo, min_hi)
            )

        ranked = sorted(candidates, key=composite, reverse=True)
        for p in ranked[:target]:
            pool[p.player_id] = p

    return pool


def sample_roster(pool: dict[str, Projection], rng: random.Random | None = None) -> Roster:
    """Build one plausible 5-5-3 roster from a projection pool.

    Weighted-random by projected_pir_with_bonus within each position bucket,
    so the sample favors decent players without being trivially "top 13 in
    the league" every time. POC-only: real rosters come from the league's
    actual draft/ownership data, not this.
    """
    rng = rng or random.Random()

    players: list[Projection] = []
    for position, count in REQUIRED_COUNTS.items():
        candidates = [p for p in pool.values() if p.position == position]
        players.extend(_weighted_sample_without_replacement(candidates, count, rng))

    return Roster(players=players)


def sample_active_squad(roster: Roster, rng: random.Random | None = None, max_attempts: int = 50) -> ActiveSquad:
    """POC-only: pick a valid random exclusion of 3 from the 13-player roster.

    Weighted toward excluding lower-projected players (a real manager
    wouldn't randomly benc their stars), retrying if the random draw would
    leave too few of some position to field any valid formation.
    """
    rng = rng or random.Random()

    for _ in range(max_attempts):
        pool = list(roster.players)
        excluded: list[Projection] = []
        for _ in range(EXCLUDED_COUNT):
            max_val = max(p.projected_pir_with_bonus for p in pool)
            # shift+invert so lower-projected players are more likely to be excluded
            weights = [max_val - p.projected_pir_with_bonus + 1.0 for p in pool]
            pick = rng.choices(pool, weights=weights, k=1)[0]
            excluded.append(pick)
            pool.remove(pick)

        active = [p for p in roster.players if p not in excluded]
        try:
            return ActiveSquad(active=active, excluded=excluded)
        except ValueError:
            continue

    raise RuntimeError(f"Could not find a valid active-squad exclusion after {max_attempts} attempts")
