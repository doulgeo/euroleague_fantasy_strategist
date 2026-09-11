"""
Starting five/sixth-man/bench selection and day-aware swap logic.

Confirmed rules (this supersedes an earlier, simpler starter/bench-only
model that was wrong on several points):

- Each round you pick an ActiveSquad of 10 from your 13-player roster,
  leaving exactly 3 excluded - those 3 score zero and can never be swapped
  in that round (see engine.roster.ActiveSquad).
- Within the active 10, there's a three-tier scoring hierarchy:
    - 5 starters, full points, one of them captain (2x).
    - 1 "sixth man", also full points, but not captain-eligible.
    - 4 bench players, HALF points - they score automatically at that rate
      even if never swapped in, they don't need to be activated to count.
- The starting five must be one of exactly three valid formations (Guard,
  Forward, Center): (2,2,1), (2,1,2), (3,1,1) - confirmed by the user
  (minimum 2 Guards; Forward and Center each capped at 2, floored at 1).
- A round spans 1-2 match days. Whatever a player earns while occupying a
  full-scoring slot (starter or sixth-man) is banked permanently - so the
  golden rule is still to fill those 6 full-scoring slots with day-1 players
  first, then once day-1 is over, swap any now-finished slot for the best
  same-position bench player who plays later. This is a straight upgrade
  for that bench player too: left alone they'd only score at half rate for
  their game, promoted into a full-scoring slot they score at full rate.

Both the pre-round recommendation and the post-hoc "best possible" backtest
benchmark reuse the exact same selection logic - they only differ in
whether the ranking uses projections (decision time, foresight only) or
actual results (hindsight, for benchmarking the heuristic).

Assumptions made where the rules weren't fully pinned down (flagged here so
they're easy to revisit):
- The sixth-man slot has a position "type" fixed at whoever holds it (like
  any starter slot) - a swap into it must match that position, same as a
  starter slot. ("the 6th player is just like any other player, he can be
  swapped into a started position or a fully benched position" was taken to
  mean the *role* moves between players, not that it's position-agnostic.)
- The starting formation shape is chosen once at the initial (day-1) lock
  and is not reshuffled at the swap window - only *who* fills an
  already-typed slot can change, not the (G,F,C) shape itself.
- Captain can only be one of the 5 starters, never the sixth man (matches
  how the user described it: "5 players are starters ... 1 of them is the
  captain ... then there is a 6th player").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from engine.projections import Projection
from engine.roster import ActiveSquad

VALID_FORMATIONS: list[tuple[int, int, int]] = [(2, 2, 1), (2, 1, 2), (3, 1, 1)]  # (Guard, Forward, Center)
BENCH_SCORE_MULTIPLIER = 0.5

ValueFn = Callable[[str], float]


@dataclass
class Lineup:
    starters: list[Projection]  # exactly 5, shape per one of VALID_FORMATIONS
    sixth_man: Projection
    bench: list[Projection]  # exactly 4, score at BENCH_SCORE_MULTIPLIER
    captain: Projection  # one of `starters`


def _date_only(iso_str: str | None) -> str | None:
    if not iso_str:
        return None
    return iso_str.split("T")[0]


def team_dates_for_round(rows: list[dict], round_no: int) -> dict[str, str]:
    """team_code -> date (YYYY-MM-DD) they play in the given round.

    Built from the flat player-game rows rather than a separate games list -
    every team appears at least once per round it plays in.
    """
    dates: dict[str, str] = {}
    for r in rows:
        if r.get("round") != round_no:
            continue
        date = _date_only(r.get("game_date"))
        if date and r.get("team") not in dates:
            dates[r["team"]] = date
    return dates


def _availability_rank(player: Projection, team_dates: dict[str, str], min_date: str | None) -> int:
    """0 = plays on the round's earliest date (day 1), 1 = plays later, 2 = not playing this round."""
    date = team_dates.get(player.team)
    if date is None:
        return 2
    return 0 if date == min_date else 1


def _min_date(team_dates: dict[str, str]) -> str | None:
    return min(team_dates.values()) if team_dates else None


def _all_players(lineup: Lineup) -> list[Projection]:
    return list(lineup.starters) + [lineup.sixth_man] + list(lineup.bench)


def _tier_multiplier(player: Projection, lineup: Lineup) -> float:
    if player.player_id == lineup.captain.player_id:
        return 2.0
    starter_ids = {p.player_id for p in lineup.starters}
    if player.player_id in starter_ids or player.player_id == lineup.sixth_man.player_id:
        return 1.0
    return BENCH_SCORE_MULTIPLIER


def _build_formation_starters(
    active_squad: ActiveSquad,
    formation: tuple[int, int, int],
    team_dates: dict[str, str],
    value_fn: ValueFn,
) -> list[Projection] | None:
    guards, forwards, centers = formation
    requirements = [("Guard", guards), ("Forward", forwards), ("Center", centers)]
    min_date = _min_date(team_dates)
    starters: list[Projection] = []

    for position, count in requirements:
        candidates = active_squad.by_position(position)
        if len(candidates) < count:
            return None  # this formation isn't feasible with this active squad

        ranked = sorted(
            candidates,
            key=lambda p: (_availability_rank(p, team_dates, min_date), -value_fn(p.player_id)),
        )
        starters.extend(ranked[:count])

    return starters


