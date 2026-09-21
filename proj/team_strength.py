"""Adjusted team ratings (spec §4.2), preseason net-rating projection
(§4.3-§4.4), the game-level margin model (§4.5), and matchup multipliers
(§4.6). Independent of proj/minutes.py - see reports/data_audit.md §1.3
for why this is a second, more rigorous attempt at the same idea
`engine/team_strength.py` already tried once (and shelved for lack of
predictive value).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge


# --- §4.2 Adjusted ratings per season ---

@dataclass
class AdjustedRatings:
    table: pd.DataFrame  # team, off_rating, def_rating, net, pace, off_se, def_se
    lam: float
    cv_mse_by_lambda: dict[float, float]
    league_avg_pts_per100: float


def _design_matrix(ratings: pd.DataFrame) -> tuple[np.ndarray, list[str], list[str]]:
    offense_dummies = pd.get_dummies(ratings["team"], prefix="off")
    defense_dummies = pd.get_dummies(ratings["opponent"], prefix="def")
    home = (ratings["home_away"] == "home").astype(float).to_numpy().reshape(-1, 1)
    X = np.hstack([offense_dummies.to_numpy(dtype=float), defense_dummies.to_numpy(dtype=float), home])
    return X, list(offense_dummies.columns), list(defense_dummies.columns)


def fit_adjusted_ratings(
    ratings: pd.DataFrame,
    lambda_grid: list[float],
    recency_half_life: float | None = None,
    bootstrap_resamples: int = 0,
    seed: int = 1,
) -> AdjustedRatings:
    """Ridge regression of pts_per100 on offense-team dummy + defense
    (opponent)-team dummy + home indicator - spec §4.2. Both dummy sets
    are one-hot with no dropped reference level; ridge's L2 penalty (not
    OLS) is what makes this identifiable, since every team appears as
    both an offense and a defense side roughly equally often across a
    round-robin schedule (the standard "regularized adjusted plus-minus"
    trick - not something spelled out in the spec text, but necessary for
    this exact design to have a unique solution).
    """
    X, off_cols, def_cols = _design_matrix(ratings)
    y = ratings["pts_per100"].to_numpy(dtype=float)
    rounds = ratings["round"].to_numpy()

    weights = np.ones(len(ratings))
    if recency_half_life:
        max_round = ratings["round"].max()
        age = (max_round - ratings["round"]).to_numpy(dtype=float)
        weights = 0.5 ** (age / recency_half_life)

    cv_mse: dict[float, float] = {}
    for lam in lambda_grid:
        sq_errors, w_used = [], []
        for held_out_round in np.unique(rounds):
            train_mask = rounds != held_out_round
            test_mask = ~train_mask
            if train_mask.sum() < 10 or test_mask.sum() == 0:
                continue
            model = Ridge(alpha=lam)
            model.fit(X[train_mask], y[train_mask], sample_weight=weights[train_mask])
            preds = model.predict(X[test_mask])
            sq_errors.extend(((preds - y[test_mask]) ** 2).tolist())
            w_used.extend(weights[test_mask].tolist())
        cv_mse[lam] = float(np.average(sq_errors, weights=w_used)) if sq_errors else float("inf")

    best_lambda = min(cv_mse, key=cv_mse.get)
    final_model = Ridge(alpha=best_lambda)
    final_model.fit(X, y, sample_weight=weights)

    n_off = len(off_cols)
    off_coefs = dict(zip([c.replace("off_", "") for c in off_cols], final_model.coef_[:n_off]))
    def_coefs = dict(zip([c.replace("def_", "") for c in def_cols], final_model.coef_[n_off:n_off + len(def_cols)]))
    league_avg = float(np.average(y, weights=weights))

    pace = ratings.groupby("team")["pace_per40"].mean()

    boot_off: dict[str, list[float]] = {t: [] for t in off_coefs}
    boot_def: dict[str, list[float]] = {t: [] for t in def_coefs}
    if bootstrap_resamples > 0:
        rng = np.random.default_rng(seed)
        game_ids = ratings[["season_code", "game_code"]].drop_duplicates()
        n_games = len(game_ids)
        for _ in range(bootstrap_resamples):
            sampled = game_ids.sample(n=n_games, replace=True, random_state=rng.integers(0, 2**31 - 1))
            boot_rows = sampled.merge(ratings, on=["season_code", "game_code"], how="left")
            Xb, off_cols_b, def_cols_b = _design_matrix(boot_rows)
            # align columns to the full-sample dummy set (a resample can miss a team entirely by chance)
            Xb_full = np.zeros((len(boot_rows), len(off_cols) + len(def_cols) + 1))
            off_index = {c: i for i, c in enumerate(off_cols)}
            def_index = {c: i for i, c in enumerate(def_cols)}
            for j, c in enumerate(off_cols_b):
                if c in off_index:
                    Xb_full[:, off_index[c]] = Xb[:, j]
            for j, c in enumerate(def_cols_b):
                if c in def_index:
                    Xb_full[:, n_off + def_index[c]] = Xb[:, len(off_cols_b) + j]
            Xb_full[:, -1] = Xb[:, -1]
            yb = boot_rows["pts_per100"].to_numpy(dtype=float)
            bm = Ridge(alpha=best_lambda)
            bm.fit(Xb_full, yb)
            for i, t in enumerate([c.replace("off_", "") for c in off_cols]):
                boot_off[t].append(bm.coef_[i])
            for i, t in enumerate([c.replace("def_", "") for c in def_cols]):
                boot_def[t].append(bm.coef_[n_off + i])

    rows = []
    for team in off_coefs:
        off_rating = league_avg + off_coefs[team]
        def_rating = league_avg - def_coefs.get(team, 0.0)  # points ALLOWED per 100, lower = better defense
        rows.append({
            "team": team,
            "off_rating": off_rating,
            "def_rating": def_rating,
            "net": off_rating - def_rating,
            "pace": float(pace.get(team, ratings["pace_per40"].mean())),
            "off_se": float(np.std(boot_off[team])) if boot_off.get(team) else None,
            "def_se": float(np.std(boot_def[team])) if boot_def.get(team) else None,
        })

    return AdjustedRatings(table=pd.DataFrame(rows), lam=best_lambda, cv_mse_by_lambda=cv_mse, league_avg_pts_per100=league_avg)


# --- §4.3 Preseason projection (team-season level) ---

def returning_minutes_share(prior_stints: pd.DataFrame, current_roster_player_ids: set[str]) -> float:
    """Share of last season's team minutes played by players on the
    current roster. `prior_stints` MUST already be filtered to the one
    team in question (e.g. `stints[from_season]` sliced to `team == X`) -
    passing the full league stint table silently computes something else
    (returning minutes as a share of the whole LEAGUE's minutes, not the
    team's) - caught exactly this way while building
    proj/eval_team_strength.py, see reports/milestone4_team_strength.md."""
    prior_stints = prior_stints[prior_stints["games_active"] > 0]
    total = (prior_stints["mpg_active"] * prior_stints["games_active"]).sum()
    if total <= 0:
        return 0.0
    returning = prior_stints[prior_stints["player_id"].isin(current_roster_player_ids)]
    returning_minutes = (returning["mpg_active"] * returning["games_active"]).sum()
    return float(returning_minutes / total)


def shrunk_value_above_position(stints: pd.DataFrame, k: float = 600.0) -> pd.DataFrame:
    """value_i = shrunk PIR/40 above position average - spec §4.3.
    shrunk_pir40 = (n_min*pir40 + k*pir40_pos) / (n_min + k); value is
    that minus the (unshrunk) position-group average pir40, so an average
    player at his position lands near 0."""
    df = stints[stints["games_active"] > 0].copy()
    df["n_min"] = df["mpg_active"] * df["games_active"]
    pos_avg = df.groupby("pos_group").apply(
        lambda g: float(np.average(g["pir40"], weights=g["n_min"])) if g["n_min"].sum() > 0 else float(g["pir40"].mean()),
        include_groups=False,
    )
    df["pir40_pos"] = df["pos_group"].map(pos_avg)
    df["shrunk_pir40"] = (df["n_min"] * df["pir40"] + k * df["pir40_pos"]) / (df["n_min"] + k)
    df["value"] = df["shrunk_pir40"] - df["pir40_pos"]
    return df[["player_id", "team", "pos_group", "n_min", "value"]]


def team_projected_value(
    value_table: pd.DataFrame, roster: pd.DataFrame, league_avg_newcomer_residual: float = 0.0
) -> float:
    """sum_i(share_i * value_i) for one team - spec §4.3. share_i is each
    roster player's share of the team's TRAILING minutes (their own
    n_min from `value_table` if they have history; a newcomer with no
    n_min gets a small fixed weight, see below) - a self-contained proxy
    that doesn't require proj.minutes' full projection (kept independent,
    per the ground rules' "narrow interface" between the two features).
    Newcomers use league-average value minus the average first-year
    residual (spec's own wording) - `league_avg_newcomer_residual` is
    that adjustment, computed once by the caller from backtest residuals.
    """
    merged = roster.merge(value_table[["player_id", "value", "n_min"]], on="player_id", how="left")
    league_avg_value = float(value_table["value"].mean()) if not value_table.empty else 0.0
    newcomer_value = league_avg_value - league_avg_newcomer_residual
    merged["value"] = merged["value"].fillna(newcomer_value)
    # newcomers get a small fixed weight (median n_min among rows with <600 min, i.e. the low-sample-size regime this k already targets) rather than 0
    fallback_weight = float(value_table["n_min"].median()) if not value_table.empty else 1.0
    merged["n_min"] = merged["n_min"].fillna(fallback_weight)
    total_weight = merged["n_min"].sum()
    if total_weight <= 0:
        return 0.0
    return float((merged["n_min"] * merged["value"]).sum() / total_weight)


@dataclass
class PreseasonProjection:
    model: Ridge | None
    lam: float | None
    coefficients: dict[str, float]
    cv_mse_by_lambda: dict[float, float]
    n_team_seasons: int
    feature_means: dict[str, float]
    feature_stds: dict[str, float]

    def predict(self, features: dict[str, float], is_new_to_league: bool = False) -> float:
        if self.model is None:
            return features.get("net_last", 0.0) * features.get("returning_minutes_share", 1.0)
        cols = FEATURE_ORDER_NO_B1 if is_new_to_league else FEATURE_ORDER
        x = []
        for c in cols:
            v = features.get(c, 0.0)
            x.append((v - self.feature_means[c]) / self.feature_stds[c] if self.feature_stds[c] > 0 else 0.0)
        coefs = [self.coefficients[c] for c in cols]
        return float(self.coefficients["intercept"] + np.dot(x, coefs))


FEATURE_ORDER = ["net_last_x_returning_share", "team_value", "coach_changed"]
FEATURE_ORDER_NO_B1 = ["team_value", "coach_changed"]  # spec §4.4: every term except b1 for teams new to the league


def fit_preseason_projection(training_rows: pd.DataFrame, lambda_grid: list[float]) -> PreseasonProjection:
    """Ridge with standardized inputs, leave-one-team-out CV - spec §4.3.
    `training_rows` columns: team, net_last_x_returning_share, team_value,
    coach_changed, net_next (target)."""
    n = len(training_rows)
    means = {c: float(training_rows[c].mean()) for c in FEATURE_ORDER}
    stds = {c: float(training_rows[c].std(ddof=0)) for c in FEATURE_ORDER}
    if n < 15:
        return PreseasonProjection(model=None, lam=None, coefficients={}, cv_mse_by_lambda={}, n_team_seasons=n, feature_means=means, feature_stds=stds)

    Xz = np.column_stack([
        (training_rows[c] - means[c]) / stds[c] if stds[c] > 0 else np.zeros(n) for c in FEATURE_ORDER
    ])
    y = training_rows["net_next"].to_numpy(dtype=float)
    teams = training_rows["team"].to_numpy()

    cv_mse = {}
    for lam in lambda_grid:
        sq_errors = []
        for held_out in np.unique(teams):
            train_mask = teams != held_out
            test_mask = ~train_mask
            if train_mask.sum() < 5 or test_mask.sum() == 0:
                continue
            model = Ridge(alpha=lam)
            model.fit(Xz[train_mask], y[train_mask])
            preds = model.predict(Xz[test_mask])
            sq_errors.extend(((preds - y[test_mask]) ** 2).tolist())
        cv_mse[lam] = float(np.mean(sq_errors)) if sq_errors else float("inf")

    best_lambda = min(cv_mse, key=cv_mse.get)
    final_model = Ridge(alpha=best_lambda)
    final_model.fit(Xz, y)
    coefficients = dict(zip(FEATURE_ORDER, final_model.coef_.tolist()))
    coefficients["intercept"] = float(final_model.intercept_)

    return PreseasonProjection(
        model=final_model, lam=best_lambda, coefficients=coefficients, cv_mse_by_lambda=cv_mse,
        n_team_seasons=n, feature_means=means, feature_stds=stds,
    )


# --- §4.5 Game-level model ---

def expected_margin(net_a: float, net_b: float, pace: float, home_court: float) -> float:
    return (net_a - net_b) * pace / 100 + home_court


def fit_blowout_logistic(net_diffs: np.ndarray, is_blowout: np.ndarray) -> LogisticRegression:
    """P(|margin| > 15) via logistic regression on |net difference| -
    spec §4.5."""
    model = LogisticRegression()
    model.fit(np.abs(net_diffs).reshape(-1, 1), is_blowout)
    return model


def fit_minutes_multiplier(games: pd.DataFrame, role_class_col: str = "role_class") -> dict[str, tuple[float, float]]:
    """Starter minutes vs |final margin| on box scores, per role class -
    spec §4.5's minutes_multiplier(role_class, expected_abs_margin).
    Returns {role_class: (intercept, slope)} from an OLS-via-numpy fit of
    minutes ~ |margin|, so minutes_multiplier can be evaluated as
    intercept + slope * expected_abs_margin at prediction time."""
    out = {}
    for role, g in games.groupby(role_class_col):
        if len(g) < 20:
            continue
        x = g["abs_margin"].to_numpy(dtype=float)
        y = g["minutes"].to_numpy(dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        out[role] = (float(intercept), float(slope))
    return out


def minutes_multiplier(role_class: str, expected_abs_margin: float, fitted: dict[str, tuple[float, float]], baseline_minutes: float) -> float:
    if role_class not in fitted or baseline_minutes <= 0:
        return 1.0
    intercept, slope = fitted[role_class]
    predicted = intercept + slope * expected_abs_margin
    return float(predicted / baseline_minutes)


# --- §4.6 Matchup multipliers ---

def matchup_pir_allowed(games: pd.DataFrame, shrink_games: int = 10) -> pd.DataFrame:
    """PIR allowed per 40 by (opponent, position group), shrunk toward
    the league average with k=shrink_games - spec §4.6."""
    df = games[games["played"]].copy()
    df["pir_per40"] = df["pir_official"] / df["minutes"].clip(lower=1) * 40
    league_avg = df.groupby("pos_group")["pir_per40"].mean()

    grouped = df.groupby(["opponent", "pos_group"]).agg(
        games_faced=("game_code", "nunique"), raw_mean=("pir_per40", "mean")
    ).reset_index()
    grouped["league_avg"] = grouped["pos_group"].map(league_avg)
    grouped["shrunk"] = (
        grouped["games_faced"] * grouped["raw_mean"] + shrink_games * grouped["league_avg"]
    ) / (grouped["games_faced"] + shrink_games)
    grouped["multiplier"] = grouped["shrunk"] / grouped["league_avg"]
    return grouped.rename(columns={"opponent": "team"})
