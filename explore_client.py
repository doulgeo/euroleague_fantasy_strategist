"""
Exploration script: fetch and normalize EuroLeague box scores from the public
(undocumented) official endpoints, to see what real data looks like.

Thin CLI over engine.data - not the production ingestion pipeline. See
engine/data.py for the actual client/normalizers (shared with poc_run.py).

Usage:
    python explore_client.py [--competition E] [--season E2025] [--games 10]
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from engine.data import (
    FIELDNAMES,
    EuroleagueClient,
    normalize_legacy,
    normalize_v2,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition", default="E", help="E for EuroLeague, U for EuroCup")
    parser.add_argument("--season", default="E2025", help="Season code, e.g. E2025")
    parser.add_argument("--games", type=int, default=10, help="How many recent completed games to pull")
    parser.add_argument("--out", default="explore_output.csv", help="Output CSV path")
    args = parser.parse_args()

    client = EuroleagueClient()

    print(f"Listing games for {args.season}...")
    games = client.list_games(args.competition, args.season, limit=100)
    played_games = [g for g in games if g.get("played")]
    target_games = played_games[: args.games]
    print(f"Found {len(games)} games in response, {len(played_games)} played; using {len(target_games)}.")

    all_rows: list[dict] = []
    v2_failures = 0

    for g in target_games:
        game_code = g["gameCode"]
        round_no = g.get("round")
        game_date = g.get("date")
        local_code = g["local"]["club"]["code"]
        road_code = g["road"]["club"]["code"]
        local_score = g["local"].get("score")
        road_score = g["road"].get("score")

        team_win = {local_code: None, road_code: None}
        if isinstance(local_score, (int, float)) and isinstance(road_score, (int, float)) and local_score != road_score:
            team_win[local_code] = local_score > road_score
            team_win[road_code] = road_score > local_score

        print(f"  game {game_code} (round {round_no}): {local_code} vs {road_code}")

        stats_v2 = client.game_stats_v2(args.competition, args.season, game_code)
        if stats_v2:
            rows = normalize_v2(
                stats_v2, args.season, game_code, round_no, game_date, local_code, road_code, team_win
            )
        else:
            v2_failures += 1
            print("    v2 stats unavailable, falling back to legacy Boxscore")
            legacy = client.game_boxscore_legacy(args.season, game_code)
            if not legacy:
                print("    legacy Boxscore also unavailable, skipping game")
                continue
            rows = normalize_legacy(legacy, args.season, game_code)

        all_rows.extend(rows)

    if not all_rows:
        print("No rows collected.")
        return

    out_path = Path(args.out)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    mismatches = [r for r in all_rows if r["pir_diff"] != 0]

    print()
    print(f"Wrote {len(all_rows)} player-game rows to {out_path}")
    print(f"v2 fallbacks to legacy: {v2_failures}")
    print(f"PIR mismatches (official != recomputed): {len(mismatches)}")
    for r in mismatches[:10]:
        print(
            f"  game {r['game_code']} {r['player_name']}: official={r['pir_official']} "
            f"recomputed={r['pir_recomputed']} diff={r['pir_diff']}"
        )


if __name__ == "__main__":
    main()
