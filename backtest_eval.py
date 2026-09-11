"""
Broader backtest evaluation: run the recommendation pipeline across many
(season, cutoff-round) combinations and aggregate results, instead of the
handful of spot-checked rounds poc_run.py exercises one at a time.

For each season, for each cutoff round with enough prior-round history,
build leakage-safe projections (only rounds strictly before the cutoff, same
season - see engine.projections), sample `--trials-per-round` different
random rosters/active-squads (roster sampling is random - see
engine.roster.sample_roster - so we average over several draws per round to
reduce sampling noise), and score no-swap / engine-recommended /
best-possible-hindsight for each trial.

Usage:
    python backtest_eval.py --seasons E2020 E2021 E2022 E2023 E2024 E2025 \
        --min-round 6 --trials-per-round 5
"""

from __future__ import annotations

import argparse
import random
import statistics
from dataclasses import dataclass, field

from engine.data import EuroleagueClient, fetch_season
from engine.lineup import build_lineup, compute_round_score, swap_after_day1, team_dates_for_round
from engine.projections import build_projections
from engine.roster import sample_active_squad, sample_roster


@dataclass
class Trial:
    season: str
    round_no: int
    no_swap: float
    recommended: float
    best_possible: float


@dataclass
class SeasonSummary:
    season: str
    rounds_attempted: int = 0
    rounds_skipped: int = 0
    trials: list[Trial] = field(default_factory=list)


def actual_pir_lookup(rows: list[dict], round_no: int) -> dict[str, float]:
    return {r["player_id"]: float(r["pir_official"]) for r in rows if r.get("round") == round_no}


def run_one_trial(
    season: str,
    round_no: int,
    projections: dict,
    rows: list[dict],
    team_dates: dict[str, str],
    seed: int,
) -> Trial | None:
    rng = random.Random(seed)
    try:
        roster = sample_roster(projections, rng)
        active_squad = sample_active_squad(roster, rng)
    except (ValueError, RuntimeError):
        return None  # not enough eligible players at this position/cutoff - skip trial

    projected_value = lambda pid: projections[pid].projected_pir_with_bonus  # noqa: E731

    try:
        initial = build_lineup(active_squad, team_dates, projected_value)
    except RuntimeError:
        return None
    recommended = swap_after_day1(initial, team_dates, projected_value)

    actual = actual_pir_lookup(rows, round_no)
    no_swap_score = compute_round_score(initial, initial, team_dates, actual)
    recommended_score = compute_round_score(initial, recommended, team_dates, actual)

    hindsight_value = lambda pid: actual.get(pid, 0.0)  # noqa: E731
    hindsight_initial = build_lineup(active_squad, team_dates, hindsight_value)
    hindsight_best = swap_after_day1(hindsight_initial, team_dates, hindsight_value)
    best_possible_score = compute_round_score(hindsight_initial, hindsight_best, team_dates, actual)

    return Trial(
        season=season,
        round_no=round_no,
        no_swap=no_swap_score,
        recommended=recommended_score,
        best_possible=best_possible_score,
    )


def evaluate_season(
    client: EuroleagueClient,
    competition: str,
    season: str,
    min_round: int,
    trials_per_round: int,
    base_seed: int,
) -> SeasonSummary:
    rows = fetch_season(client, competition, season, verbose=False)
    if not rows:
        print(f"{season}: no data, skipping season entirely")
        return SeasonSummary(season=season)

    rounds_present = sorted({r["round"] for r in rows if r.get("round") is not None})
    summary = SeasonSummary(season=season)

    for round_no in rounds_present:
        if round_no < min_round:
            continue

        team_dates = team_dates_for_round(rows, round_no)
        if not team_dates:
            summary.rounds_skipped += 1
            continue

        projections = build_projections(rows, as_of_round=round_no)
        if len(projections) < 20:  # not enough of a pool this early in the season
            summary.rounds_skipped += 1
            continue

        got_any = False
        for t in range(trials_per_round):
            trial = run_one_trial(season, round_no, projections, rows, team_dates, base_seed + t)
            if trial is not None:
                summary.trials.append(trial)
                got_any = True

        if got_any:
            summary.rounds_attempted += 1
        else:
            summary.rounds_skipped += 1

    return summary


