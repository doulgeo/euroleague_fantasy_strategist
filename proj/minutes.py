"""Minutes-when-active prior + ridge correction (spec §3.3), newcomer role
fallback (§3.4), and prediction-uncertainty bands (§3.8).

Season-transition pairs to fit the ridge correction on are scarce by
construction (see reports/data_audit.md §3 and config.yaml's ground-rules
comment): predicting E2024 (fold 1) has ZERO usable prior transitions
(train=[E2023] alone has no season pair, since the pair's own target
season can't be the fold's test season), so fold 1 falls back to the
uncorrected prior. Predicting E2025 (fold 2) has exactly one usable
transition (E2023 -> E2024). This is documented here, not hidden - see
`fit_ridge_correction`'s return when there isn't enough data to fit.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from proj.availability import (
    detect_runs,
    player_minutes_sequence,
    return_game_indices,
)


def weighted_mpg_active(
    games: pd.DataFrame,
    season: str,
    player_id: str,
    team: str,
    downweight: float = 0.5,
    injury_run_min_games: int = 3,
    n_flag: int = 2,
) -> tuple[float, int]:
    """mpg_active for one stint, with the first `n_flag` games back from an
    injury-like absence down-weighted rather than counted at face value
    (spec §3.1/§3.2: those games aren't representative of the player's
    normal rate yet). Returns (weighted_mean, games_active) - games_active
    stays a plain count (used elsewhere for the games-weighted denominator
    in mpg_prior), only the *mean* is down-weighted.
    """
    sequence, _ = player_minutes_sequence(games, season, player_id, team)
    runs = detect_runs(sequence, injury_run_min_games)
    flagged = return_game_indices(runs, len(sequence), n_flag)

    played_idxs = [i for i, m in enumerate(sequence) if m > 0]
    if not played_idxs:
        return 0.0, 0
    weights = [downweight if i in flagged else 1.0 for i in played_idxs]
    minutes = [sequence[i] for i in played_idxs]
    mean = float(np.average(minutes, weights=weights))
    return mean, len(played_idxs)


def player_weighted_history(
    games: pd.DataFrame, stint_tables: dict[str, pd.DataFrame], train_seasons: list[str],
    player_id: str, downweight: float, injury_run_min_games: int, n_flag: int,
) -> list[tuple[str, dict]]:
    """Like proj.backtest._player_history but with injury-down-weighted
    mpg_active instead of the plain stint-table mean, and folding a
    mid-season transfer's stints into one games-weighted entry per season
    (mpg_prior operates at the season level, spec §3.3)."""
    out = []
    for season in train_seasons:
        st = stint_tables[season]
        rows = st[st["player_id"] == player_id]
        if rows.empty:
            continue
        weighted_means, weights = [], []
        for _, r in rows.iterrows():
            mean, n_active = weighted_mpg_active(
                games, season, player_id, r["team"], downweight, injury_run_min_games, n_flag
            )
            if n_active > 0:
                weighted_means.append(mean)
                weights.append(n_active)
        if not weights:
            continue
        mpg = float(np.average(weighted_means, weights=weights))
        out.append((season, {
            "mpg_active": mpg,
            "games_active": int(sum(weights)),
            "pos_group": rows.iloc[0]["pos_group"],
            "starts": int(rows["starts"].sum()),
            "team": rows.iloc[-1]["team"] if len(rows) == 1 else "MULTI",
        }))
    return out


def mpg_prior(
    history: list[tuple[str, dict]],
    season_weights: tuple[float, ...] = (0.6, 0.3, 0.1),
    shrink_games: int = 8,
) -> float | None:
    if not history:
        return None
    recent = list(reversed(history))[:3]
    num, den = 0.0, 0.0
    for w, (_season, row) in zip(season_weights, recent):
        games_active = float(row["games_active"])
        r = games_active / (games_active + shrink_games)
        num += w * r * row["mpg_active"]
        den += w * r
    return num / den if den > 0 else None


def age_bucket(age: float | None) -> int:
    """Ordinal, not one-hot - one coefficient in the ridge model rather
    than four, per the spec's "keep coefficients few" ground rule."""
    if age is None:
        return 1  # unknown -> middle bucket, the least-committal default
    if age < 23:
        return 0
    if age < 28:
        return 1
    if age < 32:
        return 2
    return 3


