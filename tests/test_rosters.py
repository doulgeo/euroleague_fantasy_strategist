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


def test_active_override_beats_the_model_for_a_known_player():
    pool = {"A": _proj("A", "MAD", 0.7)}
    roster = [{"player_id": "A", "player_name": "A", "team": "MAD", "position": "Guard"}]
    merged, _new, _gone, est = merge_roster(pool, roster, {"A"}, {"A": {"projected_pir": 20.0}})
    assert est == {"A"}
    assert merged["A"].projected_pir == 20.0
    assert merged["A"].projected_pir_with_bonus == pytest.approx(21.4)


def test_override_expires_once_its_round_is_played():
    import sqlite3
    from engine.db import active_manual_projections, get_connection, set_manual_projection

    conn = get_connection(":memory:")
    for rnd, played in ((3, 1), (4, 0)):
        conn.execute("INSERT INTO schedule VALUES ('E2026', ?, ?, '2026-10-01', 'A', 'B', ?, 'x')", (rnd * 10, rnd, played))
    set_manual_projection(conn, "P3", "P3", 10.0, expires_after_round=3)
    set_manual_projection(conn, "P4", "P4", 10.0, expires_after_round=4)
    set_manual_projection(conn, "PN", "PN", 10.0)
    assert set(active_manual_projections(conn, "E2026")) == {"P4", "PN"}
    assert set(active_manual_projections(conn, "E2026", before_round=4)) == {"P4", "PN"}