def print_summary(summary: SeasonSummary) -> None:
    n = len(summary.trials)
    print(f"\n=== {summary.season}: {summary.rounds_attempted} rounds evaluated "
          f"({summary.rounds_skipped} skipped, not enough history/pool), {n} trials ===")
    if n == 0:
        return

    no_swap = [t.no_swap for t in summary.trials]
    recommended = [t.recommended for t in summary.trials]
    best_possible = [t.best_possible for t in summary.trials]

    beat_no_swap = sum(1 for t in summary.trials if t.recommended > t.no_swap + 1e-9)
    tied_no_swap = sum(1 for t in summary.trials if abs(t.recommended - t.no_swap) <= 1e-9)
    lost_to_no_swap = sum(1 for t in summary.trials if t.recommended < t.no_swap - 1e-9)
    hit_best_possible = sum(1 for t in summary.trials if abs(t.recommended - t.best_possible) <= 1e-9)

    invariant_violations = sum(1 for t in summary.trials if t.no_swap > t.best_possible + 1e-9)

    print(f"  mean no-swap:            {statistics.mean(no_swap):7.2f}")
    print(f"  mean recommended:        {statistics.mean(recommended):7.2f}")
    print(f"  mean best-possible:      {statistics.mean(best_possible):7.2f}")
    print(f"  recommended vs no-swap:  beat {beat_no_swap} ({beat_no_swap/n:.0%}), "
          f"tied {tied_no_swap} ({tied_no_swap/n:.0%}), lost {lost_to_no_swap} ({lost_to_no_swap/n:.0%})")
    print(f"  recommended == best-possible: {hit_best_possible}/{n} ({hit_best_possible/n:.0%})")
    avg_gap_to_best = statistics.mean(t.best_possible - t.recommended for t in summary.trials)
    avg_captured_pct = statistics.mean(
        (t.recommended - t.no_swap) / (t.best_possible - t.no_swap)
        for t in summary.trials
        if t.best_possible - t.no_swap > 1e-9
    )
    print(f"  avg gap to best-possible: {avg_gap_to_best:7.2f} PIR")
    print(f"  avg %% of available swap upside captured: {avg_captured_pct:.0%}")
    if invariant_violations:
        print(f"  WARNING: no-swap > best-possible in {invariant_violations} trials - check logic.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition", default="E")
    parser.add_argument(
        "--seasons", nargs="+", default=["E2020", "E2021", "E2022", "E2023", "E2024", "E2025"]
    )
    parser.add_argument("--min-round", type=int, default=6, help="Skip cutoff rounds earlier than this (too little history)")
    parser.add_argument("--trials-per-round", type=int, default=5, help="Random roster draws per (season, round)")
    parser.add_argument("--seed", type=int, default=1, help="Base random seed")
    args = parser.parse_args()

    client = EuroleagueClient()  # all data expected pre-cached by backfill.py; live calls only on cache miss

    all_summaries: list[SeasonSummary] = []
    for season in args.seasons:
        summary = evaluate_season(
            client, args.competition, season, args.min_round, args.trials_per_round, args.seed
        )
        print_summary(summary)
        all_summaries.append(summary)

    all_trials = [t for s in all_summaries for t in s.trials]
    if all_trials:
        grand = SeasonSummary(season="ALL SEASONS COMBINED", trials=all_trials,
                               rounds_attempted=sum(s.rounds_attempted for s in all_summaries),
                               rounds_skipped=sum(s.rounds_skipped for s in all_summaries))
        print_summary(grand)


if __name__ == "__main__":
    main()
