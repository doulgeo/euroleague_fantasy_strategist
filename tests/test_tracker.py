"""Points tracker tests (engine.tracker): slot validation, scoring against a
hand-computed total, the exact best-possible search, the day-1 -> final
rule warnings, and a DB round-trip of a saved lineup."""

from __future__ import annotations

import itertools

import pytest

from engine.db import get_connection, load_tracked_lineups, save_tracked_lineup
from engine.projections import Projection
from engine.tracker import (
    actual_scores,
    best_possible_lineup,
    final_rule_warnings,
    lineup_from_slots,
    lineup_to_slots,
    score_round,
    snapshot_rows,
)


def _p(pid: str, pos: str, team: str = "AAA", value: float = 10.0) -> Projection:
    return Projection(pid, f"PLAYER {pid}", pos, team, 5, value, 1500.0, 3.0, 0.5, value)


# 5 Guards, 5 Forwards, 3 Centers - the real roster shape.
ROSTER = (
    [_p(f"g{i}", "Guard", "DAY1" if i < 3 else "DAY2") for i in range(5)]
    + [_p(f"f{i}", "Forward", "DAY1" if i < 3 else "DAY2") for i in range(5)]
    + [_p(f"c{i}", "Center", "DAY1" if i < 2 else "DAY2") for i in range(3)]
)
TEAM_DATES = {"DAY1": "2026-10-01", "DAY2": "2026-10-02"}

# 2-2-1: captain g0, starters g1 f0 f1 c0, sixth g2, bench f2 f3 c1 g3, excluded g4 f4 c2
VALID_SLOTS = {
    "g0": "captain", "g1": "starter", "f0": "starter", "f1": "starter", "c0": "starter",
    "g2": "sixth", "f2": "bench", "f3": "bench", "c1": "bench", "g3": "bench",
    "g4": "excluded", "f4": "excluded", "c2": "excluded",
}


def test_valid_slots_build_lineup():
    lineup, excluded = lineup_from_slots(ROSTER, VALID_SLOTS)
    assert lineup.captain.player_id == "g0"
    assert {p.player_id for p in lineup.starters} == {"g0", "g1", "f0", "f1", "c0"}
    assert lineup.sixth_man.player_id == "g2"
    assert {p.player_id for p in excluded} == {"g4", "f4", "c2"}
    assert lineup_to_slots(lineup, excluded) == VALID_SLOTS


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"g0": None}, "No slot chosen"),
        ({"g1": "captain"}, "Wrong slot counts"),  # two captains, three starters
        ({"g2": "bench", "f2": "sixth", "g3": "excluded", "g4": "bench"}, None),  # still valid
        ({"c0": "bench", "c1": "starter"}, None),  # still valid (different center)
        ({"g3": "starter"}, "Wrong slot counts"),  # 5 starters + captain, 3 bench
        # swap a Center starter for a Forward: 2-3-0 isn't a formation
        ({"c0": "bench", "f2": "starter"}, "must be one of"),
    ],
)
def test_slot_validation(changes, message):
    slots = {**VALID_SLOTS, **changes}
    slots = {k: v for k, v in slots.items() if v is not None}
    if message is None:
        lineup_from_slots(ROSTER, slots)
    else:
        with pytest.raises(ValueError, match=message):
            lineup_from_slots(ROSTER, slots)


def test_score_matches_hand_computed_total():
    # g0 captain 20 won -> 22 * 1.5 = 33; g1 10 (lost) = 10; f0 -2 = -2;
    # f1/c0 DNP = 0; g2 sixth 8 won -> 8.8; bench f2 12 -> 6; excluded g4 30 -> 0
    rows = [
        {"player_id": "g0", "pir_official": 20, "team_win": True},
        {"player_id": "g1", "pir_official": 10, "team_win": False},
        {"player_id": "f0", "pir_official": -2, "team_win": False},
        {"player_id": "g2", "pir_official": 8, "team_win": True},
        {"player_id": "f2", "pir_official": 12, "team_win": None},
        {"player_id": "g4", "pir_official": 30, "team_win": False},
    ]
    actual = actual_scores(rows)
    snapshot = {"formation": "2-2-1", "players": snapshot_rows(ROSTER, VALID_SLOTS)}
    result = score_round(1, {"my_final": snapshot, "my_day1": snapshot}, actual, complete=True, official=47.0)
    assert result.my_final == pytest.approx(33 + 10 - 2 + 8.8 + 6)
    assert result.swap_gain == pytest.approx(0.0)
    assert result.official_diff == pytest.approx(47.0 - result.my_final)
    assert sum(line.points for line in result.breakdown) == pytest.approx(result.my_final)