def build_lineup(active_squad: ActiveSquad, team_dates: dict[str, str], value_fn: ValueFn) -> Lineup:
    """Choose the best of the three valid formations, fill it preferring
    day-1-eligible players first (the golden rule), then by value_fn
    descending. Sixth man = best remaining player by value_fn (any
    position). Captain = highest value_fn among the five starters.
    """
    min_date = _min_date(team_dates)
    best_starters: list[Projection] | None = None
    best_key: tuple[int, float] | None = None

    for formation in VALID_FORMATIONS:
        starters = _build_formation_starters(active_squad, formation, team_dates, value_fn)
        if starters is None:
            continue

        day1_count = sum(1 for p in starters if _availability_rank(p, team_dates, min_date) == 0)
        total_value = sum(value_fn(p.player_id) for p in starters)
        key = (day1_count, total_value)

        if best_key is None or key > best_key:
            best_key = key
            best_starters = starters

    if best_starters is None:
        raise RuntimeError("No valid formation (2-2-1 / 2-1-2 / 3-1-1) could be filled from this active squad")

    starter_ids = {p.player_id for p in best_starters}
    remaining = [p for p in active_squad.active if p.player_id not in starter_ids]
    sixth_man = max(remaining, key=lambda p: value_fn(p.player_id))
    bench = [p for p in remaining if p.player_id != sixth_man.player_id]
    captain = max(best_starters, key=lambda p: value_fn(p.player_id))

    return Lineup(starters=best_starters, sixth_man=sixth_man, bench=bench, captain=captain)


def swap_after_day1(lineup: Lineup, team_dates: dict[str, str], value_fn: ValueFn) -> Lineup:
    """For every full-scoring slot (starter or sixth-man) whose occupant
    already played day 1 (banked regardless), swap in the best same-position
    bench player who plays later, if that's an improvement (value_fn > 0) -
    promoting them from half points to full points for their still-upcoming
    game. Captain is recomputed over the resulting five starters afterward
    (sixth man is never captain-eligible).
    """
    min_date = _min_date(team_dates)
    full_slots = list(lineup.starters) + [lineup.sixth_man]
    bench = list(lineup.bench)

    for i, occupant in enumerate(full_slots):
        if _availability_rank(occupant, team_dates, min_date) != 0:
            continue  # only touch slots whose current occupant already played

        same_position_pending = [
            p
            for p in bench
            if p.position == occupant.position and _availability_rank(p, team_dates, min_date) == 1
        ]
        if not same_position_pending:
            continue

        best_bench = max(same_position_pending, key=lambda p: value_fn(p.player_id))
        if value_fn(best_bench.player_id) <= 0:
            continue  # not an improvement over leaving them at half points

        full_slots[i] = best_bench
        bench.remove(best_bench)
        bench.append(occupant)

    new_starters = full_slots[:5]
    new_sixth_man = full_slots[5]
    captain = max(new_starters, key=lambda p: value_fn(p.player_id))

    return Lineup(starters=new_starters, sixth_man=new_sixth_man, bench=bench, captain=captain)


def compute_round_score(
    initial: Lineup,
    final: Lineup,
    team_dates: dict[str, str],
    actual_pir: dict[str, float],
) -> float:
    """Round score across both match days, given real (or backtest
    ground-truth) PIR per player.

    A full-scoring-slot player's points are banked when their game happens,
    regardless of any later swap - so day-1 scoring comes from `initial`
    (whoever occupied which tier when those games were actually played),
    and day-2/later scoring comes from `final` (whoever ends up where once
    the swap window has closed). Bench players score at
    BENCH_SCORE_MULTIPLIER whether or not they were ever swapped in - that's
    automatic, not conditional on being promoted. Captain doubling is per
    stage: `initial.captain` for day-1 scoring, `final.captain` for day-2
    scoring, so reassigning captain at the swap window only affects points
    not yet banked.
    """
    min_date = _min_date(team_dates)
    total = 0.0

    for p in _all_players(initial):
        if _availability_rank(p, team_dates, min_date) != 0:
            continue  # didn't play day 1
        total += actual_pir.get(p.player_id, 0.0) * _tier_multiplier(p, initial)

    for p in _all_players(final):
        if _availability_rank(p, team_dates, min_date) != 1:
            continue  # only later-playing slots score here (day-1 handled above)
        total += actual_pir.get(p.player_id, 0.0) * _tier_multiplier(p, final)

    return total


def availability_label(player: Projection, team_dates: dict[str, str]) -> str:
    min_date = _min_date(team_dates)
    rank = _availability_rank(player, team_dates, min_date)
    return {0: "day1", 1: "later", 2: "not playing"}[rank]
