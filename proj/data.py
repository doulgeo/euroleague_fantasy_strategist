"""Data loading for proj/: euroleague.db -> clean per-game rows and
per-season player-stint tables.

Reuses engine.db as the single source of truth for ingestion (no duplicate
API-parsing logic here) - this module only reshapes what's already
validated there (see reports/data_audit.md) into the DataFrame shapes the
rest of proj/ needs. Every function here is a pure reshape of what's
already in the DB; nothing here hits the network.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from engine.db import get_connection, load_rows

POSITION_GROUPS = {"Guard": "G", "Forward": "F", "Center": "C"}

# Player-game columns actually needed downstream - keeps the DataFrame
# from carrying 36 columns of box-score detail nothing in proj/ reads yet.
_GAME_COLUMNS = [
    "season_code", "game_code", "round", "game_date", "team", "opponent",
    "home_away", "team_win", "player_id", "player_name", "position",
    "is_starter", "played", "minutes_seconds", "pir_official",
]


def load_games(db_path: str | Path, seasons: list[str]) -> pd.DataFrame:
    """One row per player-game, across the given seasons, minutes in
    minutes (not seconds), position collapsed to G/F/C."""
    conn = get_connection(db_path)
    rows: list[dict] = []
    for season in seasons:
        rows.extend(load_rows(conn, season))
    conn.close()

    df = pd.DataFrame(rows, columns=_GAME_COLUMNS if not rows else None)
    if df.empty:
        return df.reindex(columns=_GAME_COLUMNS + ["minutes", "pos_group"])

    df = df[_GAME_COLUMNS].copy()
    df["minutes"] = df["minutes_seconds"] / 60.0
    df["pos_group"] = df["position"].map(POSITION_GROUPS)
    df["game_date"] = pd.to_datetime(df["game_date"])
    df["played"] = df["played"].astype(bool)
    df["is_starter"] = df["is_starter"].astype(bool)
    return df.sort_values(["season_code", "game_date", "game_code"]).reset_index(drop=True)


def build_stint_table(games: pd.DataFrame, season: str) -> pd.DataFrame:
    """One row per (season, player_id, team) stint - spec §3.1.

    Never merges a mid-season transfer's two stints into one: `team` is
    part of the group key, so a player with two teams in a season (20 real
    cases across E2023-E2025, see reports/data_audit.md §1.2) gets two
    rows here, matching the source data's own shape.
    """
    df = games[games["season_code"] == season].copy()
    if df.empty:
        return pd.DataFrame(columns=[
            "season", "player_id", "player_name", "team", "pos_group",
            "games_team", "games_active", "starts", "mpg_active",
            "minutes_sd", "pir40",
        ])

    played = df[df["played"]]

    active_agg = played.groupby(["player_id", "team"]).agg(
        games_active=("game_code", "count"),
        starts=("is_starter", "sum"),
        mpg_active=("minutes", "mean"),
        minutes_sd=("minutes", lambda s: s.std(ddof=0)),
        pir_sum=("pir_official", "sum"),
        minutes_sum=("minutes", "sum"),
    ).reset_index()

    all_agg = df.groupby(["player_id", "team"]).agg(
        games_team=("game_code", "count"),
        player_name=("player_name", "first"),
        pos_group=("pos_group", "first"),
    ).reset_index()

    stints = all_agg.merge(active_agg, on=["player_id", "team"], how="left")
    for col in ("games_active", "starts"):
        stints[col] = stints[col].fillna(0).astype(int)
    stints["mpg_active"] = stints["mpg_active"].fillna(0.0)
    stints["minutes_sd"] = stints["minutes_sd"].fillna(0.0)
    stints["pir40"] = np.where(
        stints["minutes_sum"].fillna(0) > 0,
        stints["pir_sum"] / stints["minutes_sum"] * 40,
        0.0,
    )
    stints["season"] = season
    return stints.drop(columns=["pir_sum", "minutes_sum"])[
        ["season", "player_id", "player_name", "team", "pos_group",
         "games_team", "games_active", "starts", "mpg_active",
         "minutes_sd", "pir40"]
    ]


def build_all_stint_tables(games: pd.DataFrame, seasons: list[str]) -> dict[str, pd.DataFrame]:
    return {season: build_stint_table(games, season) for season in seasons}


def team_games_per_season(games: pd.DataFrame) -> pd.DataFrame:
    """games_total per (season, team) - the denominator for p_active
    (spec §3.2) and later the team-total minutes target (§3.5)."""
    df = games.drop_duplicates(["season_code", "game_code", "team"])
    return (
        df.groupby(["season_code", "team"])
        .size()
        .reset_index(name="team_games_total")
    )


def opening_day_roster(games: pd.DataFrame, season: str, n_games: int = 3) -> pd.DataFrame:
    """Infer a season's opening roster from each team's first `n_games`
    played games - spec §3.9: "Use opening-day rosters; if no snapshot
    exists, infer rosters from each team's first 3 games and document
    that." No historical roster snapshot exists for E2023-E2025 (the
    `rosters` table only ever holds the current season's live state - see
    reports/data_audit.md), so this is the documented inference path, not
    a fallback of last resort.
    """
    df = games[(games["season_code"] == season) & (games["played"])].copy()
    df = df.sort_values(["team", "game_date", "game_code"])
    first_games = (
        df.drop_duplicates(["team", "game_code"])
        .groupby("team")["game_code"]
        .apply(lambda s: sorted(s.unique())[:n_games])
    )
    keep_games = set()
    for gcs in first_games:
        keep_games.update(gcs)
    early = df[df["game_code"].isin(keep_games)]
    roster = (
        early.groupby(["player_id", "team"])
        .agg(player_name=("player_name", "first"), pos_group=("pos_group", "first"))
        .reset_index()
    )
    roster["season"] = season
    return roster
