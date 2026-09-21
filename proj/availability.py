"""Player availability: injury-vs-rest run detection and p_active - spec
§3.2.

`injuries.csv`'s numeric `expected_games_missed` override (spec §3.2) is
not implemented here as a historical-backtest feature: reports/data_audit.md
§2 found the local `injuries` table (basketnews.com scrape) only has a
qualitative status, no games-missed count, and it's current-state-only (no
history to backtest against). The live-prediction path (milestone 5) will
instead treat status == "Out" as a binary override (p_active -> 0), the
same convention app.py's /lineup route already uses - see
`apply_out_override` at the bottom of this file.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def team_game_sequence(games: pd.DataFrame, season: str, team: str) -> list[int]:
    """Chronological game_codes for a team's season - the ordering basis
    for run detection. Derived from box-score presence (no separate
    authoritative schedule exists for historical seasons - see
    reports/data_audit.md)."""
    df = games[(games["season_code"] == season) & (games["team"] == team) & (games["played"])]
    df = df.drop_duplicates("game_code").sort_values(["game_date", "game_code"])
    return df["game_code"].tolist()


def player_minutes_sequence(games: pd.DataFrame, season: str, player_id: str, team: str) -> tuple[list[float], list[int]]:
    """Per-team-game minutes for a player's stint, reindexed against every
    team game - a game with no row for this player counts as 0 minutes
    (not in the game-day squad), matching a real absence."""
    team_game_codes = team_game_sequence(games, season, team)
    df = games[(games["season_code"] == season) & (games["team"] == team) & (games["player_id"] == player_id)]
    minutes_by_game = dict(zip(df["game_code"], df["minutes"]))
    sequence = [minutes_by_game.get(gc, 0.0) for gc in team_game_codes]
    return sequence, team_game_codes


def detect_runs(minutes_sequence: list[float], injury_run_min_games: int = 3) -> list[dict]:
    """Runs of consecutive 0-minute team-games. `kind` is 'injury_like'
    (length >= injury_run_min_games) or 'rest' (shorter - coach's
    decision)."""
    runs = []
    i, n = 0, len(minutes_sequence)
    while i < n:
        if minutes_sequence[i] <= 0:
            j = i
            while j < n and minutes_sequence[j] <= 0:
                j += 1
            length = j - i
            kind = "injury_like" if length >= injury_run_min_games else "rest"
            runs.append({"start": i, "end": j - 1, "length": length, "kind": kind})
            i = j
        else:
            i += 1
    return runs


def return_game_indices(runs: list[dict], n_games: int, n_flag: int = 2) -> set[int]:
    """Indices of the up-to-`n_flag` games immediately after an
    injury-like run - down-weighted in the minutes prior (proj/minutes.py)
    since a player easing back from injury isn't playing his normal rate
    yet."""
    flags: set[int] = set()
    for run in runs:
        if run["kind"] != "injury_like":
            continue
        for k in range(1, n_flag + 1):
            idx = run["end"] + k
            if idx < n_games:
                flags.add(idx)
    return flags


def player_return_game_codes(
    games: pd.DataFrame, season: str, player_id: str, team: str,
    injury_run_min_games: int = 3, n_flag: int = 2,
) -> set[int]:
    """game_codes (not indices) flagged as "return from injury" games for
    this player-team stint - the unit proj/minutes.py actually needs to
    down-weight specific games."""
    sequence, team_game_codes = player_minutes_sequence(games, season, player_id, team)
    runs = detect_runs(sequence, injury_run_min_games)
    idxs = return_game_indices(runs, len(sequence), n_flag)
    return {team_game_codes[i] for i in idxs if sequence[i] > 0}  # only flag games actually played


def league_position_rates(stint_tables: dict[str, pd.DataFrame], train_seasons: list[str]) -> dict[str, float]:
    """p_position: games_team-weighted mean per-stint availability rate
    (games_active / games_team) by position group, pooled over train
    seasons - the shrinkage target in compute_p_active."""
    pooled = pd.concat([stint_tables[s] for s in train_seasons], ignore_index=True)
    pooled = pooled[pooled["games_team"] > 0].copy()
    pooled["rate"] = pooled["games_active"] / pooled["games_team"]
    rates = {}
    for pos, g in pooled.groupby("pos_group"):
        rates[pos] = float(np.average(g["rate"], weights=g["games_team"]))
    return rates


def fit_age_slope(
    stint_tables: dict[str, pd.DataFrame], bio: pd.DataFrame, train_seasons: list[str],
    season_start_dates: dict[str, str], min_games_team: int = 10,
) -> tuple[float, float]:
    """Slope of availability rate (games_active/games_team) vs age at
    season start, fit by weighted least squares (weight = games_team) on
    every train-season player-stint with enough games to not be pure
    noise. Returns (slope, age_ref) where age_ref is the weighted-mean age
    in the fitting sample - compute_p_active adds slope * (age - age_ref),
    so a player at the reference age gets no adjustment."""
    pooled = pd.concat([stint_tables[s] for s in train_seasons], ignore_index=True)
    pooled = pooled[pooled["games_team"] >= min_games_team].copy()
    pooled["rate"] = pooled["games_active"] / pooled["games_team"]

    ages = []
    for _, row in pooled.iterrows():
        season_start = season_start_dates.get(row["season"])
        age = None
        if season_start is not None:
            bio_row = bio[bio["player_id"] == row["player_id"]]
            if not bio_row.empty and pd.notna(bio_row.iloc[0]["birth_date"]):
                age = (pd.Timestamp(season_start) - bio_row.iloc[0]["birth_date"]).days / 365.25
        ages.append(age)
    pooled["age"] = ages
    pooled = pooled.dropna(subset=["age"])

    if len(pooled) < 20:
        return 0.0, 27.0  # not enough signal to fit - no adjustment, documented in the report

    w = pooled["games_team"].to_numpy(dtype=float)
    x = pooled["age"].to_numpy(dtype=float)
    y = pooled["rate"].to_numpy(dtype=float)
    age_ref = float(np.average(x, weights=w))
    # weighted least squares, single slope term around the weighted mean age
    xc = x - age_ref
    slope = float(np.sum(w * xc * y) / np.sum(w * xc * xc)) if np.sum(w * xc * xc) > 0 else 0.0
    return slope, age_ref


def compute_p_active(
    stint_tables: dict[str, pd.DataFrame],
    train_seasons: list[str],
    player_id: str,
    pos_group: str,
    position_rates: dict[str, float],
    k: int = 15,
    age: float | None = None,
    age_slope: float = 0.0,
    age_ref: float = 27.0,
    p_min: float = 0.55,
    p_max: float = 0.98,
) -> float:
    total_active, total_games = 0, 0
    for season in train_seasons:
        st = stint_tables[season]
        rows = st[st["player_id"] == player_id]
        total_active += int(rows["games_active"].sum())
        total_games += int(rows["games_team"].sum())

    p_position = position_rates.get(pos_group, float(np.mean(list(position_rates.values()))) if position_rates else 0.8)
    raw = (total_active + k * p_position) / (total_games + k)
    if age is not None:
        raw += age_slope * (age - age_ref)
    return float(np.clip(raw, p_min, p_max))


def apply_out_override(p_active: float, status: str | None) -> float:
    """Live-prediction convention (matches app.py's /lineup route): an
    "Out" status zeroes decision value outright; every other status is
    informational only, not a numeric discount (see
    docs/testing_log.md -> "Injury report scraping")."""
    if status == "Out":
        return 0.0
    return p_active
