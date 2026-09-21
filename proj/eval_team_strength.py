"""Milestone-4 acceptance evaluation: preseason net-rating projection
(spec §4.3/§4.4) backtested per §4.7, against the two baselines §4.7
specifies (last-season net rating, league mean 0). Run with
`python -m proj.eval_team_strength`.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from proj.coaches import season_primary_coach
from proj.config import load_config
from proj.data import build_all_stint_tables, load_games, opening_day_roster
from proj.possessions import build_team_game_ratings
from proj.team_strength import (
    fit_adjusted_ratings,
    fit_preseason_projection,
    returning_minutes_share,
    shrunk_value_above_position,
    team_projected_value,
)

warnings.filterwarnings("ignore")


def realized_ratings_by_season(games: pd.DataFrame, cfg: dict) -> dict[str, pd.DataFrame]:
    tcfg = cfg["team_strength"]
    out = {}
    for season in cfg["seasons"]["all"]:
        rg = build_team_game_ratings(games, season, tcfg["possessions"]["ft_coefficient"])
        ar = fit_adjusted_ratings(
            rg, tcfg["adjusted_ratings"]["ridge_lambda_grid"],
            recency_half_life=tcfg["adjusted_ratings"]["recency_half_life_games"] if tcfg["adjusted_ratings"]["recency_weighting"] else None,
            bootstrap_resamples=tcfg["adjusted_ratings"]["bootstrap_resamples"],
        )
        out[season] = ar.table.set_index("team")
    return out


def build_transition_row(
    games, stints, realized, coach_by_season, from_season: str, to_season: str, team: str,
) -> dict | None:
    if team not in realized[from_season].index or team not in realized[to_season].index:
        return None
    to_roster = opening_day_roster(games, to_season)
    current_ids = set(to_roster[to_roster["team"] == team]["player_id"])
    if not current_ids:
        return None

    net_last = float(realized[from_season].loc[team, "net"])
    prior_team_stints = stints[from_season][stints[from_season]["team"] == team]
    ret_share = returning_minutes_share(prior_team_stints, current_ids)

    value_table = shrunk_value_above_position(stints[from_season])
    team_roster_df = to_roster[to_roster["team"] == team][["player_id"]]
    team_value = team_projected_value(value_table, team_roster_df)

    coach_from = coach_by_season.get(from_season, {}).get(team)
    coach_to = coach_by_season.get(to_season, {}).get(team)
    coach_changed = float(coach_from is not None and coach_to is not None and coach_from != coach_to)

    return {
        "team": team,
        "net_last": net_last,
        "returning_minutes_share": ret_share,
        "net_last_x_returning_share": net_last * ret_share,
        "team_value": team_value,
        "coach_changed": coach_changed,
        "net_next": float(realized[to_season].loc[team, "net"]),
        "is_new_to_local_data": team not in realized[from_season].index,
    }


def run() -> tuple[pd.DataFrame, dict]:
    cfg = load_config()
    seasons = cfg["seasons"]["all"]
    games = load_games(cfg["db_path"], seasons)
    stints = build_all_stint_tables(games, seasons)
    coach_by_season = {s: season_primary_coach(games, s) for s in seasons}
    realized = realized_ratings_by_season(games, cfg)

    tcfg = cfg["team_strength"]
    folds = [
        {"train_pairs": [], "test": "E2024", "from_season": "E2023"},  # no prior transition to fit on
        {"train_pairs": [("E2023", "E2024")], "test": "E2025", "from_season": "E2024"},
    ]

    results = []
    diagnostics = {}
    for fold in folds:
        test = fold["test"]
        from_season = fold["from_season"]
        all_teams_from = set(realized[from_season].index)
        all_teams_to = set(realized[test].index)

        # training rows for the ridge, drawn from any earlier transition (only E2023->E2024 exists)
        training_rows = []
        for f, t in fold["train_pairs"]:
            for team in set(realized[f].index) & set(realized[t].index):
                row = build_transition_row(games, stints, realized, coach_by_season, f, t, team)
                if row and not row["is_new_to_local_data"]:
                    training_rows.append(row)
        training_df = pd.DataFrame(training_rows)
        proj_model = fit_preseason_projection(training_df, tcfg["preseason_projection"]["ridge_lambda_grid"]) if not training_df.empty else None

        for team in all_teams_to:
            new_to_local = team not in all_teams_from
            if new_to_local:
                row = {
                    "team": team, "net_last": None, "returning_minutes_share": None,
                    "net_last_x_returning_share": 0.0, "team_value": None, "coach_changed": None,
                    "net_next": float(realized[test].loc[team, "net"]), "is_new_to_local_data": True,
                }
                to_roster = opening_day_roster(games, test)
                team_roster_df = to_roster[to_roster["team"] == team][["player_id"]]
                if not team_roster_df.empty and from_season in stints:
                    value_table = shrunk_value_above_position(stints[from_season])
                    row["team_value"] = team_projected_value(value_table, team_roster_df)
                    row["coach_changed"] = 0.0
            else:
                row = build_transition_row(games, stints, realized, coach_by_season, from_season, test, team)
            if row is None:
                continue

            if proj_model is not None and proj_model.model is not None and row["team_value"] is not None:
                features = {k: (v if v is not None else 0.0) for k, v in row.items()}
                pred = proj_model.predict(features, is_new_to_league=new_to_local)
            else:
                pred = (row["net_last"] or 0.0) * (row["returning_minutes_share"] if row["returning_minutes_share"] is not None else 1.0)

            results.append({
                "fold_test": test, "team": team, "is_new_to_local_data": new_to_local,
                "net_last": row["net_last"], "net_next_actual": row["net_next"],
                "net_next_pred": pred, "baseline_last": row["net_last"] if row["net_last"] is not None else 0.0,
                "baseline_mean0": 0.0,
            })
        diagnostics[test] = proj_model

    return pd.DataFrame(results), diagnostics


def summarize(details: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for test, g in details.groupby("fold_test"):
        for pred_col, label in [("net_next_pred", "model"), ("baseline_last", "last_season"), ("baseline_mean0", "league_mean")]:
            sub = g.dropna(subset=[pred_col, "net_next_actual"])
            if sub.empty:
                continue
            err = sub[pred_col] - sub["net_next_actual"]
            rmse = float(np.sqrt(np.mean(err ** 2)))
            rho = spearmanr(sub[pred_col], sub["net_next_actual"]).correlation if len(sub) > 2 else None
            rows.append({"fold_test": test, "predictor": label, "n": len(sub), "rmse": rmse, "spearman": rho})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    details, diagnostics = run()
    summary = summarize(details)
    pd.set_option("display.width", 160)
    print(summary.round(3).to_string(index=False))

    cfg = load_config()
    cache_dir, reports_dir = Path(cfg["cache_dir"]), Path(cfg["reports_dir"])
    details.to_parquet(cache_dir / "team_strength_eval_details.parquet", index=False)
    summary.to_csv(reports_dir / "team_strength_eval_summary.csv", index=False)

    for test, model in diagnostics.items():
        if model is not None:
            print(f"\n{test} preseason-projection ridge: lambda={model.lam} n_team_seasons={model.n_team_seasons}")
            print("  coefficients:", model.coefficients)
