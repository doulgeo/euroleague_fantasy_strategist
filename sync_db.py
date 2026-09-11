"""
Sync euroleague.db (SQLite) from the EuroLeague API.

fetch_season only makes network calls for games not already cached under
raw/ (see engine.data.EuroleagueClient), and upsert_rows is idempotent
(keyed on season_code/game_code/player_id) - so this is the one command to
re-run after every gameweek: it picks up newly played games and writes them
into the DB, without touching or duplicating anything already there.

Usage:
    python sync_db.py --seasons E2025                            # after a gameweek
    python sync_db.py --seasons E2023 E2024 E2025                # full (re)load
"""

from __future__ import annotations

import argparse

from engine.data import EuroleagueClient, fetch_season
from engine.db import get_connection, row_count, upsert_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition", default="E")
    parser.add_argument("--seasons", nargs="+", default=["E2025"])
    parser.add_argument("--min-interval", type=float, default=2.5)
    args = parser.parse_args()

    client = EuroleagueClient(min_interval=args.min_interval)
    conn = get_connection()

    for season in args.seasons:
        print(f"\n=== {season} ===", flush=True)
        rows = fetch_season(client, args.competition, season)
        n = upsert_rows(conn, rows)
        print(f"{season}: upserted {n} rows (DB now has {row_count(conn, season)} rows for this season)")

    conn.close()


if __name__ == "__main__":
    main()
