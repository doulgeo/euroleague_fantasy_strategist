"""End-to-end minutes projection: prior + ridge correction (proj/minutes.py)
-> availability (proj/availability.py) -> team allocation + coach
concentration (proj/allocation.py) -> minutes_proj output (spec §3, whole
section). This is what proj/run.py and the milestone-3 backtest both call.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from proj.allocation import (
    apply_tau,
    coach_n_eff_targets,
    expected_minutes_monte_carlo,
    position_group_cap,
    solve_tau,
    team_total_minutes,
)
from proj.availability import compute_p_active, fit_age_slope, league_position_rates
from proj.backtest import _role_tier, build_role_tiers
from proj.minutes import (
    RidgeCorrection,
    age_bucket,
    build_role_class_medians,
    build_training_pairs,
    fit_ridge_correction,
    mpg_prior,
    newcomer_prediction,
    player_weighted_history,
)


def build_minutes_projection(
    games: pd.DataFrame,
    stint_tables: dict[str, pd.DataFrame],
    bio: pd.DataFrame,
    coach_by_season: dict[str, dict[str, str]],
    season_start_dates: dict[str, str],
    train_seasons: list[str],
    test_season: str,
    roster: pd.DataFrame,  # player_id, team, pos_group (opening_day_roster or actual test-season roster)
    cfg: dict,
    ridge_from_to: tuple[str, str] | None = None,
    ridge_feature_cols: list[str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Returns (minutes_proj DataFrame, diagnostics dict) - diagnostics
    carries the ridge fit, tau solves per team, and position-group sanity
    adjustments, for the checkpoint report."""
    mcfg = cfg["minutes"]
    acfg = mcfg["availability"]
    downweight = acfg["return_game_downweight"]
    injury_run_min_games = acfg["injury_run_min_games"]
    n_flag = 2

    position_rates = league_position_rates(stint_tables, train_seasons)
    caps = position_group_cap(stint_tables, train_seasons, mcfg["allocation"]["cap_percentile"])
    role_tiers = build_role_tiers(stint_tables, train_seasons)
    role_medians = build_role_class_medians(stint_tables, train_seasons)

    ridge = RidgeCorrection(model=None, alpha=None, coefficients={}, cv_mse_by_alpha={}, n_training_pairs=0)
    if ridge_from_to is not None:
        pairs = build_training_pairs(
            games, stint_tables, bio, coach_by_season, season_start_dates,
            ridge_from_to[0], ridge_from_to[1], downweight, injury_run_min_games, n_flag,
        )
        ridge = fit_ridge_correction(pairs, mcfg["ridge_correction"]["alpha_grid"], ridge_feature_cols)

    p_active_slope, p_active_age_ref = fit_age_slope(stint_tables, bio, train_seasons, season_start_dates)

    season_start = season_start_dates.get(test_season)
    rows = []
    for _, r in roster.iterrows():
        player_id, team, pos_group = r["player_id"], r["team"], r["pos_group"]
        history = player_weighted_history(games, stint_tables, train_seasons, player_id, downweight, injury_run_min_games, n_flag)

        bio_row = bio[bio["player_id"] == player_id]
        age = None
        if not bio_row.empty and pd.notna(bio_row.iloc[0]["birth_date"]) and season_start:
            age = (pd.Timestamp(season_start) - bio_row.iloc[0]["birth_date"]).days / 365.25

        is_newcomer = len(history) == 0
        if is_newcomer:
            role_class = None  # spec §3.4: no depth-chart data to assign one - see reports/data_audit.md §2
            mpg_pred, flagged_unknown_role = newcomer_prediction(role_medians, pos_group, role_class, mcfg["newcomer"]["role_fallback_multiplier"])
        else:
            prior = mpg_prior(history, tuple(mcfg["prior"]["season_weights"]), mcfg["prior"]["shrink_games"])
            last_row = history[-1][1]
            last_team = last_row["team"]
            features = {
                "mpg_prior": prior,
                "age_bucket": age_bucket(age),
                "team_changed": float(last_team not in (None, "MULTI") and last_team != team),
                "start_share": (last_row["starts"] / last_row["games_active"]) if last_row["games_active"] > 0 else 0.0,
                "coach_changed": float(
                    coach_by_season.get(history[-1][0], {}).get(last_team) is not None
                    and coach_by_season.get(test_season, {}).get(team) is not None
                    and coach_by_season.get(history[-1][0], {}).get(last_team) != coach_by_season.get(test_season, {}).get(team)
                ),
            }
            mpg_pred = ridge.predict(features)
            flagged_unknown_role = False

        cap = caps.get(pos_group, 34.0)
        mpg_pred = float(np.clip(mpg_pred, 0.0, cap))

        p_active = compute_p_active(
            stint_tables, train_seasons, player_id, pos_group, position_rates,
            k=acfg["k"], age=age, age_slope=p_active_slope, age_ref=p_active_age_ref,
            p_min=acfg["p_active_min"], p_max=acfg["p_active_max"],
        )

        role_class = _role_tier(mpg_pred, *role_tiers.get(pos_group, (0.0, 1e9))) if not is_newcomer else "newcomer"

        rows.append({
            "player_id": player_id, "team": team, "pos_group": pos_group,
            "mpg_active_pred": mpg_pred, "p_active": p_active,
            "is_newcomer": is_newcomer, "flagged_unknown_role": flagged_unknown_role,
            "role_class": role_class,
        })

    proj_df = pd.DataFrame(rows)

    # --- team allocation (spec §3.5) + coach concentration (§3.6) ---
    coach_targets, league_mean_n_eff = coach_n_eff_targets(games, coach_by_season, train_seasons)
    tau_cfg = mcfg["coach_concentration"]
    draws = mcfg["allocation"]["monte_carlo_draws"]
    seed = cfg["random_seed"]

    exp_minutes = np.zeros(len(proj_df))
    tau_log = []
    for team, idx in proj_df.groupby("team").groups.items():
        idx = list(idx)
        sub = proj_df.loc[idx]
        raw = sub["mpg_active_pred"].to_numpy(dtype=float)
        cap_arr = sub["pos_group"].map(caps).fillna(34.0).to_numpy(dtype=float)
        p_active_arr = sub["p_active"].to_numpy(dtype=float)
        total = team_total_minutes(games, train_seasons, team)

        coach_name = coach_by_season.get(test_season, {}).get(team)
        target_n_eff = coach_targets.get(coach_name, league_mean_n_eff) if coach_name else league_mean_n_eff
        tau = solve_tau(raw, target_n_eff, tau_cfg["tau_min"], tau_cfg["tau_max"], tau_cfg["tau_search_steps"])
        raw_tau = apply_tau(raw, tau)
        tau_log.append({"team": team, "tau": tau, "target_n_eff": target_n_eff})

        team_exp = expected_minutes_monte_carlo(raw_tau, p_active_arr, cap_arr, total, draws=draws, seed=seed)
        exp_minutes[[proj_df.index.get_loc(i) for i in idx]] = team_exp

    proj_df["exp_minutes"] = exp_minutes

    diagnostics = {"ridge": ridge, "tau_log": pd.DataFrame(tau_log), "p_active_age_slope": p_active_slope, "p_active_age_ref": p_active_age_ref}
    return proj_df, diagnostics
