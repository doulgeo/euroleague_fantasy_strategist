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
