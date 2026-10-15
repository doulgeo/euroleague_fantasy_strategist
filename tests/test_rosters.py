import pytest

from engine.projections import Projection
from engine.rosters import merge_roster


def _proj(pid, team, rate):
    return Projection(pid, pid, "Guard", team, 10, 10.0, 0.0, 0.0, rate, 10.0 * (1 + 0.1 * rate))


def test_manual_override_gets_the_team_win_bonus():
    pool = {"A": _proj("A", "MAD", 0.7)}
    roster = [
        {"player_id": "A", "player_name": "A", "team": "MAD", "position": "Guard"},
        {"player_id": "N", "player_name": "N", "team": "MAD", "position": "Guard"},
        {"player_id": "Z", "player_name": "Z", "team": "XXX", "position": "Guard"},
    ]
    manual = {"N": {"projected_pir": 10.0}, "Z": {"projected_pir": 10.0}}
    merged, new, _gone, est = merge_roster(pool, roster, {"A"}, manual)
    assert est == {"N", "Z"}
    assert merged["N"].projected_pir_with_bonus == pytest.approx(10.7)  # same as a real MAD player
    assert merged["Z"].projected_pir_with_bonus == pytest.approx(10.0)  # club with no rate: no bonus
