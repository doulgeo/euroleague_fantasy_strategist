"""
Regression-model projections: a drop-in replacement for
engine.projections.build_projections's point estimate, trained on the
leakage-safe feature table from engine.ml_features.

Design: build_projections is called first and supplies every Projection
field except projected_pir/projected_pir_with_bonus (games_sampled,
player_name, position, team, minutes_trend_seconds, volatility,
team_win_rate - all already correct and already tested). Only the point
estimate itself is swapped from "recency-weighted mean" to
"model.predict(features)", reusing the exact same win-bonus formula. This
keeps the ML path a true drop-in for build_projections everywhere a
dict[str, Projection] is consumed (engine.roster, engine.lineup,
engine.transfers, backtest_eval).

Feature set and model hyperparameters are both configurable (see
FEATURE_SETS in engine.ml_features, and build_pipeline's **model_kwargs)
so a caller can grid-search hyperparameters or A/B a smaller feature subset
without touching this module - see ml_grid_search.py.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from engine.ml_features import FEATURE_SETS, build_current_features
from engine.projections import (
    ROLLING_WINDOW,
    TEAM_WIN_WINDOW,
    MIN_GAMES_FOR_PROJECTION,
    WIN_BONUS_FRACTION,
    Projection,
    build_projections,
)

ModelType = Literal["ridge", "gbm"]

# Defaults, chosen by ml_grid_search.py's fixed-split sweep (see
# docs/testing_log.md for the full table) - override via build_pipeline's
# **model_kwargs to revisit. Ridge's alpha barely moved MAE across a 1000x
# range (0.1 to 100) - 100.0 edges out the sweep but the difference is inside
# noise, not a real regularization effect. GBM's max_depth=6/lr=0.1 (the
# original untuned guess) was clearly worse than shallower/slower trees.
RIDGE_DEFAULT_ALPHA = 100.0
GBM_DEFAULTS = dict(max_iter=150, max_depth=3, learning_rate=0.05, early_stopping=True, random_state=0)


def _season_rank(season_code: str) -> int:
    return int(season_code[1:])  # "E2023" -> 2023


def resolve_feature_set(feature_set: str) -> tuple[list[str], list[str]]:
    return FEATURE_SETS[feature_set]


def design_matrix(rows: list[dict], feature_set: str = "full") -> np.ndarray:
    """Feature dicts (from build_feature_table or build_current_features) ->
    a single object-dtype 2D array, numeric columns first then categorical.
    No pandas dependency. `feature_set` must match whatever the consuming
    model was trained with (see FEATURE_SETS in engine.ml_features)."""
    numeric_cols, categorical_cols = resolve_feature_set(feature_set)
    numeric = [[row[c] for c in numeric_cols] for row in rows]
    categorical = [[str(row[c]) for c in categorical_cols] for row in rows]
    return np.array([n + c for n, c in zip(numeric, categorical)], dtype=object)


def _to_float(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=float)


def build_pipeline(model_type: ModelType, feature_set: str = "full", **model_kwargs) -> Pipeline:
    numeric_cols, categorical_cols = resolve_feature_set(feature_set)
    num_idx = list(range(len(numeric_cols)))
    cat_idx = list(range(len(numeric_cols), len(numeric_cols) + len(categorical_cols)))

    if model_type == "ridge":
        alpha = model_kwargs.get("alpha", RIDGE_DEFAULT_ALPHA)
        numeric = Pipeline([
            ("cast", FunctionTransformer(_to_float, feature_names_out="one-to-one")),
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ])
        pre = ColumnTransformer([
            ("num", numeric, num_idx),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), cat_idx),
        ], verbose_feature_names_out=False)
        return Pipeline([("pre", pre), ("model", Ridge(alpha=alpha))])

    if model_type == "gbm":
        params = {**GBM_DEFAULTS, **model_kwargs}
        # HistGradientBoostingRegressor handles NaN/unscaled numerics natively.
        pre = ColumnTransformer([
            ("num", FunctionTransformer(_to_float, feature_names_out="one-to-one"), num_idx),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), cat_idx),
        ], verbose_feature_names_out=False)
        return Pipeline([("pre", pre), ("model", HistGradientBoostingRegressor(**params))])

    raise ValueError(f"unknown model_type: {model_type!r}")


def train_model(
    feature_table: list[dict],
    as_of_season: str,
    as_of_round: int,
    model_type: ModelType,
    feature_set: str = "full",
    **model_kwargs,
) -> Pipeline:
    """feature_table is the full multi-season output of
    engine.ml_features.build_feature_table, computed ONCE up front. Training
    rows = every strictly-prior season in full, plus as_of_season's own
    rounds < as_of_round - the cross-season pooling the heuristic can't do."""
    cutoff_rank = _season_rank(as_of_season)
    train_rows = [
        r for r in feature_table
        if _season_rank(r["season_code"]) < cutoff_rank
        or (r["season_code"] == as_of_season and r["round"] < as_of_round)
    ]
    if not train_rows:
        raise ValueError(f"no training rows available before {as_of_season} round {as_of_round}")

    X = design_matrix(train_rows, feature_set)
    y = np.array([r["y"] for r in train_rows], dtype=float)
    pipeline = build_pipeline(model_type, feature_set, **model_kwargs)
    pipeline.fit(X, y)
    return pipeline


