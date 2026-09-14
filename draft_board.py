"""
Draft-prep cheat sheet: rank all EuroLeague players by projected fantasy
value, per position, to guide picks during the live 12-manager draft.

This is NOT a live draft tracker - the project has no real ownership data
yet (see CLAUDE.md "Explicitly NOT done yet"). It produces a static board
from the most recently completed season's data; during the actual draft,
cross a player off as soon as any manager (including you) takes them, and
read down to the next best available at that position.

Reuses the same heuristic projections (engine.projections) validated
elsewhere in this project - no ML, nothing new to trust.

Two things this adds beyond a plain per-position sort:
  - Tiers: within each position, a new tier starts wherever the drop to the
    next-best player is unusually large (bigger than this position's average
    gap between consecutive players). Players in the same tier are
    roughly interchangeable - it doesn't matter much which one you get, so
    don't reach; a tier boundary is where it starts to matter.
  - VORP (value over replacement): projected value minus the value of the
    last player at that position who'd plausibly still get drafted league-
    wide (12 managers x the position's roster requirement - e.g. the 36th
    Center, since only 3 Centers x 12 teams are needed). This makes
    positions comparable: a Center only slightly below the top Guards can
    still be the better pick once you account for Centers being scarcer.

Caveats (inherent to using only past EuroLeague box scores):
  - Built from last season's form. Summer transfers, retirements, and
    players new to EuroLeague this season won't be reflected or may show
    up under their old team.
  - No opponent-strength or role-change adjustment - see
    docs/technical_notes.md "Open questions" for the same caveat as it
    applies to the in-season projections this reuses.

Usage:
    python draft_board.py --season E2025 --top-n 20
    python draft_board.py --season E2025 --output-csv draft_board_E2025.csv
"""

from __future__ import annotations

import argparse
import csv

from engine.db import get_connection, known_player_ids, load_roster, load_rows
from engine.projections import Projection, build_projections, stdev
from engine.roster import REQUIRED_COUNTS
from engine.rosters import merge_roster

LEAGUE_SIZE = 12


def position_replacement_rank(position: str, league_size: int = LEAGUE_SIZE) -> int:
    """How many players at this position get drafted league-wide - the
    replacement level for VORP is the value of the player at exactly this
    rank (the last one who'd plausibly still make a roster)."""
    return REQUIRED_COUNTS[position] * league_size


def build_draft_board(projections: dict[str, Projection]) -> dict[str, list[Projection]]:
    board: dict[str, list[Projection]] = {}
    for position in REQUIRED_COUNTS:
        candidates = [p for p in projections.values() if p.position == position]
        board[position] = sorted(candidates, key=lambda p: p.projected_pir_with_bonus, reverse=True)
    return board


def replacement_values(board: dict[str, list[Projection]]) -> dict[str, float]:
    """Value of the last plausibly-drafted player at each position - the
    baseline VORP compares everyone else against."""
    values: dict[str, float] = {}
    for position, ranked in board.items():
        idx = min(position_replacement_rank(position), len(ranked)) - 1
        values[position] = ranked[idx].projected_pir_with_bonus if idx >= 0 else 0.0
    return values


def tier_breaks(ranked: list[Projection]) -> set[int]:
    """Indices (0-based, into `ranked`) right after which a new tier starts:
    where the gap to the next player is bigger than this list's average
    consecutive gap plus one standard deviation. Self-scaling per position
    instead of a hardcoded PIR threshold."""
    if len(ranked) < 3:
        return set()
    gaps = [
        ranked[i].projected_pir_with_bonus - ranked[i + 1].projected_pir_with_bonus
        for i in range(len(ranked) - 1)
    ]
    threshold = (sum(gaps) / len(gaps)) + stdev(gaps)
    return {i for i, gap in enumerate(gaps) if gap >= threshold}


def _label(p: Projection, new_ids: frozenset[str]) -> str:
    name = p.player_name
    return f"{name} [NEW]" if p.player_id in new_ids else name


def print_board(
    board: dict[str, list[Projection]],
    replacement: dict[str, float],
    top_n: int,
    new_ids: frozenset[str] = frozenset(),
) -> None:
    for position in ("Guard", "Forward", "Center"):
        ranked = board.get(position, [])
        breaks = tier_breaks(ranked)
        print(f"\n=== {position}s (top {min(top_n, len(ranked))} of {len(ranked)}, "
              f"replacement level = {replacement[position]:.1f}) ===")
        print(f"{'#':>3} {'Player':<28} {'Team':<5} {'Proj+Bonus':>10} {'VORP':>7} {'n':>3} {'Volatility':>10}")
        tier = 1
        for i, p in enumerate(ranked[:top_n]):
            print(
                f"{i + 1:>3} {_label(p, new_ids):<28} {p.team:<5} {p.projected_pir_with_bonus:>10.1f} "
                f"{p.projected_pir_with_bonus - replacement[position]:>+7.1f} {p.games_sampled:>3} "
                f"{p.volatility:>10.1f}"
            )
            if i in breaks:
                tier += 1
                print(f"    --- tier {tier} ---")


