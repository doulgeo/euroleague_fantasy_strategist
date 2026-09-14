"""
Sync current-season club rosters (euroleague.db `rosters` table) from the
EuroLeague v2 /people endpoint.

Unlike sync_db.py (player_game_stats - an immutable historical log,
upsert-only), this is a full snapshot replace each run: a club roster is
current state, not history, so a player who's been dropped/left a club
should stop appearing rather than linger as a stale row. Not disk-cached
like box scores either (engine.data.EuroleagueClient.list_people) - every
run re-fetches live, since a roster can change at any time (a transfer),
not just after a played game.

Run this whenever you want fresh team/new-player info - before a draft, or
after summer transfer news breaks - it's not tied to gameweeks the way
sync_db.py is.

Usage:
    python sync_rosters.py --season E2026
"""

from __future__ import annotations

import argparse

from engine.data import EuroleagueClient, normalize_people
from engine.db import get_connection, known_player_ids, replace_roster


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competition", default="E")
    parser.add_argument("--season", default="E2026")
    args = parser.parse_args()

    client = EuroleagueClient()
    people = client.list_people(args.competition, args.season)
    rows = normalize_people(people, args.season)

    conn = get_connection()
    n = replace_roster(conn, args.season, rows)

    seen = known_player_ids(conn)
    new_count = sum(1 for r in rows if r["player_id"] not in seen)
    skipped = len(people) - len(rows)

    print(
        f"{args.season}: {n} players synced to rosters "
        f"({skipped} non-player staff/officials filtered out, {new_count} new to the league)"
    )
    conn.close()


if __name__ == "__main__":
    main()