def build_ml_projections(
    rows: list[dict],
    as_of_round: int,
    model: Pipeline,
    feature_set: str = "full",
    rolling_window: int = ROLLING_WINDOW,
    team_win_window: int = TEAM_WIN_WINDOW,
    min_games: int = MIN_GAMES_FOR_PROJECTION,
) -> dict[str, Projection]:
    """Same eligible-player set and same non-point-estimate fields as
    build_projections - only projected_pir/projected_pir_with_bonus differ,
    coming from `model` instead of the recency-weighted mean. `feature_set`
    must match what `model` was trained with."""
    baseline = build_projections(rows, as_of_round, rolling_window, team_win_window, min_games)
    if not baseline:
        return {}

    ordered_ids = list(baseline.keys())
    feats = build_current_features(rows, as_of_round, ordered_ids, rolling_window, team_win_window)
    X = design_matrix([feats[pid] for pid in ordered_ids], feature_set)
    preds = model.predict(X)

    out: dict[str, Projection] = {}
    for pid, pred in zip(ordered_ids, preds):
        base = baseline[pid]
        pred = float(pred)
        bonus = (base.team_win_rate or 0.0) * WIN_BONUS_FRACTION * pred
        out[pid] = dataclasses.replace(base, projected_pir=pred, projected_pir_with_bonus=pred + bonus)
    return out


def build_ensemble_projections(
    rows: list[dict],
    as_of_round: int,
    models: dict[str, Pipeline],
    feature_set: str = "full",
    include_heuristic: bool = True,
    rolling_window: int = ROLLING_WINDOW,
    team_win_window: int = TEAM_WIN_WINDOW,
    min_games: int = MIN_GAMES_FOR_PROJECTION,
) -> dict[str, Projection]:
    """Equal-weight average of the heuristic's own projected_pir (if
    include_heuristic) and every model in `models` (e.g. {"ridge": ..,
    "gbm": ..}), each on the same eligible-player set. Averaging happens on
    the raw point estimate BEFORE the win bonus is applied (once, on the
    averaged value) - applying the bonus per-source first and then averaging
    would double-count team_win_rate's effect for no reason, since it's the
    same team_win_rate for every source."""
    baseline = build_projections(rows, as_of_round, rolling_window, team_win_window, min_games)
    if not baseline:
        return {}

    ordered_ids = list(baseline.keys())
    predictions: list[np.ndarray] = []

    if include_heuristic:
        predictions.append(np.array([baseline[pid].projected_pir for pid in ordered_ids], dtype=float))

    if models:
        feats = build_current_features(rows, as_of_round, ordered_ids, rolling_window, team_win_window)
        X = design_matrix([feats[pid] for pid in ordered_ids], feature_set)
        for model in models.values():
            predictions.append(np.asarray(model.predict(X), dtype=float))

    if not predictions:
        raise ValueError("build_ensemble_projections needs at least the heuristic or one model")

    ensemble_pred = np.mean(np.vstack(predictions), axis=0)

    out: dict[str, Projection] = {}
    for pid, pred in zip(ordered_ids, ensemble_pred):
        base = baseline[pid]
        pred = float(pred)
        bonus = (base.team_win_rate or 0.0) * WIN_BONUS_FRACTION * pred
        out[pid] = dataclasses.replace(base, projected_pir=pred, projected_pir_with_bonus=pred + bonus)
    return out
