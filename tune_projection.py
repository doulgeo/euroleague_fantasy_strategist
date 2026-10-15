"""
Coordinate-descent tuning of engine.projections.ProjectionConfig on ONE
tuning season (default E2024; history = earlier seasons), per the plan's
tuning protocol: pick by tuning-season MAE (conditional on playing), calibration
slope within [0.9, 1.05] as the tie-break, then evaluate once elsewhere with
projection_eval.py. Never tune on the evaluation season.

    python tune_projection.py --season E2024 --passes 2
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from multiprocessing import get_context

from engine.db import get_connection, load_rows
from engine.proj_eval import _pairs, build_targets, calibration, config_estimator, error_metrics
from engine.projections import ProjectionConfig

GRID = {
    "half_life_games": [6, 8, 10, 12, 15, 20],
    "window_games": [20, 30, 60, 100],
    "season_age_weight": [0.15, 0.25, 0.4, 0.6],
    "shrink_k": [0, 1, 2, 3, 4],
    "shrink_target_scale": [1.0, 0.6],
    "exclude_prior_playoffs": [False, True],
}
START = ProjectionConfig(weighting="exp", half_life_games=10, window_games=60, season_age_weight=0.25,
                         shrink_k=2.0, min_games=1)

_ROWS: dict = {}
_SEASON = ""


def _score(config: ProjectionConfig) -> tuple[float, float, int]:
    ts = build_targets(_ROWS, [_SEASON], {"c": config_estimator(config)})
    pairs = _pairs(ts, "c", True)
    return error_metrics(pairs)["mae"], calibration(pairs)[0], len(pairs)


def main() -> None:
    global _ROWS, _SEASON
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="E2024")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--procs", type=int, default=4)
    args = ap.parse_args()
    _SEASON = args.season
    conn = get_connection()
    for s in sorted({r["season_code"] for r in load_rows(conn)}):
        _ROWS[s] = load_rows(conn, s)

    best = START
    mae, slope, n = _score(best)
    print(f"start {best}\n  MAE {mae:.4f} slope {slope:.3f} n={n}", flush=True)
    with get_context("fork").Pool(args.procs) as pool:
        for p in range(args.passes):
            for field, values in GRID.items():
                cands = [replace(best, **{field: v}) for v in values if getattr(best, field) != v]
                results = pool.map(_score, cands)
                scored = list(zip(cands, results)) + [(best, (mae, slope, n))]
                # slope in the acceptable band beats out-of-band; then lowest MAE
                key = lambda cr: (not (0.9 <= cr[1][1] <= 1.05), cr[1][0])  # noqa: E731
                for c, (m, sl, _n) in zip(cands, results):
                    print(f"  pass {p} {field}={getattr(c, field)}: MAE {m:.4f} slope {sl:.3f}", flush=True)
                best, (mae, slope, n) = min(scored, key=key)
                print(f"pass {p} {field} -> {getattr(best, field)} (MAE {mae:.4f}, slope {slope:.3f})", flush=True)
    print(f"\nBEST {best}\n  MAE {mae:.4f} slope {slope:.3f}")


if __name__ == "__main__":
    main()
