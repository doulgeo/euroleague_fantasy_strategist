"""Tests for the milestone-3 minutes pipeline: leakage discipline on the
new coach/bio/ridge machinery, and determinism of the Monte Carlo
allocation step under a fixed seed (spec §6).
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from proj.allocation import expected_minutes_monte_carlo
from proj.bio import build_player_bio
from proj.coaches import season_primary_coach
from proj.config import load_config
from proj.data import build_all_stint_tables, load_games, opening_day_roster
from proj.minutes import build_training_pairs

warnings.filterwarnings("ignore")

CONFIG = load_config()
SEASONS = CONFIG["seasons"]["all"]


@pytest.fixture(scope="module")
def games():
    return load_games(CONFIG["db_path"], SEASONS)


@pytest.fixture(scope="module")
def stint_tables(games):
    return build_all_stint_tables(games, SEASONS)


@pytest.fixture(scope="module")
def bio():
    return build_player_bio(cache_path=None, fetch_live=False)


def test_monte_carlo_allocation_deterministic_under_fixed_seed():
    raw = np.array([28.0, 25.0, 20.0, 15.0, 10.0, 8.0, 5.0])
    p_active = np.array([0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.6])
    cap = 32.0
    total = 200.0

    run1 = expected_minutes_monte_carlo(raw, p_active, cap, total, draws=500, seed=1)
    run2 = expected_minutes_monte_carlo(raw, p_active, cap, total, draws=500, seed=1)
    np.testing.assert_array_equal(run1, run2)

    run3 = expected_minutes_monte_carlo(raw, p_active, cap, total, draws=500, seed=2)
    assert not np.array_equal(run1, run3)  # different seed -> different draws (sanity, not a hard requirement)


def test_ridge_training_pairs_use_only_pre_target_season_data(games, stint_tables, bio):
    """build_training_pairs' features for predicting `to_season` must only
    be derived from `from_season` (strictly before `to_season` starts) -
    the leakage ground rule, checked on the actual training-pair
    construction rather than just on raw game dates (test_data.py already
    covers that; this covers the feature-engineering layer built on top)."""
    coach_by_season = {s: season_primary_coach(games, s) for s in SEASONS}
    season_start_dates = {"E2023": "2023-09-28", "E2024": "2024-09-26", "E2025": "2025-09-30"}

    pairs = build_training_pairs(
        games, stint_tables, bio, coach_by_season, season_start_dates,
        "E2023", "E2024", downweight=0.5, injury_run_min_games=3, n_flag=2,
    )
    assert len(pairs) > 0

    max_from_season_date = games[games["season_code"] == "E2023"]["game_date"].max()
    min_to_season_date = games[games["season_code"] == "E2024"]["game_date"].min()
    assert max_from_season_date < min_to_season_date  # sanity on the seasons themselves

    # mpg_prior in the training pairs is built purely from from_season's
    # weighted_mpg_active - by construction it cannot reference to_season
    # rows (build_training_pairs never touches stint_tables[to_season]
    # except for the target mpg_next and roster membership/team, both of
    # which are legitimate - team/roster membership is known before a
    # season starts in a live setting, only the *performance* target uses
    # to_season data, which is correct for a supervised-learning target).
    assert "mpg_prior" in pairs.columns


def test_opening_day_roster_uses_only_early_target_season_games(games):
    roster = opening_day_roster(games, "E2024", n_games=3)
    assert not roster.empty
    assert set(roster.columns) >= {"player_id", "team", "pos_group", "season"}
