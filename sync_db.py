"""
Sync euroleague.db (SQLite) from the EuroLeague API.

fetch_season only makes network calls for games not already cached under
raw/ (see engine.data.EuroleagueClient), and upsert_rows is idempotent
(keyed on season_code/game_code/player_id) - so this is the one command to
re-run after every gameweek: it picks up newly played games and writes them
into the DB, without touching or duplicating anything already there.

Also refreshes the `schedule` table (every game in the season, played or
not - see engine.db's schedule comment block) each run: fetch_season
already fetches the season's game list internally to decide what to fetch,
so calling list_games again here hits the same disk cache
(raw/v2_games/...), not the network, even on an otherwise-cold run.

Usage:
    python sync_db.py --seasons E2025                            # after a gameweek
    python sync_db.py --seasons E2023 E2024 E2025                # full (re)load
"""

from __future__ import annotations

import argparse

from engine.data import EuroleagueClient, fetch_season, normalize_schedule
from engine.db import get_connection, replace_schedule, row_count, upsert_rows


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

        games = client.list_games(args.competition, season)
        schedule_rows = normalize_schedule(games, season)
        n_sched = replace_schedule(conn, season, schedule_rows)
        played_n = sum(1 for r in schedule_rows if r["played"])
        print(f"{season}: schedule refreshed - {n_sched} games ({played_n} played, {n_sched - played_n} upcoming)")

    conn.close()


if __name__ == "__main__":
    main()
