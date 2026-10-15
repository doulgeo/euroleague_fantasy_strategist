"""
Projection-accuracy harness CLI (engine/proj_eval.py, plan Phase 0).

    python projection_eval.py --seasons E2024 E2025 --estimators live season_mean last5
    python projection_eval.py --seasons E2025 --csv reports/projection_eval.csv
"""

from __future__ import annotations

import argparse
import csv

from engine.db import get_connection, load_rows
from engine.proj_eval import (
    ESTIMATORS, build_targets, calibration, common_targets, decile_table, error_metrics, mae_metric,
    paired_bootstrap, ranking_metrics, brier, reliability, expected_value_bias, segment_report, top_actual_metric, zeroed_cost, _pairs,
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", nargs="+", default=["E2024", "E2025"], help="target seasons (earlier seasons in the DB are history)")
    ap.add_argument("--estimators", nargs="+", default=["live", "season_mean", "last5"], choices=sorted(ESTIMATORS))
    ap.add_argument("--mode", choices=["blended", "single"], default="blended")
    ap.add_argument("--min-round", type=int, default=1)
    ap.add_argument("--segments", action="store_true", help="print segment + decile tables for each estimator")
    ap.add_argument("--csv", help="write the per-target table here")
    args = ap.parse_args()

    conn = get_connection()
    rows_by_season: dict[str, list[dict]] = {}
    for s in sorted({r["season_code"] for r in load_rows(conn)}):
        rows_by_season[s] = load_rows(conn, s)
    estimators = {n: ESTIMATORS[n] for n in args.estimators}
    targets = build_targets(rows_by_season, args.seasons, estimators, args.mode, args.min_round)
    print(f"{len(targets)} targets over {args.seasons} ({args.mode} history), "
          f"{sum(t.played for t in targets)} played")

    if "p_play" in args.estimators:
        ptargets = [t for t in targets if t.proj.get("p_play") is not None]
        base = sum(t.played for t in ptargets) / len(ptargets)
        print(f"\n== p_play on {len(ptargets)} targets (base rate {base:.3f}) ==")
        print(f"Brier {brier(ptargets, 'p_play'):.4f} | always-plays {sum((1 - t.played) ** 2 for t in ptargets) / len(ptargets):.4f}"
              f" | flat rate {sum((base - t.played) ** 2 for t in ptargets) / len(ptargets):.4f}")
        for pred, act, n in reliability(ptargets, "p_play"):
            print(f"  predicted {pred:.2f} -> played {act:.2f} (n={n})")
        for other in args.estimators:
            if other not in ("p_play",) and other in ptargets[0].proj:
                m = expected_value_bias(ptargets, other, "p_play")
                print(f"  unconditional {other} x p_play: MAE {m['mae']:.3f} bias {m['bias']:+.2f} (n={m['n']})")
        args.estimators = [e for e in args.estimators if e != "p_play"]

    common = common_targets(targets, args.estimators)
    print(f"\n== Accuracy on {len(common)} common targets ==")
    print(f"{'estimator':<14}{'view':<14}{'n':>7}{'MAE':>8}{'RMSE':>8}{'bias':>8}{'slope':>7}{'top30 act':>10}{'top30 hit':>10}{'spearman':>9}")
    for n in args.estimators:
        for view, played_only in (("conditional", True), ("unconditional", False)):
            pairs = _pairs(common, n, played_only)
            m = error_metrics(pairs)
            slope = calibration(pairs)[0]
            rk = ranking_metrics(common, n) if played_only else {}
            print(f"{n:<14}{view:<14}{m['n']:>7}{m['mae']:>8.3f}{m['rmse']:>8.3f}{m['bias']:>8.2f}{slope:>7.2f}"
                  + (f"{rk['top30_actual']:>10.2f}{rk['top30_hit']:>10.1%}{rk['spearman']:>9.3f}" if rk else ""))
    rk = ranking_metrics(common, args.estimators[0])
    print(f"hindsight ceiling, mean actual of the true top 10/30: {rk['top10_ceiling']:.2f} / {rk['top30_ceiling']:.2f}")

    for n in args.estimators:
        print(f"\nzeroed-player cost [{n}]: {zeroed_cost(targets, n)}")
        if args.segments:
            print(f"-- segments [{n}] (played targets) --")
            for seg, m in segment_report(targets, n).items():
                print(f"  {seg:<22}n={m['n']:<6}MAE {m['mae']:.3f}  bias {m['bias']:+.2f}")
            print(f"-- deciles [{n}] (projected -> actual) --")
            for p, a, k in decile_table(_pairs(common, n, True)):
                print(f"  {p:6.2f} -> {a:6.2f}  (n={k})")

    base = args.estimators[0]
    for other in args.estimators[1:]:
        for label, metric in (("dMAE", mae_metric()), ("dTop30 actual", top_actual_metric(30))):
            d, lo, hi = paired_bootstrap(common, other, base, metric)
            print(f"{other} - {base}  {label}: {d:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["season", "round", "player_id", "position", "team", "team_changed", "games_before",
                        "actual", "played", "absent"] + args.estimators)
            for t in targets:
                w.writerow([t.season, t.round, t.player_id, t.position, t.team, t.team_changed, t.games_before,
                            t.actual, t.played, t.absent] + [t.proj.get(n) for n in args.estimators])


if __name__ == "__main__":
    main()
