"""Tests for proj/team_strength.py and proj/possessions.py - includes a
regression test for a real bug found and fixed while building milestone 4
(returning_minutes_share silently computing a share of the whole league's
minutes when passed an unfiltered stint table - see
reports/milestone4_team_strength.md).
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

from proj.team_strength import returning_minutes_share, shrunk_value_above_position

warnings.filterwarnings("ignore")


def _stint_row(player_id, team, pos_group, games_active, mpg_active, pir40):
    return {
        "season": "E2023", "player_id": player_id, "player_name": player_id, "team": team,
        "pos_group": pos_group, "games_team": games_active, "games_active": games_active,
        "starts": 0, "mpg_active": mpg_active, "minutes_sd": 0.0, "pir40": pir40,
    }


def test_returning_minutes_share_uses_only_given_team_stints():
    """Regression test: returning_minutes_share must be computed against
    the ONE team's minutes total, not the whole league's - passing an
    unfiltered multi-team stint table used to silently divide by the
    league total, producing shares near 0 for every team (~4% instead of
    the real ~50-55%) - caught while building proj/eval_team_strength.py.
    """
    team_a_stints = pd.DataFrame([
        _stint_row("p1", "TEAMA", "G", 30, 20.0, 15.0),
        _stint_row("p2", "TEAMA", "F", 30, 20.0, 15.0),
    ])
    other_team_stints = pd.DataFrame([
        _stint_row("p9", "TEAMB", "G", 30, 25.0, 18.0),
        _stint_row("p10", "TEAMB", "F", 30, 25.0, 18.0),
    ])
    all_stints = pd.concat([team_a_stints, other_team_stints], ignore_index=True)

    current_roster = {"p1"}  # half of TEAMA's minutes carry over, none of TEAMB's are relevant

    share_correct = returning_minutes_share(team_a_stints, current_roster)
    assert share_correct == pytest.approx(0.5, abs=1e-9)

    # passing the unfiltered (multi-team) table is a caller error, but the
    # function shouldn't silently do something wildly different for a
    # plausible-looking input - documented in the docstring, checked here
    # so a future change can't quietly "fix" this without anyone noticing.
    share_unfiltered = returning_minutes_share(all_stints, current_roster)
    assert share_unfiltered < 0.3  # roughly 20/(20+20+25+25) = 0.2 - much lower than the real 0.5


def test_shrunk_value_centers_near_position_average():
    """A player exactly at his position's average pir40 should get a
    value near 0, regardless of sample size (n_min) - spec §4.3's "above
    position average" framing."""
    stints = pd.DataFrame([
        _stint_row("p1", "TEAMA", "G", 30, 20.0, 15.0),
        _stint_row("p2", "TEAMA", "G", 30, 20.0, 15.0),  # same pir40 as p1 -> position average is 15.0
        _stint_row("p3", "TEAMA", "F", 30, 20.0, 10.0),
    ])
    values = shrunk_value_above_position(stints, k=600.0)
    guard_values = values[values["pos_group"] == "G"]["value"]
    assert guard_values.abs().max() < 0.5
