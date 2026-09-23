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

Beyond the headline aggregate, this also reports:
- 95% confidence intervals on the mean recommended-vs-no-swap gain and the
  mean captured-upside percentage (so "42%" can be read as "42% +/- X%",
  not a number with unstated precision).
- An early/mid/late-season tercile breakdown, to check whether performance
  is stable across the season or concentrated where the rolling-window
  projection has more/less history to work with.
- A loss-case report: when recommended < no-swap, how often, how big, and
  where it clusters - this is the flagged-as-expected "projection was wrong"
  case (see engine/lineup.py and poc_run.py), not a logic bug, but worth
  characterizing rather than just citing a single percentage.
- A rolling-window sensitivity check (--sensitivity): reruns the same
  evaluation at a few different engine.projections rolling-window sizes to
  see how much the headline numbers move with that hyperparameter.

Usage:
    python backtest_eval.py --seasons E2023 E2024 E2025 \
        --min-round 6 --trials-per-round 30 --csv backtest_trials.csv
    python backtest_eval.py --sensitivity   # rolling-window robustness check
"""

from __future__ import annotations

import argparse
import csv
import random
import statistics
from dataclasses import dataclass, field

import sqlite3

from engine.db import get_connection, load_rows
from engine.lineup import build_lineup, choose_active_squad, compute_round_score, swap_after_day1, team_dates_for_round
from engine.ml_features import build_feature_table
from engine.ml_projections import build_ensemble_projections, build_ml_projections, train_model
from engine.projections import ROLLING_WINDOW, actual_fantasy_score, build_projections
from engine.roster import DRAFT_POOL_SIZE, build_draft_pool, sample_roster

Z_95 = 1.96


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
    """Each player's real fantasy score for the round - PIR plus the real
    +10% team-win bonus when their team actually won (see
    engine.projections.actual_fantasy_score, confirmed 2026-09-22; this
    function's name predates that fix and is kept for the existing call
    sites, but it's no longer just raw PIR)."""
    return {
        r["player_id"]: actual_fantasy_score(float(r["pir_official"]), r.get("team_win"))
        for r in rows
        if r.get("round") == round_no
    }


def run_one_trial(
    season: str,
    round_no: int,
    pool: dict,
    rows: list[dict],
    team_dates: dict[str, str],
    seed: int,
) -> Trial | None:
    """`pool` is the sampling universe for this trial's random roster draw -
    either the full leakage-safe projections dict (old behavior, uniform
    random league-wide sample) or a draft-pool subset of it (see
    engine.roster.build_draft_pool) restricting the draw to plausible,
    manager-drafted players. Either way, projected values used for the
    swap/lineup decision itself come from this same dict.
    """
    rng = random.Random(seed)
    projected_value = lambda pid: pool[pid].projected_pir_with_bonus  # noqa: E731

    try:
        roster = sample_roster(pool, rng)
        # Exclusion is a decision-time-only choice in the real game (locked
        # in before any round results exist), so it uses projections here -
        # same as the initial lineup - not the hindsight `actual` values
        # used below for the best-possible ceiling. See
        # engine.lineup.choose_active_squad's docstring.
        active_squad = choose_active_squad(roster, team_dates, projected_value)
    except (ValueError, RuntimeError):
        return None  # not enough eligible players at this position/cutoff - skip trial

    try:
        initial = build_lineup(active_squad, team_dates, projected_value)
    except RuntimeError:
        return None

    # The real swap decision happens AFTER day-1 concludes, with day-1's
    # actual results already known (not just their pre-round projection) -
    # see engine.lineup's 2026-09-16 correction. Using day-1 projections for
    # this decision (as an earlier version of this backtest did) understates
    # the real risk: a day-1 player who actually outperforms their
    # projection can get wrongly demoted, genuinely costing real points
    # under the corrected halving-on-demotion rule. So the swap decision
    # here uses actual PIR for whichever teams already played on day 1, and
    # projections only for teams that haven't played yet - exactly the
    # information a real manager has at that decision point. (Day-2 actuals
    # are deliberately withheld from this lookup - peeking at those would be
    # hindsight leakage into a decision that, in reality, precedes them.)
    actual = actual_pir_lookup(rows, round_no)
    min_date = min(team_dates.values()) if team_dates else None
    day1_teams = {team for team, date in team_dates.items() if date == min_date}
    day1_actual = {pid: pir for pid, pir in actual.items() if pid in pool and pool[pid].team in day1_teams}
    decision_value = lambda pid: day1_actual[pid] if pid in day1_actual else projected_value(pid)  # noqa: E731
    recommended = swap_after_day1(initial, team_dates, decision_value)

    no_swap_score = compute_round_score(initial, actual)
    recommended_score = compute_round_score(recommended, actual)

    hindsight_value = lambda pid: actual.get(pid, 0.0)  # noqa: E731
    hindsight_initial = build_lineup(active_squad, team_dates, hindsight_value)
    hindsight_best = swap_after_day1(hindsight_initial, team_dates, hindsight_value)
    best_possible_score = compute_round_score(hindsight_best, actual)

    return Trial(
        season=season,
        round_no=round_no,
        no_swap=no_swap_score,
        recommended=recommended_score,
        best_possible=best_possible_score,
    )


def evaluate_season(
    conn: sqlite3.Connection,
    season: str,
    min_round: int,
    trials_per_round: int,
    base_seed: int,
    rolling_window: int = ROLLING_WINDOW,
    min_games: int = 3,
    include_playoffs: bool = False,
    pool_size: int = DRAFT_POOL_SIZE,
) -> SeasonSummary:
    rows = load_rows(conn, season)
    if not rows:
        print(f"{season}: no data in db, skipping season entirely (run sync_db.py first)")
        return SeasonSummary(season=season)

    rounds_present = sorted({r["round"] for r in rows if r.get("round") is not None})
    phase_of_round = classify_phase(rows)
    summary = SeasonSummary(season=season)

    for round_no in rounds_present:
        if round_no < min_round:
            continue
        if not include_playoffs and phase_of_round.get(round_no) == "playoffs":
            # Excluded by default: this POC samples a roster randomly from the
            # whole league pool every round, which doesn't account for team
            # elimination - a playoff-round "trial" would be mostly players
            # whose teams are already out, which isn't a meaningful test of
            # the engine. See docs/testing_log.md for the full writeup and
            # --include-playoffs to see those numbers anyway (heavily caveated).
            summary.rounds_skipped += 1
            continue

        team_dates = team_dates_for_round(rows, round_no)
        if not team_dates:
            summary.rounds_skipped += 1
            continue

        projections = build_projections(
            rows, as_of_round=round_no, rolling_window=rolling_window, min_games=min_games
        )
        if len(projections) < 20:  # not enough of a pool this early in the season
            summary.rounds_skipped += 1
            continue

        pool = build_draft_pool(projections, pool_size=pool_size) if pool_size > 0 else projections

        got_any = False
        for t in range(trials_per_round):
            trial = run_one_trial(season, round_no, pool, rows, team_dates, base_seed + t)
            if trial is not None:
                summary.trials.append(trial)
                got_any = True

        if got_any:
            summary.rounds_attempted += 1
        else:
            summary.rounds_skipped += 1

    return summary


def evaluate_season_ml(
    feature_table: list[dict],
    all_rows: list[dict],
    season: str,
    model_type: str,
    min_round: int,
    trials_per_round: int,
    base_seed: int,
    rolling_window: int = ROLLING_WINDOW,
    min_games: int = 3,
    include_playoffs: bool = False,
    pool_size: int = DRAFT_POOL_SIZE,
    feature_set: str = "full",
) -> SeasonSummary:
    """Same structure/skip-conditions/trial loop as evaluate_season, but
    trains a fresh regression model per round (cross-season-pooled via
    train_model) and projects via build_ml_projections instead of calling
    build_projections directly. Kept as a parallel function rather than a
    shared strategy-parameter abstraction so the heuristic path above stays
    provably unmodified.

    model_type == "ensemble" is a special case: trains both ridge and gbm
    that round and averages them with the heuristic itself (equal weight,
    see build_ensemble_projections) instead of using a single trained model.
    """
    rows = [r for r in all_rows if r["season_code"] == season]
    if not rows:
        print(f"{season}: no data in db, skipping season entirely (run sync_db.py first)")
        return SeasonSummary(season=season)

    rounds_present = sorted({r["round"] for r in rows if r.get("round") is not None})
    phase_of_round = classify_phase(rows)
    summary = SeasonSummary(season=season)

    for round_no in rounds_present:
        if round_no < min_round:
            continue
        if not include_playoffs and phase_of_round.get(round_no) == "playoffs":
            summary.rounds_skipped += 1
            continue

        team_dates = team_dates_for_round(rows, round_no)
        if not team_dates:
            summary.rounds_skipped += 1
            continue

        if model_type == "ensemble":
            models = {
                name: train_model(feature_table, as_of_season=season, as_of_round=round_no,
                                   model_type=name, feature_set=feature_set)
                for name in ("ridge", "gbm")
            }
            projections = build_ensemble_projections(
                rows, as_of_round=round_no, models=models, feature_set=feature_set,
                include_heuristic=True, rolling_window=rolling_window, min_games=min_games,
            )
        else:
            model = train_model(feature_table, as_of_season=season, as_of_round=round_no,
                                 model_type=model_type, feature_set=feature_set)
            projections = build_ml_projections(
                rows, as_of_round=round_no, model=model, feature_set=feature_set,
                rolling_window=rolling_window, min_games=min_games,
            )
        if len(projections) < 20:
            summary.rounds_skipped += 1
            continue

        pool = build_draft_pool(projections, pool_size=pool_size) if pool_size > 0 else projections

        got_any = False
        for t in range(trials_per_round):
            trial = run_one_trial(season, round_no, pool, rows, team_dates, base_seed + t)
            if trial is not None:
                summary.trials.append(trial)
                got_any = True

        if got_any:
            summary.rounds_attempted += 1
        else:
            summary.rounds_skipped += 1

    return summary


def ci95(values: list[float]) -> tuple[float, float] | None:
    """(mean, half-width) of a 95% CI, or None if fewer than 2 samples."""
    n = len(values)
    if n < 2:
        return None
    mean = statistics.mean(values)
    half_width = Z_95 * statistics.stdev(values) / (n**0.5)
    return mean, half_width


def print_summary(summary: SeasonSummary) -> None:
    n = len(summary.trials)
    print(f"\n=== {summary.season}: {summary.rounds_attempted} rounds evaluated "
          f"({summary.rounds_skipped} skipped, not enough history/pool), {n} trials ===")
    if n == 0:
        return

    no_swap = [t.no_swap for t in summary.trials]
    recommended = [t.recommended for t in summary.trials]
    best_possible = [t.best_possible for t in summary.trials]
    gains = [t.recommended - t.no_swap for t in summary.trials]

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
    captures = [
        (t.recommended - t.no_swap) / (t.best_possible - t.no_swap)
        for t in summary.trials
        if t.best_possible - t.no_swap > 1e-9
    ]
    avg_captured_pct = statistics.mean(captures) if captures else float("nan")
    print(f"  avg gap to best-possible: {avg_gap_to_best:7.2f} PIR")
    print(f"  avg %% of available swap upside captured: {avg_captured_pct:.0%}")

    gain_ci = ci95(gains)
    if gain_ci:
        mean_gain, hw = gain_ci
        print(f"  mean gain (recommended - no-swap): {mean_gain:+.2f} PIR (95% CI +/- {hw:.2f}, n={n})")
    capture_ci = ci95(captures)
    if capture_ci:
        mean_cap, hw_cap = capture_ci
        print(f"  mean swap-upside captured: {mean_cap:.0%} (95% CI +/- {hw_cap:.0%}, n={len(captures)})")

    if invariant_violations:
        print(f"  WARNING: no-swap > best-possible in {invariant_violations} trials - check logic.")


def loss_case_report(trials: list[Trial]) -> None:
    losses = [t for t in trials if t.recommended < t.no_swap - 1e-9]
    n = len(trials)
    print(f"\n=== Loss-case report: recommended < no-swap in {len(losses)}/{n} trials ({len(losses)/n:.1%}) ===")
    if not losses:
        print("  (none)")
        return

    deficits = [t.no_swap - t.recommended for t in losses]
    print(f"  mean deficit: {statistics.mean(deficits):.2f} PIR, max deficit: {max(deficits):.2f} PIR")

    by_season: dict[str, int] = {}
    for t in losses:
        by_season[t.season] = by_season.get(t.season, 0) + 1
    print(f"  by season: {by_season}")

    worst = sorted(losses, key=lambda t: t.no_swap - t.recommended, reverse=True)[:5]
    print("  worst 5 individual losses:")
    for t in worst:
        print(f"    {t.season} round {t.round_no}: no-swap={t.no_swap:.1f} recommended={t.recommended:.1f} "
              f"(deficit {t.no_swap - t.recommended:.1f})")
    print("  Expected/documented case: the swap decision was made on a projection that turned out wrong for "
          "that round - not a logic bug (the no-swap <= best-possible structural guarantee, checked separately "
          "above, is what would indicate an actual bug).")


def classify_phase(rows: list[dict]) -> dict[int, str]:
    """round -> 'regular' or 'playoffs', per season.

    EuroLeague round-robin regular season has every team playing each round
    (games_this_round == teams_this_round / 2, at the season's max team
    count). Playoffs/Final Four are elimination rounds with far fewer teams
    (down to 2, for a single Final-Four game). Detected from the data rather
    than hardcoded round numbers, since team count changed between seasons
    (18 teams through E2024, 20 from E2025) and playoff round numbering
    isn't fixed either.
    """
    teams_per_round: dict[int, int] = {}
    for r in rows:
        round_no = r.get("round")
        if round_no is None:
            continue
        teams_per_round.setdefault(round_no, set()).add(r["team"])  # type: ignore[arg-type]
    teams_per_round = {r: len(teams) for r, teams in teams_per_round.items()}  # type: ignore[assignment]

    full_teams = max(teams_per_round.values())
    return {
        r: ("regular" if count >= full_teams * 0.9 else "playoffs")
        for r, count in teams_per_round.items()
    }


def phase_breakdown(trials: list[Trial], phase_of: dict[tuple[str, int], str]) -> None:
    """Split trials into regular-season vs playoffs and report each
    separately - conflating the two (e.g. a naive calendar tercile) makes
    the engine look like it degrades late in the season, when really
    playoffs are a different game: far fewer teams active, and this POC's
    random roster sampling doesn't account for team elimination (a real
    manager's playoff-time roster wouldn't include players from teams
    that are already out) - see docs/testing_log.md for the full writeup."""
    by_phase: dict[str, list[Trial]] = {"regular": [], "playoffs": []}
    for t in trials:
        by_phase[phase_of.get((t.season, t.round_no), "regular")].append(t)

    print("\n=== Regular season vs. playoffs breakdown ===")
    for label in ("regular", "playoffs"):
        group = by_phase[label]
        if not group:
            continue
        n = len(group)
        beat = sum(1 for t in group if t.recommended > t.no_swap + 1e-9)
        captures = [
            (t.recommended - t.no_swap) / (t.best_possible - t.no_swap)
            for t in group
            if t.best_possible - t.no_swap > 1e-9
        ]
        avg_cap = statistics.mean(captures) if captures else float("nan")
        rounds = sorted({t.round_no for t in group})
        print(f"  {label:<9} (n={n:4d}, rounds {rounds[0]}-{rounds[-1]}): "
              f"beat no-swap {beat/n:.0%}, avg swap-upside captured {avg_cap:.0%}")
    print("  Note: playoffs numbers reflect a POC limitation (random league-wide roster sampling doesn't "
          "exclude eliminated teams' players), not a real drop in engine quality - see testing_log.md.")


def write_csv(trials: list[Trial], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["season", "round", "no_swap", "recommended", "best_possible"])
        for t in trials:
            writer.writerow([t.season, t.round_no, t.no_swap, t.recommended, t.best_possible])
    print(f"\nWrote {len(trials)} per-trial rows to {path}")


def run_full_eval(
    seasons: list[str],
    min_round: int,
    trials_per_round: int,
    seed: int,
    rolling_window: int = ROLLING_WINDOW,
    min_games: int = 3,
    include_playoffs: bool = False,
    pool_size: int = DRAFT_POOL_SIZE,
) -> list[Trial]:
    conn = get_connection()
    all_summaries: list[SeasonSummary] = []
    for season in seasons:
        summary = evaluate_season(
            conn, season, min_round, trials_per_round, seed,
            rolling_window=rolling_window, min_games=min_games, include_playoffs=include_playoffs,
            pool_size=pool_size,
        )
        all_summaries.append(summary)
    conn.close()
    return [t for s in all_summaries for t in s.trials]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", nargs="+", default=["E2023", "E2024", "E2025"])
    parser.add_argument("--min-round", type=int, default=6, help="Skip cutoff rounds earlier than this (too little history)")
    parser.add_argument("--trials-per-round", type=int, default=5, help="Random roster draws per (season, round)")
    parser.add_argument("--seed", type=int, default=1, help="Base random seed")
    parser.add_argument("--csv", default=None, help="Optional path to write every trial's raw scores to")
    parser.add_argument(
        "--include-playoffs", action="store_true",
        help="Also evaluate playoff rounds (heavily caveated - see phase_breakdown's note; excluded by default "
             "because this POC's random league-wide roster sampling doesn't account for team elimination)",
    )
    parser.add_argument(
        "--sensitivity", action="store_true",
        help="Instead of the main run, re-run at a few rolling-window sizes to check hyperparameter sensitivity "
             "(regular season only, same exclusion as the main run)",
    )
    parser.add_argument(
        "--pool-size", type=int, default=DRAFT_POOL_SIZE,
        help="Restrict roster sampling to the top N draft-worthy players (by PIR+minutes composite, see "
             "engine.roster.build_draft_pool), instead of a uniform random draw from the whole league pool. "
             "Pass 0 to disable and sample from the full league pool (old behavior).",
    )
    parser.add_argument(
        "--projection-method", choices=["heuristic", "ridge", "gbm", "ensemble"], default="heuristic",
        help="heuristic (default) = engine.projections.build_projections, unchanged. ridge/gbm = a regression "
             "model (see engine.ml_projections) trained per round on all strictly-prior seasons in full plus the "
             "current season's rounds before the cutoff - cross-season pooling the heuristic can't do. ensemble = "
             "equal-weight average of the heuristic + ridge + gbm (see build_ensemble_projections). Only affects "
             "the main run, not --sensitivity (heuristic-only).",
    )
    parser.add_argument(
        "--feature-set", choices=["full", "minimal"], default="full",
        help="Which ml_features.FEATURE_SETS to train ridge/gbm/ensemble on. 'minimal' is a small subset close to "
             "the heuristic's own inputs (rolling PIR, minutes, volatility, team win rate, position) - see "
             "docs/testing_log.md for the full-vs-minimal comparison. No effect on --projection-method heuristic.",
    )
    args = parser.parse_args()

    if args.sensitivity:
        for window in (5, 10, 15, 20):
            trials = run_full_eval(
                args.seasons, args.min_round, args.trials_per_round, args.seed,
                rolling_window=window, pool_size=args.pool_size,
            )
            summary = SeasonSummary(season=f"rolling_window={window}", trials=trials)
            print_summary(summary)
        return

    conn = get_connection()
    all_summaries: list[SeasonSummary] = []
    phase_of: dict[tuple[str, int], str] = {}

    if args.projection_method == "heuristic":
        for season in args.seasons:
            summary = evaluate_season(
                conn, season, args.min_round, args.trials_per_round, args.seed,
                include_playoffs=args.include_playoffs, pool_size=args.pool_size,
            )
            print_summary(summary)
            all_summaries.append(summary)

            rows = load_rows(conn, season)
            for round_no, phase in classify_phase(rows).items():
                phase_of[(season, round_no)] = phase
    else:
        all_rows = load_rows(conn)  # every season, once - needed for cross-season training pool
        feature_table = build_feature_table(all_rows)
        for season in args.seasons:
            summary = evaluate_season_ml(
                feature_table, all_rows, season, args.projection_method,
                args.min_round, args.trials_per_round, args.seed,
                include_playoffs=args.include_playoffs, pool_size=args.pool_size,
                feature_set=args.feature_set,
            )
            print_summary(summary)
            all_summaries.append(summary)

            rows = [r for r in all_rows if r["season_code"] == season]
            for round_no, phase in classify_phase(rows).items():
                phase_of[(season, round_no)] = phase
    conn.close()

    all_trials = [t for s in all_summaries for t in s.trials]
    if all_trials:
        grand = SeasonSummary(season="ALL SEASONS COMBINED (regular season)" if not args.include_playoffs
                               else "ALL SEASONS COMBINED (regular season + playoffs)",
                               trials=all_trials,
                               rounds_attempted=sum(s.rounds_attempted for s in all_summaries),
                               rounds_skipped=sum(s.rounds_skipped for s in all_summaries))
        print_summary(grand)
        if args.include_playoffs:
            phase_breakdown(all_trials, phase_of)

        loss_case_report(all_trials)
        if args.csv:
            write_csv(all_trials, args.csv)


if __name__ == "__main__":
    main()