def _brute_force_best(players, actual):
    """Every possible slot assignment, the slow way - to check the fast search."""
    best = None
    ids = [p.player_id for p in players]
    for excluded in itertools.combinations(ids, 3):
        active = [i for i in ids if i not in excluded]
        for full6 in itertools.combinations(active, 6):
            for sixth in full6:
                starters = [i for i in full6 if i != sixth]
                for captain in starters:
                    slots = {i: "bench" for i in active}
                    slots.update({i: "excluded" for i in excluded})
                    slots.update({i: "starter" for i in starters})
                    slots[sixth] = "sixth"
                    slots[captain] = "captain"
                    try:
                        lineup, _ = lineup_from_slots(players, slots)
                    except ValueError:
                        continue
                    score = sum(
                        actual.get(p.player_id, 0) * m
                        for p, m in [(p, 1.5 if p is lineup.captain else 1.0) for p in lineup.starters]
                        + [(lineup.sixth_man, 1.0)]
                        + [(p, 0.5) for p in lineup.bench]
                    )
                    best = score if best is None else max(best, score)
    return best


def test_best_possible_is_exact_and_dominates():
    # small hand-made spread including negatives and DNPs
    values = {"g0": 5, "g1": 25, "g2": -4, "g3": 12, "g4": 0,
              "f0": 18, "f1": 3, "f2": 22, "f3": -1,
              "c0": 9, "c1": 30, "c2": 2}  # f4 DNP
    best_lineup, best_excluded = best_possible_lineup(ROSTER, values)
    from engine.lineup import compute_round_score
    best = compute_round_score(best_lineup, values)
    assert best == pytest.approx(_brute_force_best(ROSTER, values))

    snapshot = {"formation": "2-2-1", "players": snapshot_rows(ROSTER, VALID_SLOTS)}
    result = score_round(1, {"my_final": snapshot, "tool_final": snapshot}, values, complete=True)
    assert result.best >= result.my_final
    assert result.best >= result.tool


def test_final_rule_warnings():
    day1 = dict(VALID_SLOTS)
    assert final_rule_warnings(day1, day1, ROSTER, TEAM_DATES) == []

    # f2 is a DAY1 Forward on the bench at the lock - promoting them is illegal
    promoted = {**day1, "f2": "starter", "f1": "bench"}
    assert any("Promoted" in w for w in final_rule_warnings(day1, promoted, ROSTER, TEAM_DATES))

    # swapping an exclusion
    re_excluded = {**day1, "g4": "bench", "g3": "excluded"}
    assert any("excluded" in w for w in final_rule_warnings(day1, re_excluded, ROSTER, TEAM_DATES))

    # captaincy to g1 (a DAY1 starter who already played) is illegal ...
    bad_captain = {**day1, "g0": "starter", "g1": "captain"}
    assert any("Captaincy" in w for w in final_rule_warnings(day1, bad_captain, ROSTER, TEAM_DATES))


def test_saved_lineup_round_trip(tmp_path):
    conn = get_connection(tmp_path / "t.db")
    conn.execute("INSERT INTO managers (manager_id, name) VALUES (1, 'me')")
    rows = snapshot_rows(ROSTER, VALID_SLOTS)
    save_tracked_lineup(conn, "E2026", 1, 1, "my_day1", "2-2-1", rows)
    save_tracked_lineup(conn, "E2026", 1, 1, "my_day1", "2-2-1", rows)  # re-save replaces, never duplicates
    loaded = load_tracked_lineups(conn, "E2026", 1)
    assert list(loaded) == [(1, "my_day1")]
    assert {r["player_id"]: r["slot"] for r in loaded[(1, "my_day1")]["players"]} == VALID_SLOTS
    assert conn.execute("SELECT COUNT(*) FROM tracked_lineup_players").fetchone()[0] == 13
