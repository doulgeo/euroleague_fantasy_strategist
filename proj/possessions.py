"""Possessions and pace (spec §4.1) - the shared unit adjusted ratings
(§4.2) and everything downstream in proj/team_strength.py are built on.
"""

from __future__ import annotations

import pandas as pd


def team_game_box(games: pd.DataFrame, ft_coefficient: float = 0.44) -> pd.DataFrame:
    """One row per (season, game_code, team) - team-level box totals plus
    a raw possession estimate: poss = FGA - OREB + TO + ft_coefficient*FTA.
    Requires games to carry fg2a/fg3a/oreb/turnovers/fta/points
    (proj.data.load_games's _GAME_COLUMNS includes these for this module).
    """
    df = games.copy()
    df["fga"] = df["fg2a"] + df["fg3a"]
    grouped = df[df["played"]].groupby(["season_code", "game_code", "round", "team", "opponent", "home_away"]).agg(
        points=("points", "sum"),
        fga=("fga", "sum"),
        oreb=("oreb", "sum"),
        turnovers=("turnovers", "sum"),
        fta=("fta", "sum"),
        minutes=("minutes", "sum"),
    ).reset_index()
    grouped["poss_raw"] = grouped["fga"] - grouped["oreb"] + grouped["turnovers"] + ft_coefficient * grouped["fta"]
    return grouped


def add_paired_possessions(team_box: pd.DataFrame) -> pd.DataFrame:
    """Spec §4.1: possessions "averaged across both teams in a game" -
    joins each team-game row to its opponent's row in the same game and
    takes the mean of the two raw estimates as the game's shared
    possession count (both sides then use this same value)."""
    opp = team_box[["season_code", "game_code", "team", "poss_raw"]].rename(
        columns={"team": "opponent", "poss_raw": "opp_poss_raw"}
    )
    merged = team_box.merge(opp, on=["season_code", "game_code", "opponent"], how="left")
    merged["poss"] = (merged["poss_raw"] + merged["opp_poss_raw"]) / 2
    return merged.drop(columns=["opp_poss_raw"])


def add_ratings_and_pace(team_box: pd.DataFrame) -> pd.DataFrame:
    """pts_per100 (offensive rating for that team-game-side) and
    pace_per40 (normalized for overtime via each team-game's actual total
    on-court minutes, not assumed-40)."""
    out = team_box.copy()
    out["pts_per100"] = out["points"] / out["poss"] * 100
    # a team's own total minutes (5 players x game length) - divide by 5 to get game length in minutes, matching
    # the "per 40" convention regardless of how many overtimes were played.
    game_length_minutes = out["minutes"] / 5.0
    out["pace_per40"] = out["poss"] / game_length_minutes * 40
    return out


def build_team_game_ratings(games: pd.DataFrame, season: str, ft_coefficient: float = 0.44) -> pd.DataFrame:
    """One row per (season, game_code, team) with poss, pts_per100,
    pace_per40, opponent, home_away - the input to
    proj.team_strength.fit_adjusted_ratings."""
    df = games[games["season_code"] == season]
    box = team_game_box(df, ft_coefficient)
    box = add_paired_possessions(box)
    box = add_ratings_and_pace(box)
    return box
