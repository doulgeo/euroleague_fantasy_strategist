from engine.projections import Projection, build_projections


def _rows(pir_values):
    return [
        {
            "player_id": "P1", "player_name": "PLAYER", "position": "Guard", "team": "AAA",
            "round": i + 1, "game_code": i + 1, "game_date": f"2025-10-{i + 1:02d}",
            "played": True, "pir_official": v, "minutes_seconds": 1200, "team_win": True,
        }
        for i, v in enumerate(pir_values)
    ]


def test_hot_streak_raises_projection_and_flags_hot():
    p = build_projections(_rows([8, 8, 8, 8, 8, 20, 22, 24]), as_of_round=99)["P1"]
    assert p.recent_pir == 22
    assert p.projection_change > 0
    assert p.form == "hot"


def test_cold_streak_flags_cold():
    p = build_projections(_rows([15, 15, 15, 15, 15, 3, 2, 1]), as_of_round=99)["P1"]
    assert p.projection_change < 0
    assert p.form == "cold"


def test_steady_player_has_no_form_badge():
    p = build_projections(_rows([10, 11, 9, 10, 10, 11, 9, 10]), as_of_round=99)["P1"]
    assert p.form is None


def test_no_previous_projection_means_zero_change():
    # exactly min_games games: no projection existed before the latest one
    p = build_projections(_rows([5, 5, 30]), as_of_round=99)["P1"]
    assert p.projection_change == 0.0


def test_placeholder_projection_has_no_form():
    p = Projection("X", "X", "Guard", "AAA", 0, 0.0, 0.0, 0.0, None, 0.0)
    assert p.form is None and p.recent_pir is None
