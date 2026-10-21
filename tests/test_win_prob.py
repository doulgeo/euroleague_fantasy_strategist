import pytest

from engine.win_prob import Game, WinModel, game_features, games_from_rows, team_form


def _game(date, code, home="A", away="B", won=True, margin=10, season="E2024"):
    return Game(season, code, code, date, home, away, won, margin)


def test_form_excludes_same_date_future_and_undated_games():
    old = _game("2025-01-01", 99)
    later = [_game("2025-01-02", 1, won=False), _game("2025-01-03", 2, won=False)]
    assert team_form([*later, old], "2025-01-02") == {"A": (1.0, 1.0), "B": (0.0, -1.0)}
    target = _game("2025-01-02", 3)
    assert game_features([old, *later], target) == (1.0, 2.0)
    assert game_features([old], target) == game_features([old, *later], target)


def test_form_uses_last_twenty_date_ordered_games_across_seasons():
    games = [_game(f"2025-01-{i:02d}", i, won=i > 5, season="E2024" if i <= 10 else "E2025")
             for i in range(1, 26)]
    assert team_form(list(reversed(games)), "2025-02-01")["A"][0] == 1.0


def test_unknown_teams_have_neutral_form():
    assert game_features([], _game("2025-01-01", 1)) == (0.0, 0.0)


def test_model_probabilities_are_bounded_and_improve_with_team_form():
    model = WinModel(0.4, 2.0, 0.1)
    assert 0 < model.home_probability(-100) < model.home_probability(0) < model.home_probability(100) < 1
    assert model.home_probability(0.3) > model.home_probability(-0.3)


def test_game_extraction_deduplicates_players_and_keeps_seasons_separate():
    home = {"season_code": "E2024", "game_code": 1, "round": 1, "game_date": "2025-01-01T20:00:00",
            "team": "A", "opponent": "B", "home_away": "home", "team_win": True,
            "player_id": "H", "points": 80}
    away = {**home, "team": "B", "opponent": "A", "home_away": "away", "team_win": False,
            "player_id": "V", "points": 70}
    games = games_from_rows([home, home, away, {**home, "season_code": "E2025"},
                             {**away, "season_code": "E2025"},
                             {**home, "game_code": 2, "team_win": None}])
    assert len(games) == 2
    assert [g.margin for g in games] == [10, 10]
    assert {g.season for g in games} == {"E2024", "E2025"}
    assert games_from_rows([home]) == []


def test_fit_rejects_no_outcomes():
    from engine.win_prob import fit_win_model

    with pytest.raises(ValueError, match="both home wins and losses"):
        fit_win_model([], ("E2024",))


def _projection(pid="P", team="A", pir=10.0, p_play=0.8):
    from engine.projections import Projection

    return Projection(pid, pid, "Guard", team, 10, pir, 1200, 2, 0.9, pir * 1.09, p_play=p_play)


def _schedule(date="2025-01-02", code=2, home="A", away="B", rnd=2):
    return {"season_code": "E2024", "game_code": code, "round": rnd, "game_date": date,
            "local_team_code": home, "road_team_code": away}


def test_round_values_apply_availability_and_bonus_once_without_mutating_pool():
    from engine.win_prob import round_projections, round_values

    pool = {"P": _projection(), "B": _projection("B", "B"), "Z": _projection("Z", "Z")}
    model = WinModel(0, 0)  # 50% each side
    schedule = [_schedule()]
    actual = round_projections(pool, schedule, 2, [], model=model, home_pir=0.4)
    assert actual["P"].expected_value == pytest.approx(10.4 * 1.05 * 0.8)
    assert actual["B"].expected_value == pytest.approx(9.6 * 1.05 * 0.8)
    assert actual["Z"].expected_value == 0.0
    assert pool["P"].projected_pir == 10.0 and pool["P"].team_win_rate == 0.9
    assert actual["P"].p_play == pool["P"].p_play
    assert round_values(pool, schedule, 2, [], model=model, home_pir=0.4)["P"] == actual["P"].expected_value


def test_round_context_uses_one_round_start_cutoff_for_all_dates():
    from engine.win_prob import round_projections

    model = WinModel(0.4, 2)
    first = _game("2025-01-01", 1)
    later = _game("2025-01-02", 2, home="C", away="D")
    history = []
    for g in (first, later):
        history.extend([
            {"season_code": "E2024", "game_code": g.code, "round": 1, "game_date": g.date,
             "team": g.home, "player_id": g.home, "home_away": "home", "team_win": True, "points": 80},
            {"season_code": "E2024", "game_code": g.code, "round": 1, "game_date": g.date,
             "team": g.away, "player_id": g.away, "home_away": "away", "team_win": False, "points": 70},
        ])
    pool = {"P": _projection(team="C")}
    schedule = [_schedule(), _schedule("2025-01-03", 3, "C", "D")]
    before = round_projections(pool, schedule, 2, history[:2], model=model)
    after = round_projections(pool, schedule, 2, history, model=model)
    assert before == after  # same-day game never becomes a feature for later dates
    late_cutoff = round_projections(pool, schedule, 2, history, model=model, cutoff_date="2025-01-04")
    assert before == late_cutoff  # caller cannot advance the round-start freeze


