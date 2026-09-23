"""
POC harness: fetch a round window of a season, build heuristic projections
as of a cutoff round, sample a plausible 13-player roster and a valid
active-squad exclusion, and walk the round end-to-end - lineup/sixth-man/
captain recommendation, transfer suggestions, and a backtest against what
actually happened.

Usage:
    python poc_run.py --season E2025 --cutoff-round 20 --history-rounds 12 --validate-rounds 3
"""

from __future__ import annotations

import argparse
import random

from engine.data import EuroleagueClient, fetch_season
from engine.lineup import (
    availability_label,
    build_lineup,
    choose_active_squad,
    compute_round_score,
    swap_after_day1,
    team_dates_for_round,
)
from engine.projections import actual_fantasy_score, build_projections
from engine.roster import sample_roster
from engine.transfers import suggest_transfers


def print_ranked_list(projections, top_n: int) -> None:
    ranked = sorted(projections.values(), key=lambda p: p.projected_pir_with_bonus, reverse=True)
    print(f"\n=== Top {top_n} fantasy potential (as of cutoff) ===")
    print(f"{'#':>3} {'Player':<28} {'Pos':<8} {'Team':<5} {'Proj PIR':>9} {'+Bonus':>8} {'n':>3}")
    for i, p in enumerate(ranked[:top_n], 1):
        print(
            f"{i:>3} {p.player_name:<28} {p.position:<8} {p.team:<5} "
            f"{p.projected_pir:>9.1f} {p.projected_pir_with_bonus:>8.1f} {p.games_sampled:>3}"
        )


def print_active_squad(active_squad, team_dates) -> None:
    print("\n=== Active squad (10 of 13 - 3 excluded this round) ===")
    for position in ("Guard", "Forward", "Center"):
        players = active_squad.by_position(position)
        print(f"{position}s:")
        for p in players:
            label = availability_label(p, team_dates)
            print(f"  {p.player_name:<28} {p.team:<5} proj={p.projected_pir_with_bonus:>6.1f}  [{label}]")

    print("Excluded (score 0, cannot be swapped in this round):")
    for p in active_squad.excluded:
        print(f"  {p.player_name:<28} {p.position:<8} {p.team:<5} proj={p.projected_pir_with_bonus:>6.1f}")


def print_lineup(title: str, lineup, team_dates) -> None:
    print(f"\n=== {title} ===")
    formation = ", ".join(p.position[0] for p in lineup.starters)
    print(f"  Formation: {formation}")
    for p in lineup.starters:
        tag = " (C)" if p.player_id == lineup.captain.player_id else ""
        label = availability_label(p, team_dates)
        print(f"  START  {p.position:<8} {p.player_name:<28}{tag:<4} [{label}] proj={p.projected_pir_with_bonus:.1f}")
    label = availability_label(lineup.sixth_man, team_dates)
    print(
        f"  6TH    {lineup.sixth_man.position:<8} {lineup.sixth_man.player_name:<28}     "
        f"[{label}] proj={lineup.sixth_man.projected_pir_with_bonus:.1f}"
    )
    print("  Bench (half points):")
    for p in lineup.bench:
        label = availability_label(p, team_dates)
        print(f"    {p.position:<8} {p.player_name:<28} [{label}] proj={p.projected_pir_with_bonus:.1f}")


def print_transfers(suggestions) -> None:
    print("\n=== Transfer suggestions (naive - pool is a stand-in for real free agents) ===")
    if not suggestions:
        print("  No suggested upgrades above the threshold.")
        return
    for s in suggestions[:10]:
        print(
            f"  DROP {s.drop.player_name:<24} ({s.drop.position}, {s.drop.projected_pir_with_bonus:.1f}) "
            f"-> ADD {s.add.player_name:<24} ({s.add.projected_pir_with_bonus:.1f})  "
            f"gain={s.projected_gain:+.1f}"
        )


