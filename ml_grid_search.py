"""
Hyperparameter grid search for the regression projections, using the same
cheap fixed train/test split as ml_sanity_check.py (train E2023+E2024, test
E2025) rather than the expensive full walk-forward backtest - this is a
model-selection step, not the final evaluation.

Sweeps:
  - Ridge: alpha (regularization strength) - motivated by Ridge's largest
    coefficients in the sanity check being team dummies, suggesting alpha=1.0
    (sklearn's default, never actually tuned) might be under-regularizing.
  - GBM: max_depth x learning_rate.

Also reports each combo on the "minimal" feature set (see
engine.ml_features.FEATURE_SETS) alongside "full", to see whether the
smaller, heuristic-like feature subset is competitive.

Usage:
    python ml_grid_search.py
"""

from __future__ import annotations

import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from engine.db import get_connection, load_rows
from engine.ml_features import build_feature_table
from engine.ml_projections import build_pipeline, design_matrix

RIDGE_ALPHAS = [0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0]
GBM_MAX_DEPTHS = [3, 6, 9]
GBM_LEARNING_RATES = [0.05, 0.1, 0.2]


def main() -> None:
    conn = get_connection()
    rows = load_rows(conn)
    conn.close()

    table = build_feature_table(rows)
    train = [r for r in table if r["season_code"] in ("E2023", "E2024")]
    test = [r for r in table if r["season_code"] == "E2025"]
    y_train = np.array([r["y"] for r in train], dtype=float)
    y_test = np.array([r["y"] for r in test], dtype=float)
    print(f"train rows: {len(train)}, test rows: {len(test)}\n")

    def fit_and_score(model_type: str, feature_set: str, **kwargs) -> tuple[float, float, float]:
        X_train = design_matrix(train, feature_set)
        X_test = design_matrix(test, feature_set)
        model = build_pipeline(model_type, feature_set, **kwargs).fit(X_train, y_train)
        preds = model.predict(X_test)
        mae = mean_absolute_error(y_test, preds)
        rmse = mean_squared_error(y_test, preds) ** 0.5
        r2 = r2_score(y_test, preds)
        return mae, rmse, r2

    for feature_set in ("full", "minimal"):
        print(f"=== Ridge alpha sweep, feature_set={feature_set} ===")
        results = []
        for alpha in RIDGE_ALPHAS:
            mae, rmse, r2 = fit_and_score("ridge", feature_set, alpha=alpha)
            results.append((alpha, mae, rmse, r2))
            print(f"  alpha={alpha:7.2f}  MAE={mae:6.3f}  RMSE={rmse:6.3f}  R2={r2:6.3f}")
        best = min(results, key=lambda r: r[1])
        print(f"  best by MAE: alpha={best[0]}\n")

    for feature_set in ("full", "minimal"):
        print(f"=== GBM max_depth x learning_rate sweep, feature_set={feature_set} ===")
        results = []
        for max_depth in GBM_MAX_DEPTHS:
            for lr in GBM_LEARNING_RATES:
                mae, rmse, r2 = fit_and_score("gbm", feature_set, max_depth=max_depth, learning_rate=lr)
                results.append((max_depth, lr, mae, rmse, r2))
                print(f"  max_depth={max_depth}  lr={lr:.2f}  MAE={mae:6.3f}  RMSE={rmse:6.3f}  R2={r2:6.3f}")
        best = min(results, key=lambda r: r[2])
        print(f"  best by MAE: max_depth={best[0]}, lr={best[1]}\n")


if __name__ == "__main__":
    main()
