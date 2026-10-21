"""Phase 4 context acceptance checks on local, historical data.

Fits the home/away PIR residual on E2024 only. Evaluates E2025 and E2026
with fixed win coefficients and paired round bootstrap. Trial rosters are
sampled once from the baseline, then reused unchanged by both arms.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
from dataclasses import replace

import numpy as np

from backtest_eval import actual_pir_lookup
from engine.availability import DEFAULT_AVAILABILITY, estimate_p_play
from engine.db import DB_PATH, load_rows
from engine.lineup import build_lineup, choose_active_squad, compute_round_score, plan_round, team_dates_from_schedule
from engine.proj_eval import blended_history
from engine.projections import TUNED_CONFIG, actual_fantasy_score, build_projections
from engine.roster import sample_roster
from engine.rounds import classify_phase, in_main_window, round_start_dates, round_windows
from engine.win_prob import games_from_rows, round_projections, schedule_from_games


def history_pool(by_season, season, cutoff):
    history = blended_history(by_season, season, cutoff)
    pool = build_projections(history, cutoff_date=cutoff, config=TUNED_CONFIG)
    availability = estimate_p_play(history, cutoff)
    return history, {pid: replace(p, p_play=availability.get(pid, DEFAULT_AVAILABILITY.prior_mean))
                     for pid, p in pool.items()}


def fit_home_effect(by_season):
    rows = by_season["E2024"]
    phase, starts, windows = classify_phase(rows), round_start_dates(rows), round_windows(rows)
    errors = {"home": [], "away": []}
    for rnd, cutoff in sorted(starts.items()):
        if phase.get(rnd) != "regular":
            continue
        _, pool = history_pool(by_season, "E2024", cutoff)
        for r in rows:
            if r["round"] != rnd or not r["played"] or not in_main_window(r["game_date"], windows[rnd]):
                continue
            p = pool.get(r["player_id"])
            if p is not None and r["home_away"] in errors:
                errors[r["home_away"]].append(float(r["pir_official"]) - p.projected_pir)
    means = {side: float(np.mean(es)) for side, es in errors.items()}
    return (means["home"] - means["away"]) / 2, {side: len(es) for side, es in errors.items()}, means


def bootstrap(groups, seed=0, draws=2000):
    """groups: per-round (sum of paired differences, count). Resample rounds."""
    if not groups:
        return None
    values = np.asarray(groups, dtype=float)
    rng = np.random.default_rng(seed)
    ix = rng.integers(0, len(values), size=(draws, len(values)))
    totals = values[ix].sum(axis=1)
    samples = totals[:, 0] / totals[:, 1]
    return [float(values[:, 0].sum() / values[:, 1].sum()),
            *[float(v) for v in np.quantile(samples, [0.025, 0.975])]]


def evaluate(by_season, season, home_effect, trials):
    rows = by_season[season]
    games = games_from_rows(rows)
    schedule = schedule_from_games(games)
    phase, starts, windows = classify_phase(rows), round_start_dates(rows), round_windows(rows)
    error_groups, point_groups, context_point_groups = [], [], []
    totals = {"played_targets": 0, "baseline_absolute_error": 0.0, "context_absolute_error": 0.0,
              "trials": 0, "baseline_points": 0.0, "context_points": 0.0}
    for rnd, cutoff in sorted(starts.items()):
        if phase.get(rnd) != "regular":
            continue
        history, pool = history_pool(by_season, season, cutoff)
        contextual = round_projections(pool, schedule, rnd, history, cutoff_date=cutoff, home_pir=home_effect)
        schedule_only = round_projections(pool, schedule, rnd, history, cutoff_date=cutoff, enabled=False)
        errors = []
        # Conditional bonus-inclusive target: actual known club from the box
        # score is used only to select target coverage, never to alter pools.
        for r in rows:
            if r["round"] != rnd or not r["played"] or not in_main_window(r["game_date"], windows[rnd]):
                continue
            pid = r["player_id"]
            if pid not in pool or pool[pid].team != r["team"]:
                continue
            actual = actual_fantasy_score(float(r["pir_official"]), r["team_win"])
            base_err = abs(pool[pid].projected_pir_with_bonus - actual)
            new_err = abs(contextual[pid].projected_pir_with_bonus - actual)
            totals["played_targets"] += 1
            totals["baseline_absolute_error"] += base_err
            totals["context_absolute_error"] += new_err
            errors.append(new_err - base_err)
        if errors:
            error_groups.append((sum(errors), len(errors)))
        dates = team_dates_from_schedule(schedule, rnd)
        actuals = actual_pir_lookup(rows, rnd)
        differences, context_differences = [], []
        for trial in range(trials):
            try:
                roster = sample_roster(pool, random.Random(20261021 + trial))
            except ValueError:
                continue
            scores = []
            for arm in (pool, contextual, schedule_only):
                value = lambda pid, arm=arm: arm[pid].expected_value  # noqa: E731
                active = choose_active_squad(roster, dates, value)
                initial = build_lineup(active, dates, value)
                final = plan_round(initial, dates, value, actuals)
                scores.append(compute_round_score(final, actuals))
            totals["trials"] += 1
            totals["baseline_points"] += scores[0]
            totals["context_points"] += scores[1]
            differences.append(scores[1] - scores[0])
            context_differences.append(scores[1] - scores[2])
        if differences:
            point_groups.append((sum(differences), len(differences)))
            context_point_groups.append((sum(context_differences), len(context_differences)))
    n = totals.pop("played_targets")
    t = totals.pop("trials")
    return {"season": season, "rounds": len(error_groups), "played_targets": n,
            "baseline_bonus_mae": totals["baseline_absolute_error"] / n,
            "context_bonus_mae": totals["context_absolute_error"] / n,
            "dMAE_round_bootstrap": bootstrap(error_groups), "paired_trials": t,
            "baseline_mean_points": totals["baseline_points"] / t if t else None,
            "context_mean_points": totals["context_points"] / t if t else None,
            "dPoints_round_bootstrap": bootstrap(point_groups),
            "context_vs_schedule_only_dPoints": bootstrap(context_point_groups)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", nargs="+", default=["E2025", "E2026"])
    parser.add_argument("--trials-per-round", type=int, default=5)
    parser.add_argument("--home-pir", type=float, help="fixed adjustment; omit to use the E2024 fit")
    args = parser.parse_args()
    if args.trials_per_round < 0:
        parser.error("--trials-per-round must be nonnegative")
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = load_rows(conn)
    by_season = {s: [r for r in rows if r["season_code"] == s] for s in sorted({r["season_code"] for r in rows})}
    effect, counts, means = fit_home_effect(by_season)
    print(json.dumps({"E2024_home_pir_fit": effect, "n": counts, "residuals": means}), flush=True)
    for season in args.seasons:
        print(json.dumps(evaluate(by_season, season, effect if args.home_pir is None else args.home_pir,
                                  args.trials_per_round), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
