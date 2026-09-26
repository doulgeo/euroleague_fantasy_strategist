"""swap_after_day1 rule tests: the captain rule (captaincy can only move to a
starter who hasn't played yet), day-1 bench players never promoted, and the
final re-solve being the true optimum among every legal final lineup."""

from __future__ import annotations

import itertools
import random

import pytest

from engine.lineup import VALID_FORMATIONS, Lineup, compute_round_score, swap_after_day1
from engine.projections import Projection

TEAM_DATES = {"D1": "2026-10-01", "D2": "2026-10-02"}


def _p(pid: str, pos: str, team: str) -> Projection:
    return Projection(pid, pid, pos, team, 5, 10.0, 1500.0, 3.0, 0.5, 10.0)


def _day1_lineup() -> Lineup:
    g0, g1, g2, g3 = _p("g0", "Guard", "D1"), _p("g1", "Guard", "D1"), _p("g2", "Guard", "D1"), _p("g3", "Guard", "D2")
    f0, f1, f2, f3 = _p("f0", "Forward", "D1"), _p("f1", "Forward", "D2"), _p("f2", "Forward", "D1"), _p("f3", "Forward", "D2")
    c0, c1 = _p("c0", "Center", "D1"), _p("c1", "Center", "D1")
    return Lineup(starters=[g0, g1, f0, f1, c0], sixth_man=g2, bench=[f2, f3, c1, g3], captain=g0)


def _legal_optimum(lineup: Lineup, values: dict[str, float]) -> float:
    """Every legal final lineup, the slow way."""
    day1 = {p.player_id for p in [*lineup.starters, lineup.sixth_man, *lineup.bench] if p.team == "D1"}
    locked = [p for p in lineup.bench if p.player_id in day1]
    flexible = [p for p in [*lineup.starters, lineup.sixth_man, *lineup.bench] if p not in locked]
    best = None
    for full6 in itertools.combinations(flexible, 6):
        for sixth in full6:
            starters = [p for p in full6 if p is not sixth]
            shape = tuple(sum(p.position == pos for p in starters) for pos in ("Guard", "Forward", "Center"))
            if shape not in VALID_FORMATIONS:
                continue
            for captain in starters:
                if captain.player_id in day1 and captain is not lineup.captain:
                    continue
                bench = [p for p in flexible if p not in full6] + locked
                score = compute_round_score(Lineup(starters, sixth, bench, captain), values)
                best = score if best is None else max(best, score)
    return best


def test_captaincy_never_moves_to_a_player_who_already_played():
    lineup = _day1_lineup()
    # g1 (a day-1 starter, NOT captain) had a huge game; the day-1 captain flopped.
    values = {p.player_id: 10.0 for p in [*lineup.starters, lineup.sixth_man, *lineup.bench]}
    values.update({"g1": 50.0, "g0": 2.0})
    final = swap_after_day1(lineup, TEAM_DATES, lambda pid: values[pid])
    assert final.captain.player_id != "g1"
    assert final.captain.player_id == "g0" or final.captain.team == "D2"


def test_day1_bench_player_never_promoted():
    lineup = _day1_lineup()
    values = {p.player_id: 5.0 for p in [*lineup.starters, lineup.sixth_man, *lineup.bench]}
    values["c1"] = 40.0  # day-1 bench Center had a monster game - still locked on the bench
    final = swap_after_day1(lineup, TEAM_DATES, lambda pid: values[pid])
    assert "c1" in {p.player_id for p in final.bench}


@pytest.mark.parametrize("seed", range(40))
def test_final_resolve_is_the_legal_optimum(seed):
    rng = random.Random(seed)
    lineup = _day1_lineup()
    values = {p.player_id: rng.uniform(-5, 35) for p in [*lineup.starters, lineup.sixth_man, *lineup.bench]}
    final = swap_after_day1(lineup, TEAM_DATES, lambda pid: values[pid])
    assert compute_round_score(final, values) == pytest.approx(_legal_optimum(lineup, values))
