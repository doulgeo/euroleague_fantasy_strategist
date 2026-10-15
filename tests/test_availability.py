import pytest

from engine.availability import AvailabilityConfig, estimate_p_play, injury_multiplier

CFG = AvailabilityConfig(window=3, prior_mean=0.8, prior_strength=1.0, half_life=None)


def _g(pid, team, code, played=True):
    return {"player_id": pid, "team": team, "game_code": code, "game_date": f"2025-10-{code:02d}T20:00:00", "played": played}


def _team_games(team, codes):  # a teammate who always plays, so every game exists
    return [_g("mate-" + team, team, c) for c in codes]


def test_share_of_recent_club_games_with_prior():
    hist = _team_games("A", range(1, 7)) + [_g("P", "A", c, played=c in (4, 6)) for c in range(1, 7)]
    # last 3 club games 4,5,6: played 4 and 6 -> (2 + 0.8) / (3 + 1)
    assert estimate_p_play(hist, config=CFG)["P"] == pytest.approx(0.7)


def test_games_before_joining_the_club_dont_count():
    hist = _team_games("A", range(1, 7)) + _team_games("B", range(1, 7))
    hist += [_g("P", "B", c) for c in (1, 2)] + [_g("P", "A", 5), _g("P", "A", 6, played=False)]
    # joined A at game 5: only games 5,6 count -> played 1 of 2
    assert estimate_p_play(hist, config=CFG)["P"] == pytest.approx((1 + 0.8) / (2 + 1))


def test_current_team_override_and_no_stint():
    hist = _team_games("A", range(1, 4)) + [_g("P", "A", c) for c in range(1, 4)]
    assert "P" not in estimate_p_play(hist, current_team={"P": "ZZZ"}, config=CFG)


def test_cutoff_date_hides_later_games():
    hist = _team_games("A", range(1, 5)) + [_g("P", "A", c, played=c < 4) for c in range(1, 5)]
    assert estimate_p_play(hist, "2025-10-04", config=CFG)["P"] == pytest.approx((3 + 0.8) / 4)


def test_injury_multipliers():
    assert injury_multiplier("Out") == 0.0
    assert injury_multiplier("Questionable") == 0.6
    assert injury_multiplier(None) == 1.0
    assert injury_multiplier("Something new") == 1.0
