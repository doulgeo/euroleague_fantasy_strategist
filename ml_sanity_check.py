"""
Fixed train/test regression sanity check: train on E2023+E2024, test on
E2025, and report plain regression metrics (MAE/RMSE/R2) for Ridge, GBM, and
the existing heuristic, over the SAME (player, round) sample - a cheap
signal before running the full walk-forward backtest_eval.py.

Usage:
    python ml_sanity_check.py
"""

from __future__ import annotations

import numpy as np
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from engine.db import get_connection, load_rows
from engine.ml_features import FEATURE_COLUMNS, build_feature_table
from engine.ml_projections import build_pipeline, design_matrix
from engine.projections import build_projections


def main() -> None:
    conn = get_connection()
    rows = load_rows(conn)
    conn.close()

    table = build_feature_table(rows)
    train = [r for r in table if r["season_code"] in ("E2023", "E2024")]
    test = [r for r in table if r["season_code"] == "E2025"]
    print(f"train rows: {len(train)} (E2023+E2024), test rows: {len(test)} (E2025)")

    X_train, y_train = design_matrix(train), np.array([r["y"] for r in train], dtype=float)
    X_test, y_test = design_matrix(test), np.array([r["y"] for r in test], dtype=float)

    ridge = build_pipeline("ridge").fit(X_train, y_train)
    gbm = build_pipeline("gbm").fit(X_train, y_train)

    # Heuristic prediction for each test row, pulled from build_projections at
    # that row's own round - same sample as the ML test rows, not an
    # independently-drawn one, so the comparison can't silently go wrong.
    e2025_rows = [r for r in rows if r.get("season_code") == "E2025"]
    proj_cache: dict[int, dict] = {}
    heuristic_preds: list[float] = []
    kept_idx: list[int] = []
    for i, r in enumerate(test):
        round_no = r["round"]
        if round_no not in proj_cache:
            proj_cache[round_no] = build_projections(e2025_rows, as_of_round=round_no)
        proj = proj_cache[round_no].get(r["player_id"])
        if proj is None:
            continue  # heuristic's own min_games gate excluded this player at this round
        heuristic_preds.append(proj.projected_pir)
        kept_idx.append(i)

    y_test_matched = y_test[kept_idx]
    heuristic_preds = np.array(heuristic_preds)
    ridge_preds_matched = ridge.predict(X_test)[kept_idx]
    gbm_preds_matched = gbm.predict(X_test)[kept_idx]
    print(f"heuristic-matched sample: {len(kept_idx)}/{len(test)} test rows "
          f"(heuristic's own min_games gate excludes the rest)")

    def report(name: str, y_true: np.ndarray, y_pred: np.ndarray) -> None:
        mae = mean_absolute_error(y_true, y_pred)
        rmse = mean_squared_error(y_true, y_pred) ** 0.5
        r2 = r2_score(y_true, y_pred)
        print(f"  {name:<12} n={len(y_true):5d}  MAE={mae:6.3f}  RMSE={rmse:6.3f}  R2={r2:6.3f}")

    print("\n=== Regression metrics, E2025 test set (heuristic-matched sample) ===")
    report("heuristic", y_test_matched, heuristic_preds)
    report("ridge", y_test_matched, ridge_preds_matched)
    report("gbm", y_test_matched, gbm_preds_matched)

    print("\n=== Regression metrics, E2025 full test set (ML-only, no heuristic gate) ===")
    report("ridge", y_test, ridge.predict(X_test))
    report("gbm", y_test, gbm.predict(X_test))

    # design_matrix builds a plain numpy array with no attached column names,
    # so get_feature_names_out needs FEATURE_COLUMNS passed explicitly to
    # resolve the numeric block's "one-to-one" names correctly.
    feature_names = ridge.named_steps["pre"].get_feature_names_out(FEATURE_COLUMNS)

    print("\n=== Ridge coefficients (sorted by |value|) ===")
    coefs = ridge.named_steps["model"].coef_
    for name, coef in sorted(zip(feature_names, coefs), key=lambda kv: -abs(kv[1]))[:15]:
        print(f"  {name:<30} {coef:+.3f}")

    print("\n=== GBM permutation importances (top 15) ===")
    # HistGradientBoostingRegressor has no .feature_importances_ - use
    # permutation_importance on the fitted pipeline instead. Importances here
    # correspond to the RAW input columns (FEATURE_COLUMNS, pre-one-hot),
    # since permutation_importance permutes the pipeline's own input, not the
    # post-transform expanded columns - do not pair with get_feature_names_out().
    result = permutation_importance(gbm, X_test, y_test, n_repeats=5, random_state=0, n_jobs=-1)
    for name, imp in sorted(zip(FEATURE_COLUMNS, result.importances_mean), key=lambda kv: -kv[1])[:15]:
        print(f"  {name:<30} {imp:+.4f}")


if __name__ == "__main__":
    main()