def print_overall_board(
    board: dict[str, list[Projection]],
    replacement: dict[str, float],
    top_n: int,
    new_ids: frozenset[str] = frozenset(),
) -> None:
    all_players = [p for ranked in board.values() for p in ranked]
    all_players.sort(key=lambda p: p.projected_pir_with_bonus - replacement[p.position], reverse=True)
    print(f"\n=== Best available overall, scarcity-adjusted (VORP), top {top_n} ===")
    print(f"{'#':>3} {'Player':<28} {'Pos':<8} {'Team':<5} {'Proj+Bonus':>10} {'VORP':>7}")
    for i, p in enumerate(all_players[:top_n]):
        print(
            f"{i + 1:>3} {_label(p, new_ids):<28} {p.position:<8} {p.team:<5} "
            f"{p.projected_pir_with_bonus:>10.1f} {p.projected_pir_with_bonus - replacement[p.position]:>+7.1f}"
        )


def write_csv(
    board: dict[str, list[Projection]],
    replacement: dict[str, float],
    path: str,
    new_ids: frozenset[str] = frozenset(),
) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["position", "position_rank", "player_name", "team", "games_sampled",
                          "projected_pir_with_bonus", "vorp", "volatility", "new_to_league", "drafted"])
        for position, ranked in board.items():
            for i, p in enumerate(ranked, 1):
                writer.writerow([
                    position, i, p.player_name, p.team, p.games_sampled,
                    round(p.projected_pir_with_bonus, 2),
                    round(p.projected_pir_with_bonus - replacement[position], 2),
                    round(p.volatility, 2),
                    "yes" if p.player_id in new_ids else "",
                    "",  # blank column to check off during the live draft
                ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default="E2025", help="Season to build the board from (most recent completed season)")
    parser.add_argument("--roster-season", default="E2026",
                         help="Season to pull current club rosters from (engine.db `rosters`, via sync_rosters.py) - "
                              "used to correct transferred players' team and surface [NEW] players missing from --season entirely")
    parser.add_argument("--min-games", type=int, default=10,
                         help="Drop players with fewer than this many sampled games - filters out end-of-bench noise")
    parser.add_argument("--top-n", type=int, default=20, help="How many players to print per position/overall")
    parser.add_argument("--output-csv", default=None, help="Optional path to write the full board as CSV")
    args = parser.parse_args()

    conn = get_connection()
    rows = load_rows(conn, season_code=args.season)
    if not rows:
        print(f"No rows found for season {args.season} in euroleague.db - run sync_db.py first.")
        return

    max_round = max(r["round"] for r in rows if r.get("round") is not None)
    projections = build_projections(rows, as_of_round=max_round + 1, min_games=args.min_games)
    print(f"Built projections for {len(projections)} players from {args.season} "
          f"(rounds 1-{max_round}, min {args.min_games} games played)")

    roster_rows = load_roster(conn, args.roster_season)
    new_ids: frozenset[str] = frozenset()
    if roster_rows:
        seen = known_player_ids(conn)
        projections, new_ids_set, gone_ids_set = merge_roster(projections, roster_rows, seen)
        new_ids = frozenset(new_ids_set)
        gone_count = len(gone_ids_set)
        projections = {pid: p for pid, p in projections.items() if pid not in gone_ids_set}
        print(f"Merged {args.roster_season} roster ({len(roster_rows)} players, {len(new_ids)} new to the league, "
              f"{gone_count} no longer on any current roster - excluded from this board)")
    else:
        print(f"No {args.roster_season} roster synced yet - run sync_rosters.py to get current teams and [NEW] markers")

    board = build_draft_board(projections)
    replacement = replacement_values(board)

    print_board(board, replacement, args.top_n, new_ids)
    print_overall_board(board, replacement, args.top_n * 2, new_ids)

    if args.output_csv:
        write_csv(board, replacement, args.output_csv, new_ids)
        print(f"\nFull board written to {args.output_csv}")


if __name__ == "__main__":
    main()
