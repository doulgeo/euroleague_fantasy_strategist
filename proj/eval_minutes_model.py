"""Milestone-3 acceptance evaluation: the full minutes pipeline
(proj/pipeline.py) against the same fold/segment/baseline framework as
milestone 2 (proj/backtest.py), so the comparison is apples-to-apples.
Run with `python -m proj.eval_minutes_model`.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from proj.allocation import allocate_minutes, position_group_cap, team_total_minutes
from proj.backtest import FOLDS, evaluate_fold, summarize_fold
from proj.bio import build_player_bio
from proj.coaches import season_primary_coach
from proj.config import load_config
from proj.data import build_all_stint_tables, load_games, opening_day_roster
from proj.pipeline import build_minutes_projection

warnings.filterwarnings("ignore")

SEASON_START_DATES = {"E2023": "2023-09-28", "E2024": "2024-09-26", "E2025": "2025-09-30"}


def run(ridge_feature_cols: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    cfg = load_config()
    seasons = cfg["seasons"]["all"]
    games = load_games(cfg["db_path"], seasons)
    stints = build_all_stint_tables(games, seasons)
    bio = build_player_bio(cache_path=Path(cfg["cache_dir"]) / "player_bio.parquet", fetch_live=True)
    coach_by_season = {s: season_primary_coach(games, s) for s in seasons}
    caps = position_group_cap(stints, seasons[:-1], cfg["minutes"]["allocation"]["cap_percentile"])

    all_summaries = []
    all_details = []
    diagnostics = {}

    for fold in FOLDS:
        train, test = fold["train"], fold["test"]
        ridge_from_to = (train[0], train[1]) if len(train) >= 2 else None

        roster = opening_day_roster(games, test)
        proj_df, diag = build_minutes_projection(
            games, stints, bio, coach_by_season, SEASON_START_DATES, train, test, roster, cfg,
            ridge_from_to, ridge_feature_cols,
        )

        # deterministic single-allocation variant (no Monte Carlo / p_active) - diagnostic, kept in the report
        team_caps = position_group_cap(stints, train, cfg["minutes"]["allocation"]["cap_percentile"])
        no_mc = np.zeros(len(proj_df))
        for team, idx in proj_df.groupby("team").groups.items():
            idx = list(idx)
            sub = proj_df.loc[idx]
            raw = sub["mpg_active_pred"].to_numpy(dtype=float)
            cap_arr = sub["pos_group"].map(team_caps).fillna(34.0).to_numpy(dtype=float)
            total = team_total_minutes(games, train, team)
            m = allocate_minutes(raw, cap_arr, min(total, cap_arr.sum()))
            no_mc[[proj_df.index.get_loc(i) for i in idx]] = m
        proj_df["no_mc_alloc"] = no_mc

        base_fdf = evaluate_fold(stints, train, test, tuple(cfg["minutes"]["prior"]["season_weights"]), cfg["minutes"]["prior"]["shrink_games"])
        merged = base_fdf.merge(
            proj_df[["player_id", "mpg_active_pred", "exp_minutes", "no_mc_alloc", "p_active", "is_newcomer" ]].rename(columns={"is_newcomer": "model_is_newcomer"}),
            on="player_id", how="left",
        )
        merged["fold_test"] = test
        all_details.append(merged)

        summary = summarize_fold(merged, pred_cols=("b0", "b1", "b2", "mpg_active_pred", "exp_minutes", "no_mc_alloc"))
        summary.insert(0, "fold_test", test)
        all_summaries.append(summary)

        diagnostics[test] = diag

    details = pd.concat(all_details, ignore_index=True)
    summary = pd.concat(all_summaries, ignore_index=True)
    return details, summary, diagnostics


if __name__ == "__main__":
    details, summary, diagnostics = run()
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    cols = ["fold_test", "segment", "n"] + [c for c in summary.columns if "mae" in c or "bias" in c]
    print(summary[cols].round(2).to_string(index=False))

    cfg = load_config()
    cache_dir = Path(cfg["cache_dir"])
    reports_dir = Path(cfg["reports_dir"])
    details.to_parquet(cache_dir / "minutes_model_eval_details.parquet", index=False)
    summary.to_csv(reports_dir / "minutes_model_eval_summary.csv", index=False)

    for test_season, diag in diagnostics.items():
        print(f"\n{test_season} ridge: alpha={diag['ridge'].alpha} n_pairs={diag['ridge'].n_training_pairs}")
        print("  coefficients:", diag["ridge"].coefficients)
        print(f"  p_active age slope={diag['p_active_age_slope']:.4f} age_ref={diag['p_active_age_ref']:.1f}")