def actual_pir_lookup(rows: list[dict], round_no: int) -> dict[str, float]:
    """Each player's real fantasy score for the round - PIR plus the real
    +10% team-win bonus when their team actually won (see
    engine.projections.actual_fantasy_score, confirmed 2026-09-22)."""
    return {
        r["player_id"]: actual_fantasy_score(float(r["pir_official"]), r.get("team_win"))
        for r in rows
        if r.get("round") == round_no
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition", default="E")
    parser.add_argument("--season", default="E2025")
    parser.add_argument("--cutoff-round", type=int, default=20, help="Round to build the recommendation for")
    parser.add_argument("--history-rounds", type=int, default=12, help="Rounds of history to fetch before the cutoff")
    parser.add_argument("--validate-rounds", type=int, default=3, help="Extra rounds after cutoff to fetch (for re-runs)")
    parser.add_argument("--top-n", type=int, default=20, help="How many players to show in the ranked list")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for roster sampling")
    args = parser.parse_args()

    min_round = max(1, args.cutoff_round - args.history_rounds)
    max_round = args.cutoff_round + args.validate_rounds

    client = EuroleagueClient()
    rows = fetch_season(
        client, args.competition, args.season, min_round=min_round, max_round=max_round
    )

    if not any(r.get("round") == args.cutoff_round for r in rows):
        print(f"No data found for round {args.cutoff_round} in the fetched window - nothing to recommend.")
        return

    projections = build_projections(rows, as_of_round=args.cutoff_round)
    print(f"\nBuilt projections for {len(projections)} players using rounds {min_round}-{args.cutoff_round - 1}")

    print_ranked_list(projections, args.top_n)

    rng = random.Random(args.seed)
    roster = sample_roster(projections, rng)
    team_dates = team_dates_for_round(rows, args.cutoff_round)

    if not team_dates:
        print(f"\nNo games found for round {args.cutoff_round} itself - can't build a lineup for it.")
        return

    projected_value = lambda pid: projections[pid].projected_pir_with_bonus  # noqa: E731

    # Roster is still a random stand-in (no real ownership data yet), but the
    # exclusion is the real per-round decision: which 3 of the 13 to bench.
    active_squad = choose_active_squad(roster, team_dates, projected_value)
    print_active_squad(active_squad, team_dates)

    initial = build_lineup(active_squad, team_dates, projected_value)
    print_lineup(f"Initial lineup recommendation (round {args.cutoff_round}, pre-day-1)", initial, team_dates)

    # The real swap decision happens AFTER day-1 concludes, with day-1's
    # actual results already known - not just their pre-round projection
    # (see engine.lineup's 2026-09-16 correction: demoting a day-1 player
    # now genuinely halves their score, so the decision needs to weigh what
    # they actually scored, not what they were expected to). This POC run
    # covers a round that's already fully played (validate-rounds), so
    # `actual` is available here to build that realistic decision input -
    # day-1 teams use their actual PIR, day-2 teams still use projections
    # (day-2 hasn't happened relative to the decision point; using their
    # actuals here would be hindsight leakage).
    actual = actual_pir_lookup(rows, args.cutoff_round)
    min_date = min(team_dates.values()) if team_dates else None
    day1_teams = {team for team, date in team_dates.items() if date == min_date}
    day1_actual = {pid: pir for pid, pir in actual.items() if pid in projections and projections[pid].team in day1_teams}
    decision_value = lambda pid: day1_actual[pid] if pid in day1_actual else projected_value(pid)  # noqa: E731

    recommended = swap_after_day1(initial, team_dates, decision_value)
    print_lineup(f"Post-day-1 swap recommendation (round {args.cutoff_round})", recommended, team_dates)

    transfers = suggest_transfers(roster, projections)
    print_transfers(transfers)

    # --- Backtest: score initial (no-swap), recommended (with swap), and
    # best-possible-hindsight lineups against what actually happened. A
    # player's score is governed entirely by whichever tier they hold in the
    # lineup being scored (see compute_round_score's docstring) - demoting a
    # played starter to the bench genuinely halves their score, it isn't
    # banked regardless. Bench players score at half rate automatically,
    # whether or not they were ever swapped in.
    no_swap_score = compute_round_score(initial, actual)
    recommended_score = compute_round_score(recommended, actual)

    hindsight_value = lambda pid: actual.get(pid, 0.0)  # noqa: E731
    hindsight_initial = build_lineup(active_squad, team_dates, hindsight_value)
    hindsight_best = swap_after_day1(hindsight_initial, team_dates, hindsight_value)
    best_possible_score = compute_round_score(hindsight_best, actual)

    print(f"\n=== Backtest: round {args.cutoff_round} actual results ===")
    print(f"  No-swap score (ignored the golden rule):     {no_swap_score:6.1f}")
    print(f"  Engine-recommended score (with day-1 swap):  {recommended_score:6.1f}")
    print(f"  Best-possible hindsight score:                {best_possible_score:6.1f}")

    # Only no_swap <= best_possible is a structural guarantee (best_possible
    # optimizes with perfect information, including the option to not swap).
    # recommended is decision-time, projection-based, and can occasionally
    # underperform no_swap in a single realized round if a projection was
    # wrong - that's expected forecasting noise, not a logic bug.
    if no_swap_score > best_possible_score + 1e-9:
        print("  WARNING: expected no_swap <= best_possible to hold - check logic.")
    if recommended_score + 1e-9 < no_swap_score:
        print("  Note: recommended underperformed no-swap this round - a projection missed, not a bug.")


if __name__ == "__main__":
    main()
