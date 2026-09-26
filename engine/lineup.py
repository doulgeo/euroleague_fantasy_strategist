"""
Starting five/sixth-man/bench selection and day-aware swap logic.

Confirmed rules (this supersedes an earlier, simpler starter/bench-only
model that was wrong on several points):

- Each round you pick an ActiveSquad of 10 from your 13-player roster,
  leaving exactly 3 excluded - those 3 score zero and can never be swapped
  in that round (see engine.roster.ActiveSquad).
- Within the active 10, there's a three-tier scoring hierarchy:
    - 5 starters, full points, one of them captain (1.5x, corrected
      2026-09-22 - was previously 2x).
    - 1 "sixth man", also full points, but not captain-eligible.
    - 4 bench players, HALF points - they score automatically at that rate
      even if never swapped in, they don't need to be activated to count.
- The starting five must be one of exactly three valid formations (Guard,
  Forward, Center): (2,2,1), (2,1,2), (3,1,1) - confirmed by the user
  (minimum 2 Guards; Forward and Center each capped at 2, floored at 1).
- A round spans 1-2 match days. **Corrected 2026-09-16**: an earlier
  assumption here - that a full-scoring slot's day-1 points were "banked
  permanently," untouchable by a later swap - was wrong. The user confirmed
  (against both the official rules and their own live experience) that
  moving an already-played full-scoring-slot player DOWN to the bench
  HALVES their score, exactly like it would have if they'd started on the
  bench all along. So the swap window isn't a free upgrade: demoting a
  slot's current occupant only pays off if the incoming replacement
  outscores them - a straight value comparison, not "any positive value
  beats leaving them on the bench" (see swap_after_day1). The golden rule
  is still to fill your 6 full-scoring slots with day-1 players first at
  the initial lock (see build_lineup) - before day-1 games happen you can't
  yet know who'll turn out best, so reserving those slots for your best
  day-1-projected players preserves the option to either keep or demote
  them once actual results (or, pre-round, updated projections) are in.
- Formation can also be changed at the swap window (also confirmed
  2026-09-16, superseding an earlier "formation is locked at day-1"
  assumption below) - swap_after_day1 does a full re-solve across all
  three valid formations, not just a same-position patch to the day-1
  lineup.

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
- Captain can only be one of the 5 starters, never the sixth man (matches
  how the user described it: "5 players are starters ... 1 of them is the
  captain ... then there is a 6th player"). Per the 2026-09-16 correction
  above, captain doubling - like every other tier - is governed entirely by
  the FINAL lineup: there's no separate "day-1 captain's points stay
  doubled regardless" carve-out (an earlier assumption, now also
  superseded) - a demoted former captain loses the captain multiplier along
  with their full-rate slot, same as anyone else who gets demoted.

Two ways to get the team_dates dict every function below needs:
team_dates_for_round (from played box scores - historical/backtest use)
and team_dates_from_schedule (from the schedule table - the only one that
works for a round that hasn't been played yet, which is what the live
/lineup route in app.py needs). Both produce the same dict[str, str] shape.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable

from engine.projections import Projection
from engine.roster import ActiveSquad, Roster

VALID_FORMATIONS: list[tuple[int, int, int]] = [(2, 2, 1), (2, 1, 2), (3, 1, 1)]  # (Guard, Forward, Center)
BENCH_SCORE_MULTIPLIER = 0.5
CAPTAIN_SCORE_MULTIPLIER = 1.5

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


def team_dates_from_schedule(schedule_rows: list[dict], round_no: int) -> dict[str, str]:
    """team_code -> date (YYYY-MM-DD) for the given round, built from
    engine.db's `schedule` table (engine.db.load_schedule) instead of
    played box-score rows.

    The future-round sibling of team_dates_for_round: that function can
    only ever see rounds that have already happened, since it's built from
    player_game_stats (played games only). This one works for a round that
    hasn't been played yet - the actual case a lineup *decision* needs.
    Produces the exact same dict[str, str] shape, so build_lineup/
    choose_active_squad/swap_after_day1/availability_label consume either
    interchangeably without caring which one built it.
    """
    dates: dict[str, str] = {}
    for r in schedule_rows:
        if r.get("round") != round_no:
            continue
        date = _date_only(r.get("game_date"))
        if not date:
            continue
        for team in (r.get("local_team_code"), r.get("road_team_code")):
            if team and team not in dates:
                dates[team] = date
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
        return CAPTAIN_SCORE_MULTIPLIER
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


def _lock_formation(
    active_squad: ActiveSquad,
    formation: tuple[int, int, int],
    team_dates: dict[str, str],
    value_fn: ValueFn,
) -> Lineup | None:
    """Build the day-1 lock (starters/sixth-man/bench/captain) for one
    specific formation - the shared inner step both build_lineup's
    formation search and a manually-forced formation use.
    """
    starters = _build_formation_starters(active_squad, formation, team_dates, value_fn)
    if starters is None:
        return None

    min_date = _min_date(team_dates)
    starter_ids = {p.player_id for p in starters}
    remaining = [p for p in active_squad.active if p.player_id not in starter_ids]
    sixth_man = min(
        remaining,
        key=lambda p: (_availability_rank(p, team_dates, min_date), -value_fn(p.player_id)),
    )
    bench = [p for p in remaining if p.player_id != sixth_man.player_id]
    captain = max(starters, key=lambda p: value_fn(p.player_id))

    return Lineup(starters=starters, sixth_man=sixth_man, bench=bench, captain=captain)


def build_lineup(
    active_squad: ActiveSquad,
    team_dates: dict[str, str],
    value_fn: ValueFn,
    formation: tuple[int, int, int] | None = None,
) -> Lineup:
    """The day-1 lock: fill a formation preferring day-1-eligible players
    first (the golden rule), then by value_fn descending. Sixth man = best
    remaining player under that same day-1-first-then-value rule (any
    position) - it's a full-scoring slot just like a starter slot, so it
    needs to follow the golden rule too: otherwise a later-playing player
    can grab it outright (capturing only their one game) while a day-1
    player of real value gets stuck on the half-scoring bench with no way
    to ever be upgraded. Captain = highest value_fn among the five starters.

    Pass an explicit `formation` (one of VALID_FORMATIONS) to force that
    shape - e.g. the user manually picked one on the /lineup page. By
    default (formation=None), all three valid formations are tried and
    scored by what they actually lead to: each candidate's day-1 lock is
    carried all the way through a simulated swap_after_day1 (using
    value_fn for both, since only projections exist at decision time
    either way), and whichever formation produces the highest final total
    wins - not just whichever has the most day-1 starters (an earlier,
    weaker proxy). This matters because the choice of formation determines
    which specific day-1 players get "protected" with a full slot (and so
    stay flexible - keepable-or-demotable - once their games are over): a
    formation with more Guard slots protects more day-1 Guards at the cost
    of fewer Forward/Center slots, and only a downstream simulation can say
    which trade-off actually pays off, since the swap step is itself
    formation-agnostic once day-1 is locked in (see swap_after_day1).
    """
    if formation is not None:
        initial = _lock_formation(active_squad, formation, team_dates, value_fn)
        if initial is None:
            raise RuntimeError(f"Formation {formation} isn't feasible with this active squad")
        return initial

    projected_as_actual = {p.player_id: value_fn(p.player_id) for p in active_squad.active}
    best_initial: Lineup | None = None
    best_score: float | None = None

    for f in VALID_FORMATIONS:
        initial = _lock_formation(active_squad, f, team_dates, value_fn)
        if initial is None:
            continue

        final = swap_after_day1(initial, team_dates, value_fn)
        score = compute_round_score(final, projected_as_actual)

        if best_score is None or score > best_score:
            best_score = score
            best_initial = initial

    if best_initial is None:
        raise RuntimeError("No valid formation (2-2-1 / 2-1-2 / 3-1-1) could be filled from this active squad")

    return best_initial


def swap_after_day1(
    lineup: Lineup,
    team_dates: dict[str, str],
    value_fn: ValueFn,
    formation: tuple[int, int, int] | None = None,
) -> Lineup:
    """The day-2 decision: given the day-1 lock, re-solve for the best final
    lineup now that both formation and full-slot occupants can change.

    A day-1 player who started on the BENCH is locked there for good - the
    real rules never let you swap in a player who's already played, so
    promoting them now is never an option. Everyone else is "flexible": the
    six day-1 full-scoring-slot players (each can be kept, at full rate, or
    demoted to the bench at half rate) plus every day-2 player regardless of
    where they were initially placed (they haven't played yet, so they're
    free to end up anywhere). The final lineup is simply the best valid
    formation + sixth man drawn from that flexible pool by value_fn alone -
    no day-1-first tiebreak here, since this is the last decision point and
    there's no future swap left to preserve optionality for.

    This is equivalent to, but simpler than, an explicit per-slot "swap only
    if the incoming player beats the outgoing one" comparison: picking the
    flexible pool's best value per slot naturally keeps a day-1 occupant
    when nothing beats them, and replaces them when something does - which
    correctly prices in that demoting them costs half of what they'd have
    kept by staying (see the module docstring's 2026-09-16 correction).

    Pass an explicit `formation` to restrict the final re-solve to that one
    shape instead of freely reconsidering all three - e.g. the user
    manually locked a formation on the /lineup page and wants the day-2
    plan to keep using it rather than switch shape.
    """
    min_date = _min_date(team_dates)

    day1_bench_locked_ids = {
        p.player_id for p in lineup.bench if _availability_rank(p, team_dates, min_date) == 0
    }
    flexible = [p for p in _all_players(lineup) if p.player_id not in day1_bench_locked_ids]
    day1_bench_locked = [p for p in lineup.bench if p.player_id in day1_bench_locked_ids]

    # Captain rule (docs/game_rules.md, enforced 2026-09-26): the captaincy
    # can only move to a starter who hasn't played yet - so the final
    # captain is either the day-1 captain (kept) or a player whose game is
    # still to come. Previously the captain was simply the top final
    # starter by value, which could illegally hand the 1.5x to a day-1
    # player after their (actual, known) score was in.
    day1_ids = {p.player_id for p in _all_players(lineup) if _availability_rank(p, team_dates, min_date) == 0}

    def captain_allowed(p: Projection) -> bool:
        return p.player_id == lineup.captain.player_id or p.player_id not in day1_ids

    # Exact re-solve: for each formation x legal captain, fill the other
    # starters and then the sixth man by value. For a fixed formation and
    # captain that greedy fill is optimal; the round total (bench at half
    # rate) ranks lineups the same as starters + sixth man + captain again,
    # since each extra full-slot point or captaincy is worth +0.5 over the
    # bench/non-captain rate.
    candidate_formations = [formation] if formation is not None else VALID_FORMATIONS
    positions = ("Guard", "Forward", "Center")
    best: tuple[float, list[Projection], Projection, Projection] | None = None

    for f in candidate_formations:
        for captain in flexible:
            if not captain_allowed(captain):
                continue
            need = dict(zip(positions, f))
            if need.get(captain.position, 0) == 0:
                continue
            need[captain.position] -= 1

            others = [p for p in flexible if p.player_id != captain.player_id]
            starters = [captain]
            for pos in positions:
                ranked = sorted((p for p in others if p.position == pos), key=lambda p: -value_fn(p.player_id))
                if len(ranked) < need[pos]:
                    break
                starters.extend(ranked[: need[pos]])
            else:
                starter_ids = {p.player_id for p in starters}
                remaining = [p for p in flexible if p.player_id not in starter_ids]
                sixth_man = max(remaining, key=lambda p: value_fn(p.player_id))
                total = (
                    sum(value_fn(p.player_id) for p in starters)
                    + value_fn(sixth_man.player_id)
                    + value_fn(captain.player_id)
                )
                if best is None or total > best[0]:
                    best = (total, starters, sixth_man, captain)

    if best is None:
        raise RuntimeError(
            "No valid formation (2-2-1 / 2-1-2 / 3-1-1) could be filled from the flexible pool - "
            "should be unreachable since the day-1 lineup's own 5 starters always remain flexible"
        )

    _total, best_starters, sixth_man, captain = best
    full_ids = {p.player_id for p in best_starters} | {sixth_man.player_id}
    bench = [p for p in flexible if p.player_id not in full_ids] + day1_bench_locked
    return Lineup(starters=best_starters, sixth_man=sixth_man, bench=bench, captain=captain)


def choose_active_squad(
    roster: Roster,
    team_dates: dict[str, str],
    value_fn: ValueFn,
    formation: tuple[int, int, int] | None = None,
) -> ActiveSquad:
    """Pick which 3 of the 13 roster players to exclude this round - the one
    real per-round decision that build_lineup/swap_after_day1 previously
    assumed was already made for them (engine.roster.sample_active_squad is
    a random stand-in, POC/backtest-only, not real selection logic).

    Brute-forces every combination of 3 exclusions (C(13,3) = 286 - cheap)
    rather than a greedy "cut the 3 lowest-projected players" shortcut,
    because a bench spot has option value a straight exclusion doesn't: a
    benched player can still be swapped into a full-scoring slot after day
    1, while an excluded player never can (see game_rules.md). Scoring each
    candidate by its projected value *after* the same swap_after_day1 logic
    used for the real recommendation - using projections as the stand-in
    for "actual" results, since the exclusion is locked in before the round
    starts and no results exist yet - means this picks the exclusion that
    sets up the best real decision, not just the best-looking static XI.

    `formation`, if given, is forwarded to build_lineup/swap_after_day1 to
    force that shape throughout instead of letting each auto-search for the
    best one.
    """
    best_squad: ActiveSquad | None = None
    best_score: float | None = None

    for excluded in itertools.combinations(roster.players, 3):
        excluded_ids = {p.player_id for p in excluded}
        active = [p for p in roster.players if p.player_id not in excluded_ids]

        try:
            candidate = ActiveSquad(active=active, excluded=list(excluded))
        except ValueError:
            continue  # can't field any valid formation with this exclusion

        try:
            initial = build_lineup(candidate, team_dates, value_fn, formation=formation)
        except RuntimeError:
            continue  # this exclusion can't field the forced formation, if one was given
        final = swap_after_day1(initial, team_dates, value_fn, formation=formation)
        projected_as_actual = {p.player_id: value_fn(p.player_id) for p in active}
        score = compute_round_score(final, projected_as_actual)

        if best_score is None or score > best_score:
            best_score = score
            best_squad = candidate

    if best_squad is None:
        raise RuntimeError("No feasible 3-player exclusion found across this roster - check position counts")

    return best_squad


def compute_round_score(lineup: Lineup, actual_pir: dict[str, float]) -> float:
    """Round score given real (or backtest ground-truth) fantasy score per
    player, using ONLY the given lineup's own tier assignments (captain
    1.5x, starter/sixth-man full, bench half - see _tier_multiplier).

    Confirmed by the user (2026-09-16): a player's score is governed
    entirely by whatever tier they hold in the lineup passed in here - there
    is no separate "day-1 points are banked regardless of a later swap"
    carve-out (an earlier, wrong assumption in this codebase - see the
    module docstring). To score a round's real final outcome, pass the
    post-swap_after_day1 lineup; to score the no-swap baseline for
    comparison, pass the initial (pre-swap) lineup instead - either way,
    this function itself doesn't need to know which day anyone played.

    `actual_pir` is misleadingly named for historical reasons - despite the
    name, this function is agnostic to what's in it, but every real caller
    should pass each player's actual PIR ALREADY adjusted for the +10%
    team-win bonus (see engine.projections.actual_fantasy_score), not raw
    PIR - this function has no access to who actually won, so it can't
    apply that bonus itself. **Confirmed 2026-09-22**: this was missed
    entirely until then, silently understating every real/backtested round
    score.
    """
    return sum(actual_pir.get(p.player_id, 0.0) * _tier_multiplier(p, lineup) for p in _all_players(lineup))


def availability_label(player: Projection, team_dates: dict[str, str]) -> str:
    min_date = _min_date(team_dates)
    rank = _availability_rank(player, team_dates, min_date)
    return {0: "day1", 1: "later", 2: "not playing"}[rank]