def test_home_and_away_probabilities_complement_and_unknown_matchup_uses_home_prior():
    from engine.win_prob import round_projections

    model = WinModel(0.6, 2)
    pool = {"H": _projection("H", "A"), "V": _projection("V", "B")}
    adjusted = round_projections(pool, [_schedule()], 2, [], model=model)
    assert adjusted["H"].team_win_rate + adjusted["V"].team_win_rate == pytest.approx(1.0)
    assert adjusted["H"].team_win_rate == pytest.approx(model.home_probability(0))


def test_postponed_games_do_not_count_and_duplicate_schedule_rows_do_not_double_bonus():
    from engine.win_prob import round_values

    schedule = [_schedule(), _schedule(), _schedule("2025-01-03", 3, "C", "D"),
                _schedule("2025-03-01", 4, "E", "F")]
    pool = {"A": _projection("A", "A"), "E": _projection("E", "E")}
    values = round_values(pool, schedule, 2, [], model=WinModel(0, 0), home_pir=0)
    assert values["A"] == pytest.approx(10 * 1.05 * 0.8)
    assert values["E"] == 0.0


def test_current_team_neutral_probability_overrides_stale_bonus_even_for_manual_estimate():
    from engine.rosters import merge_roster
    from engine.win_prob import apply_neutral_context

    pool = {"P": _projection(team="OLD")}
    merged, _, _, estimated = merge_roster(pool, [
        {"player_id": "P", "player_name": "P", "team": "NEW", "position": "Guard"},
    ], {"P"}, {"P": {"projected_pir": 20}})
    result = apply_neutral_context(merged, [], model=WinModel(0.6, 2))
    assert result["P"].team == "NEW" and estimated == {"P"}
    assert result["P"].projected_pir_with_bonus == pytest.approx(21)
    assert result["P"].team_win_rate == pytest.approx(0.5)
    assert result["P"].p_play == 0.8


def test_context_before_training_end_falls_back_to_legacy():
    from engine.win_prob import apply_neutral_context, round_projections

    pool = {"P": _projection()}
    model = WinModel(0.6, 2, trained_through="2025-05-25")
    assert apply_neutral_context(pool, [], "2025-01-02", model) == pool
    old = round_projections(pool, [_schedule()], 2, [], model=model, home_pir=1)
    assert old["P"] == pool["P"]


def test_legacy_explicitly_keeps_existing_bonus_for_scheduled_players():
    from engine.win_prob import round_projections

    pool = {"P": _projection()}
    assert round_projections(pool, [_schedule()], 2, [], enabled=False)["P"] == pool["P"]


def test_unranked_placeholder_stays_zero_and_out_player_stays_zero():
    from dataclasses import replace
    from engine.win_prob import round_values

    pool = {"N": replace(_projection("N", pir=0), games_sampled=0),
            "O": _projection("O", p_play=0), "M": _projection("M", pir=0)}
    values = round_values(pool, [_schedule()], 2, [], model=WinModel(0, 0), home_pir=0.4)
    assert values["N"] == values["O"] == 0
    assert values["M"] > 0  # real zero-valued estimate still gets location context


def test_neutral_probability_uses_current_club_strength_without_home_advantage():
    from engine.win_prob import neutral_probabilities

    h = {"season_code": "E2024", "game_code": 1, "round": 1, "game_date": "2025-01-01",
         "team": "A", "player_id": "A", "home_away": "home", "team_win": True, "points": 80}
    a = {**h, "team": "B", "player_id": "B", "home_away": "away", "team_win": False, "points": 70}
    probabilities = neutral_probabilities([h, a], "2025-01-02", WinModel(0.6, 2))
    assert probabilities["A"] > 0.5 > probabilities["B"]
    assert probabilities["A"] + probabilities["B"] == pytest.approx(1)


def test_fit_is_unchanged_by_future_holdout_outcomes():
    from engine.win_prob import fit_win_model

    train = [_game("2024-01-01", 1), _game("2024-01-02", 2, won=False)]
    future = [_game("2025-01-01", 1, season="E2025", won=False)]
    assert fit_win_model(train, ("E2024",)) == fit_win_model(train + future, ("E2024",))
