"""
Does knowing the opponent's defensive form (engine.team_strength) improve
next-game PIR prediction over the existing heuristic (engine.projections)
alone? Standalone validation, deliberately kept separate from
engine.projections - nothing gets wired into the real projection pipeline
until it demonstrably earns its place here, same discipline that ruled out
the ML path (see docs/technical_notes.md).

Method: walk forward through real, already-played rounds (regular season
only by default, same playoff-exclusion rule as backtest_eval.py - see
classify_phase there). For each round R with enough history, build
leakage-safe player projections and team-strength figures using only rounds
strictly before R (both engine.projections.build_projections and
engine.team_strength.build_team_strength already enforce this), then for
every player-game actually played in round R, compare:

  - baseline:  the existing projection alone
  - adjusted:  baseline nudged by the opponent's PIR-allowed rate relative
               to the league average that round, at a few candidate weights

against what the player actually scored. Reports MAE/RMSE for each, plus
the correlation between the opponent signal and the baseline's own
prediction error (a positive correlation means the baseline is leaving
exploitable signal on the table; near zero means the signal isn't there).

Usage:
    python team_strength_backtest.py --seasons E2023 E2024 E2025 --min-round 8
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass, field

from backtest_eval import classify_phase
from engine.db import get_connection, load_rows
from engine.projections import build_projections
from engine.team_strength import build_team_strength, league_average_pir_allowed

CANDIDATE_WEIGHTS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]


@dataclass
class Sample:
    season: str
    round_no: int
    baseline_pred: float
    opponent_factor: float
    actual: float


@dataclass
class Results:
    samples: list[Sample] = field(default_factory=list)
    rounds_evaluated: int = 0
    rounds_skipped_no_history: int = 0


def run_backtest(seasons: list[str], min_round: int, include_playoffs: bool) -> Results:
    conn = get_connection()
    results = Results()

    for season in seasons:
        rows = load_rows(conn, season_code=season)
        if not rows:
            print(f"{season}: no rows in DB, skipping (run sync_db.py first)")
            continue

        phase_of = classify_phase(rows)
        max_round = max(r["round"] for r in rows if r.get("round") is not None)

        for round_no in range(min_round, max_round + 1):
            if not include_playoffs and phase_of.get(round_no) == "playoffs":
                continue

            round_rows = [r for r in rows if r.get("round") == round_no and r.get("played")]
            if not round_rows:
                continue

            projections = build_projections(rows, as_of_round=round_no)
            strengths = build_team_strength(rows, as_of_round=round_no)
            if not projections or not strengths:
                results.rounds_skipped_no_history += 1
                continue
            league_avg = league_average_pir_allowed(strengths)
            if league_avg == 0:
                results.rounds_skipped_no_history += 1
                continue

            results.rounds_evaluated += 1

            for r in round_rows:
                proj = projections.get(r["player_id"])
                opp_strength = strengths.get(r.get("opponent"))
                if proj is None or opp_strength is None:
                    continue

                opponent_factor = (opp_strength.pir_allowed - league_avg) / league_avg
                results.samples.append(
                    Sample(
                        season=season,
                        round_no=round_no,
                        baseline_pred=proj.projected_pir_with_bonus,
                        opponent_factor=opponent_factor,
                        actual=float(r["pir_official"]),
                    )
                )

    return results


def mae(errors: list[float]) -> float:
    return sum(abs(e) for e in errors) / len(errors)


def rmse(errors: list[float]) -> float:
    return (sum(e * e for e in errors) / len(errors)) ** 0.5


def print_report(results: Results) -> None:
    samples = results.samples
    print(f"\nRounds evaluated: {results.rounds_evaluated} "
          f"(skipped {results.rounds_skipped_no_history} for insufficient history)")
    print(f"Player-games evaluated: {len(samples)}")
    if not samples:
        print("No samples - nothing to report.")
        return

    baseline_errors = [s.actual - s.baseline_pred for s in samples]
    baseline_mae = mae(baseline_errors)
    baseline_rmse = rmse(baseline_errors)
    print(f"\nBaseline (no opponent info):  MAE = {baseline_mae:.3f}   RMSE = {baseline_rmse:.3f}")

    factors = [s.opponent_factor for s in samples]
    if len(set(factors)) > 1 and len(set(baseline_errors)) > 1:
        corr = statistics.correlation(factors, baseline_errors)
    else:
        corr = float("nan")
    print(f"Correlation(opponent_factor, baseline residual) = {corr:+.4f}  "
          f"(near 0 = no exploitable signal in the baseline's errors; "
          f"positive = weak-defense opponents correlate with the baseline underestimating)")

    print(f"\n{'weight':>8} {'MAE':>10} {'RMSE':>10} {'vs baseline MAE':>18}")
    best_weight, best_mae = 0.0, baseline_mae
    for w in CANDIDATE_WEIGHTS:
        adjusted_errors = [s.actual - s.baseline_pred * (1 + w * s.opponent_factor) for s in samples]
        w_mae = mae(adjusted_errors)
        w_rmse = rmse(adjusted_errors)
        marker = "  <- baseline" if w == 0.0 else ""
        print(f"{w:>8.2f} {w_mae:>10.3f} {w_rmse:>10.3f} {w_mae - baseline_mae:>+18.4f}{marker}")
        if w_mae < best_mae:
            best_weight, best_mae = w, w_mae

    print(f"\nBest weight in this grid: {best_weight} (MAE {best_mae:.3f} vs baseline {baseline_mae:.3f}, "
          f"{'improvement' if best_mae < baseline_mae else 'no improvement'} of {baseline_mae - best_mae:+.4f})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seasons", nargs="+", default=["E2023", "E2024", "E2025"])
    parser.add_argument("--min-round", type=int, default=8, help="First round to evaluate (needs prior-round history)")
    parser.add_argument("--include-playoffs", action="store_true",
                         help="Include playoff rounds (excluded by default - see classify_phase in backtest_eval.py)")
    args = parser.parse_args()

    results = run_backtest(args.seasons, args.min_round, args.include_playoffs)
    print_report(results)


if __name__ == "__main__":
    main()
