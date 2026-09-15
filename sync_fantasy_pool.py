"""
Sync the real EuroLeague Fantasy draftable player pool (euroleague.db
`fantasy_pool` table) from a user-maintained Google Sheet - not the
EuroLeague API. See engine/fantasy_pool.py for why this exists and how it's
matched to this project's own player IDs.

Not disk-cached (engine.fantasy_pool.fetch_pool_csv) - every run re-fetches
live, since the whole point of this source is that it "should be updated
constantly" (the user's words) as the real draft pool changes.

Run this whenever the sheet changes - like sync_rosters.py, it's not tied
to gameweeks.

Usage:
    python sync_fantasy_pool.py
"""

from __future__ import annotations

import argparse

from engine.db import all_known_players, get_connection, load_roster, replace_fantasy_pool
from engine.fantasy_pool import fetch_pool_csv, parse_pool_csv, resolve_pool_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default="E2026", help="Season to resolve player IDs against (rosters table).")
    args = parser.parse_args()

    text = fetch_pool_csv()
    rows = parse_pool_csv(text)

    conn = get_connection()
    n = replace_fantasy_pool(conn, rows)
    print(f"{n} players synced to fantasy_pool")

    roster_rows = load_roster(conn, args.season)
    historical_players = all_known_players(conn)
    _resolved, diag = resolve_pool_rows(rows, roster_rows, historical_players)
    print(
        f"Resolved {diag['total']} fantasy-pool players: "
        f"{diag['matched_current_roster']} matched this season's synced roster, "
        f"{diag['matched_historical']} matched an existing player_id from a prior season, "
        f"{len(diag['synthetic'])} are new to this project's data (placeholder ID, 0.0 projection)"
    )
    if diag["synthetic"]:
        for label in diag["synthetic"][:30]:
            print(f"  - {label}")
        if len(diag["synthetic"]) > 30:
            print(f"  ... and {len(diag['synthetic']) - 30} more")

    conn.close()


if __name__ == "__main__":
    main()