@dataclass
class RidgeCorrection:
    model: Ridge | None
    alpha: float | None
    coefficients: dict[str, float]
    cv_mse_by_alpha: dict[float, float]
    n_training_pairs: int

    def predict(self, features: dict[str, float]) -> float:
        if self.model is None:
            return features["mpg_prior"]
        x = np.array([[features[c] for c in FEATURE_ORDER]])
        return float(self.model.predict(x)[0])


FEATURE_ORDER = ["mpg_prior", "age_bucket", "team_changed", "start_share", "coach_changed"]


def build_training_pairs(
    games: pd.DataFrame,
    stint_tables: dict[str, pd.DataFrame],
    bio: pd.DataFrame,
    coach_by_season: dict[str, dict[str, str]],
    season_start_dates: dict[str, str],
    from_season: str,
    to_season: str,
    downweight: float,
    injury_run_min_games: int,
    n_flag: int,
) -> pd.DataFrame:
    """One row per player with an active stint in `to_season`, features
    computed only from `from_season` (the single prior season - this is
    what's available for a 1-transition training pair; multi-season
    mpg_prior only applies once >=2 prior seasons exist, i.e. at
    prediction time, not for fitting this correction)."""
    from_st = stint_tables[from_season]
    to_st = stint_tables[to_season][stint_tables[to_season]["games_active"] > 0]

    rows = []
    for _, to_row in to_st.iterrows():
        player_id = to_row["player_id"]
        from_rows = from_st[from_st["player_id"] == player_id]
        if from_rows.empty:
            continue  # no prior-season history -> can't form a training pair (this is what "newcomer" means)

        weighted_means, weights, starts_sum = [], [], 0
        for _, r in from_rows.iterrows():
            mean, n_active = weighted_mpg_active(
                games, from_season, player_id, r["team"], downweight, injury_run_min_games, n_flag
            )
            if n_active > 0:
                weighted_means.append(mean)
                weights.append(n_active)
                starts_sum += r["starts"]
        if not weights:
            continue
        prior_mpg = float(np.average(weighted_means, weights=weights))
        games_active_from = sum(weights)
        prior_team = from_rows.iloc[-1]["team"] if len(from_rows) == 1 else "MULTI"

        bio_row = bio[bio["player_id"] == player_id]
        age = None
        season_start = season_start_dates.get(to_season)
        if not bio_row.empty and pd.notna(bio_row.iloc[0]["birth_date"]) and season_start:
            age = (pd.Timestamp(season_start) - bio_row.iloc[0]["birth_date"]).days / 365.25

        coach_from = coach_by_season.get(from_season, {}).get(prior_team)
        coach_to = coach_by_season.get(to_season, {}).get(to_row["team"])

        rows.append({
            "player_id": player_id,
            "team": to_row["team"],
            "mpg_prior": prior_mpg,
            "age_bucket": age_bucket(age),
            "team_changed": float(prior_team not in (None, "MULTI") and prior_team != to_row["team"]),
            "start_share": (starts_sum / games_active_from) if games_active_from > 0 else 0.0,
            "coach_changed": float(coach_from is not None and coach_to is not None and coach_from != coach_to),
            "mpg_next": to_row["mpg_active"],
            "weight": to_row["games_active"],
        })
    return pd.DataFrame(rows)


