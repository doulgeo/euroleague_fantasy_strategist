from engine.projections import LEGACY_CONFIG, Projection, ProjectionConfig, build_projections


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
    p = build_projections(_rows([5, 5, 30]), as_of_round=99, config=LEGACY_CONFIG)["P1"]
    assert p.projection_change == 0.0


def test_placeholder_projection_has_no_form():
    p = Projection("X", "X", "Guard", "AAA", 0, 0.0, 0.0, 0.0, None, 0.0)
    assert p.form is None and p.recent_pir is None


def test_season_points_totals_win_bonus_and_last_game():
    from engine.projections import season_points

    rows = _rows([10, 20])
    rows[1]["team_win"] = False
    rows.append({**rows[0], "round": 3, "game_code": 3, "played": False, "pir_official": 0})
    sp = season_points(rows)["P1"]
    assert sp.games == 2  # the DNP doesn't count
    assert sp.total == 11 + 20  # +10% only on the win
    assert sp.avg == 15.5
    assert sp.last == 20 and sp.last_round == 2


import pytest  # noqa: E402


def test_legacy_config_is_linear_ramp_over_last_10():
    p = build_projections(_rows([10, 20, 30]), as_of_round=99, config=LEGACY_CONFIG)["P1"]
    assert p.projected_pir == pytest.approx((10 * 1 + 20 * 2 + 30 * 3) / 6)
    assert p.n_eff == pytest.approx(36 / 14)  # (1+2+3)^2 / (1+4+9)


def test_exp_weights_half_life():
    cfg = ProjectionConfig(weighting="exp", half_life_games=1.0, min_games=1)
    p = build_projections(_rows([8, 16]), as_of_round=99, config=cfg)["P1"]
    assert p.projected_pir == pytest.approx((8 * 0.5 + 16 * 1.0) / 1.5)


def test_min_games_one_and_shrinkage_toward_position_mean():
    rows = _rows([30]) + [
        {**r, "player_id": f"O{i}", "player_name": f"O{i}", "game_code": 100 + i * 10 + j}
        for i in range(5) for j, r in enumerate(_rows([10, 10, 10, 10]))
    ]
    cfg = ProjectionConfig(weighting="exp", min_games=1, shrink_k=2.0)
    p = build_projections(rows, as_of_round=99, config=cfg)["P1"]
    pos_mean = (30 + 5 * 4 * 10) / 21
    assert p.n_eff == pytest.approx(1.0)
    assert p.projected_pir == pytest.approx((1 * 30 + 2 * pos_mean) / 3)
    assert "P1" not in build_projections(rows, as_of_round=99, config=LEGACY_CONFIG)  # legacy min_games=3 still drops him


def test_season_age_weight_discounts_older_seasons():
    old = [{**r, "season_code": "E2024", "game_date": f"2024-10-{i + 1:02d}"} for i, r in enumerate(_rows([30, 30]))]
    new = [{**r, "season_code": "E2025", "game_code": 10 + i, "game_date": f"2025-10-{i + 1:02d}"} for i, r in enumerate(_rows([10, 10]))]
    cfg = ProjectionConfig(weighting="exp", half_life_games=1e9, season_age_weight=0.25, min_games=1)
    p = build_projections(old + new, as_of_round=99, config=cfg)["P1"]
    assert p.projected_pir == pytest.approx((30 * 0.5 + 10 * 2) / 2.5)
