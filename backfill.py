"""
Bulk backfill: fetch and cache full box-score data for a range of seasons.

Resumable by design - every game's stats are cached individually under raw/
(see engine.data.EuroleagueClient), so re-running after an interruption just
skips games already on disk. The games-list call itself is cheap (one request
per season) and always re-run, so a re-run also naturally picks up newly
played games in an in-progress season.

Usage:
    python backfill.py --seasons E2020 E2021 E2022 E2023 E2024 E2025 --min-interval 2.5
"""

from __future__ import annotations

import argparse
import time

from engine.data import EuroleagueClient, fetch_season


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition", default="E")
    parser.add_argument(
        "--seasons",
        nargs="+",
        default=["E2020", "E2021", "E2022", "E2023", "E2024", "E2025"],
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=2.5,
        help="Seconds between live network calls (only applies to cache misses)",
    )
    args = parser.parse_args()

    client = EuroleagueClient(min_interval=args.min_interval)

    grand_total = 0
    t0 = time.monotonic()
    for season in args.seasons:
        print(f"\n=== {season} ===", flush=True)
        rows = fetch_season(client, args.competition, season)
        grand_total += len(rows)
        print(f"{season}: {len(rows)} player-game rows now cached", flush=True)

    elapsed = time.monotonic() - t0
    print(
        f"\nDone. {grand_total} player-game rows across {len(args.seasons)} seasons "
        f"(this run took {elapsed / 60:.1f} min; already-cached games were instant)."
    )


if __name__ == "__main__":
    main()
