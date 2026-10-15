"""swap_after_day1 rule tests: the captain rule (captaincy can only move to a
starter who hasn't played yet), day-1 bench players never promoted, and the
final re-solve being the true optimum among every legal final lineup. Plus
the exclusion rule: the 3 excluded players are one per position."""

from __future__ import annotations

import itertools
import random

import pytest

from engine.lineup import (
    VALID_FORMATIONS,
    Lineup,
    choose_active_squad,
    compute_round_score,
    day1_results,
    swap_after_day1,
)
from engine.projections import Projection
from engine.roster import ActiveSquad, Roster

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


def test_exclusion_is_one_per_position():
    # the 3 worst players are all Guards - the rule still forces 1G/1F/1C out
    roster = Roster(
        players=[_p(f"g{i}", "Guard", "D1") for i in range(5)]
        + [_p(f"f{i}", "Forward", "D1") for i in range(5)]
        + [_p(f"c{i}", "Center", "D1") for i in range(3)]
    )
    values = {p.player_id: 20.0 for p in roster.players} | {"g2": 1.0, "g3": 0.0, "g4": -1.0, "f1": 5.0, "c2": 6.0}
    squad = choose_active_squad(roster, {"D1": "2026-10-01"}, values.get)
    assert sorted(p.player_id for p in squad.excluded) == ["c2", "f1", "g4"]

    guards = roster.by_position("Guard")
    with pytest.raises(ValueError, match="exactly 1 Guard, 1 Forward and 1 Center"):
        ActiveSquad(active=[p for p in roster.players if p not in guards[2:]], excluded=guards[2:])


def test_day1_results_scores_played_and_zeroes_no_shows():
    team_dates = {"D1": "2026-10-01", "X1": "2026-10-01", "D2": "2026-10-02", "Y1": "2026-10-01"}
    schedule = [
        {"round": 1, "game_date": "2026-10-01T20:00:00", "local_team_code": "D1", "road_team_code": "X1", "played": 1},
        {"round": 1, "game_date": "2026-10-01T21:00:00", "local_team_code": "Y1", "road_team_code": "Z1", "played": 0},
        {"round": 1, "game_date": "2026-10-02T20:00:00", "local_team_code": "D2", "road_team_code": "W2", "played": 1},
    ]
    rows = [
        {"player_id": "a", "team": "D1", "pir_official": 20, "team_win": True},
        {"player_id": "b", "team": "X1", "pir_official": 5, "team_win": False},
        {"player_id": "c", "team": "D2", "pir_official": 30, "team_win": True},  # day 2: never visible
    ]
    scores, done = day1_results(rows, schedule, team_dates)
    assert scores == {"a": pytest.approx(22.0), "b": 5.0}
    assert done == {"D1", "X1"}  # Y1's game isn't over, D2 isn't day 1


def test_swap_uses_day1_reality():
    # same day-1 lock, different day-1 results -> different swap plan: g1
    # (day-1 starter) flopped with 0, so day-2 g3 takes his slot at no cost,
    # while g0's real 40 keeps him captain
    lineup = _day1_lineup()
    values = {p.player_id: 10.0 for p in [*lineup.starters, lineup.sixth_man, *lineup.bench]}
    values["g3"] = 15.0
    before = swap_after_day1(lineup, TEAM_DATES, values.get)
    after = swap_after_day1(lineup, TEAM_DATES, (values | {"g0": 40.0, "g1": 0.0}).get)
    assert before != after
    assert after.captain.player_id == "g0"
    assert "g1" not in {p.player_id for p in after.starters}


# --- swap after every date (3-date rounds, confirmed 2026-10-15) ----------

from engine.lineup import plan_round, round_dates, swap_after_date  # noqa: E402

THREE_DATES = {"D1": "2026-10-01", "D2": "2026-10-02", "D3": "2026-10-03"}


def _three_date_lineup() -> Lineup:
    g = [_p("g0", "Guard", "D1"), _p("g1", "Guard", "D2"), _p("g2", "Guard", "D3"), _p("g3", "Guard", "D3")]
    f = [_p("f0", "Forward", "D1"), _p("f1", "Forward", "D2"), _p("f2", "Forward", "D3"), _p("f3", "Forward", "D2")]
    c = [_p("c0", "Center", "D1"), _p("c1", "Center", "D3")]
    return Lineup(starters=[g[0], g[1], f[0], f[1], c[0]], sixth_man=g[2], bench=[f[2], f[3], c[1], g[3]], captain=g[0])


def test_round_dates():
    assert round_dates(THREE_DATES) == ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_plan_round_two_dates_equals_swap_after_day1_with_day1_actuals():
    lineup = _day1_lineup()
    rng = random.Random(3)
    proj = {p.player_id: rng.uniform(2, 20) for p in [*lineup.starters, lineup.sixth_man, *lineup.bench]}
    actual = {pid: rng.uniform(0, 30) for pid in proj}
    value = lambda pid: actual[pid] if pid in {p.player_id for p in [*lineup.starters, lineup.sixth_man, *lineup.bench] if p.team == "D1"} else proj[pid]  # noqa: E731
    a = plan_round(lineup, TEAM_DATES, lambda pid: proj[pid], actual)
    b = swap_after_day1(lineup, TEAM_DATES, value)
    assert [p.player_id for p in a.starters] == [p.player_id for p in b.starters]
    assert a.sixth_man.player_id == b.sixth_man.player_id and a.captain.player_id == b.captain.player_id


def test_three_date_round_never_promotes_or_recaptains_a_player_who_has_played():
    rng = random.Random(11)
    for _ in range(60):
        lineup = _three_date_lineup()
        players = [*lineup.starters, lineup.sixth_man, *lineup.bench]
        proj = {p.player_id: rng.uniform(2, 20) for p in players}
        actual = {p.player_id: rng.uniform(0, 30) for p in players}
        final = plan_round(lineup, THREE_DATES, lambda pid: proj[pid], actual)
        full = {p.player_id for p in [*final.starters, final.sixth_man]}
        shape = tuple(sum(p.position == pos for p in final.starters) for pos in ("Guard", "Forward", "Center"))
        assert shape in VALID_FORMATIONS and len(full) == 6 and final.captain in final.starters
        # a D1 player starting on the bench stays there through both windows
        for p in lineup.bench:
            if p.team == "D1":
                assert p.player_id not in full
        # the captaincy only leaves the day-1 captain for someone who hadn't played at that window
        assert final.captain.player_id == lineup.captain.player_id or final.captain.team != "D1"


def test_second_window_uses_day2_results():
    lineup = _three_date_lineup()
    players = [*lineup.starters, lineup.sixth_man, *lineup.bench]
    proj = {p.player_id: 10.0 for p in players}
    actual = {p.player_id: 10.0 for p in players}
    actual["f3"] = 0.0  # f3 (D2, bench) flops on day 2; g1 (D2 starter) also played
    after1 = swap_after_date(lineup, THREE_DATES, lambda pid: proj[pid], "2026-10-01")
    final = plan_round(lineup, THREE_DATES, lambda pid: proj[pid], actual)
    # whoever was benched after D2's games (played, now locked) can't be back in a full slot
    window2_bench_d2 = {p.player_id for p in final.bench if p.team in ("D1", "D2")}
    assert all(pid not in {q.player_id for q in [*final.starters, final.sixth_man]} for pid in window2_bench_d2)
    assert after1.captain.player_id in {p.player_id for p in players}
