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

from engine.ml_features import (
    CATEGORICAL_FEATURE_COLUMNS,
    NUMERIC_FEATURE_COLUMNS,
    build_current_features,
)
from engine.projections import (
    ROLLING_WINDOW,
    TEAM_WIN_WINDOW,
    MIN_GAMES_FOR_PROJECTION,
    WIN_BONUS_FRACTION,
    Projection,
    build_projections,
)

ModelType = Literal["ridge", "gbm"]

_NUM_IDX = list(range(len(NUMERIC_FEATURE_COLUMNS)))
_CAT_IDX = list(range(len(NUMERIC_FEATURE_COLUMNS), len(NUMERIC_FEATURE_COLUMNS) + len(CATEGORICAL_FEATURE_COLUMNS)))


def _season_rank(season_code: str) -> int:
    return int(season_code[1:])  # "E2023" -> 2023


def design_matrix(rows: list[dict]) -> np.ndarray:
    """Feature dicts (from build_feature_table or build_current_features) ->
    a single object-dtype 2D array, numeric columns first then categorical,
    in the exact order _NUM_IDX/_CAT_IDX expect. No pandas dependency."""
    numeric = [[row[c] for c in NUMERIC_FEATURE_COLUMNS] for row in rows]
    categorical = [[str(row[c]) for c in CATEGORICAL_FEATURE_COLUMNS] for row in rows]
    return np.array([n + c for n, c in zip(numeric, categorical)], dtype=object)


def _to_float(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=float)


def build_pipeline(model_type: ModelType) -> Pipeline:
    if model_type == "ridge":
        numeric = Pipeline([
            ("cast", FunctionTransformer(_to_float, feature_names_out="one-to-one")),
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ])
        pre = ColumnTransformer([
            ("num", numeric, _NUM_IDX),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), _CAT_IDX),
        ], verbose_feature_names_out=False)
        return Pipeline([("pre", pre), ("model", Ridge(alpha=1.0))])

    if model_type == "gbm":
        # HistGradientBoostingRegressor handles NaN/unscaled numerics natively.
        pre = ColumnTransformer([
            ("num", FunctionTransformer(_to_float, feature_names_out="one-to-one"), _NUM_IDX),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), _CAT_IDX),
        ], verbose_feature_names_out=False)
        return Pipeline([("pre", pre), ("model", HistGradientBoostingRegressor(
            max_iter=150, max_depth=6, early_stopping=True, random_state=0,
        ))])

    raise ValueError(f"unknown model_type: {model_type!r}")


def train_model(
    feature_table: list[dict],
    as_of_season: str,
    as_of_round: int,
    model_type: ModelType,
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

    X = design_matrix(train_rows)
    y = np.array([r["y"] for r in train_rows], dtype=float)
    pipeline = build_pipeline(model_type)
    pipeline.fit(X, y)
    return pipeline


def build_ml_projections(
    rows: list[dict],
    as_of_round: int,
    model: Pipeline,
    rolling_window: int = ROLLING_WINDOW,
    team_win_window: int = TEAM_WIN_WINDOW,
    min_games: int = MIN_GAMES_FOR_PROJECTION,
) -> dict[str, Projection]:
    """Same eligible-player set and same non-point-estimate fields as
    build_projections - only projected_pir/projected_pir_with_bonus differ,
    coming from `model` instead of the recency-weighted mean."""
    baseline = build_projections(rows, as_of_round, rolling_window, team_win_window, min_games)
    if not baseline:
        return {}

    ordered_ids = list(baseline.keys())
    feats = build_current_features(rows, as_of_round, ordered_ids, rolling_window, team_win_window)
    X = design_matrix([feats[pid] for pid in ordered_ids])
    preds = model.predict(X)

    out: dict[str, Projection] = {}
    for pid, pred in zip(ordered_ids, preds):
        base = baseline[pid]
        pred = float(pred)
        bonus = (base.team_win_rate or 0.0) * WIN_BONUS_FRACTION * pred
        out[pid] = dataclasses.replace(base, projected_pir=pred, projected_pir_with_bonus=pred + bonus)
    return out