def fit_ridge_correction(training_pairs: pd.DataFrame, alpha_grid: list[float]) -> RidgeCorrection:
    """Leave-one-team-out CV over `alpha_grid`. Returns a RidgeCorrection
    with model=None (predict() then just returns mpg_prior unchanged) if
    there isn't enough data to fit anything meaningful - honest fallback
    for fold 1, not a crash."""
    n = len(training_pairs)
    if n < 30:
        return RidgeCorrection(model=None, alpha=None, coefficients={}, cv_mse_by_alpha={}, n_training_pairs=n)

    X = training_pairs[FEATURE_ORDER].to_numpy(dtype=float)
    y = training_pairs["mpg_next"].to_numpy(dtype=float)
    w = training_pairs["weight"].clip(lower=1).to_numpy(dtype=float)
    teams = training_pairs["team"].to_numpy()

    cv_mse: dict[float, float] = {}
    for alpha in alpha_grid:
        sq_errors, weights_used = [], []
        for held_out_team in np.unique(teams):
            train_mask = teams != held_out_team
            test_mask = ~train_mask
            if train_mask.sum() < 5 or test_mask.sum() == 0:
                continue
            model = Ridge(alpha=alpha)
            model.fit(X[train_mask], y[train_mask], sample_weight=w[train_mask])
            preds = model.predict(X[test_mask])
            sq_errors.extend(((preds - y[test_mask]) ** 2).tolist())
            weights_used.extend(w[test_mask].tolist())
        cv_mse[alpha] = float(np.average(sq_errors, weights=weights_used)) if sq_errors else float("inf")

    best_alpha = min(cv_mse, key=cv_mse.get)
    final_model = Ridge(alpha=best_alpha)
    final_model.fit(X, y, sample_weight=w)
    coefficients = dict(zip(FEATURE_ORDER, final_model.coef_.tolist()))
    coefficients["intercept"] = float(final_model.intercept_)

    return RidgeCorrection(
        model=final_model, alpha=best_alpha, coefficients=coefficients,
        cv_mse_by_alpha=cv_mse, n_training_pairs=n,
    )


# --- Newcomers (spec §3.4) ---

def newcomer_prediction(
    role_class_means: dict[str, float], pos_group: str, role_class: str | None, fallback_multiplier: float,
) -> tuple[float, bool]:
    """Returns (predicted_mpg, was_flagged_unknown_role). If role_class is
    given and known, uses its empirical mean; otherwise falls back to
    position-group median x fallback_multiplier with the caller expected
    to widen the interval (spec §3.4) - flagged so it's visible downstream
    rather than silently blended in."""
    if role_class and role_class in role_class_means:
        return role_class_means[role_class], False
    group_key = f"__median__{pos_group}"
    return role_class_means.get(group_key, 0.0) * fallback_multiplier, True


def build_role_class_medians(stint_tables: dict[str, pd.DataFrame], train_seasons: list[str]) -> dict[str, float]:
    """First-year players' empirical mpg by role class isn't computable
    without depth-chart data (no way to know who was a true rookie vs a
    veteran signing from another league in this data) - see
    reports/data_audit.md §2. Every newcomer therefore uses the
    position-group-median fallback path today, honestly reflecting that
    gap rather than fabricating a role-class breakdown from data that
    doesn't distinguish the two cases."""
    pooled = pd.concat([stint_tables[s] for s in train_seasons], ignore_index=True)
    pooled = pooled[pooled["games_active"] > 0]
    out = {}
    for pos, g in pooled.groupby("pos_group"):
        out[f"__median__{pos}"] = float(g["mpg_active"].median())
    return out


# --- Uncertainty (spec §3.8) ---

def residual_bands(residuals_by_class: dict[str, list[float]]) -> dict[str, tuple[float, float]]:
    """p10/p90 of (actual - predicted) per role class, from backtest
    residuals - spec §3.8."""
    return {
        cls: (float(np.percentile(res, 10)), float(np.percentile(res, 90)))
        for cls, res in residuals_by_class.items() if res
    }
