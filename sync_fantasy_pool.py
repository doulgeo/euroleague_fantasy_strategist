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

from engine.db import get_connection, load_roster, replace_fantasy_pool
from engine.fantasy_pool import eligible_player_ids, fetch_pool_csv, parse_pool_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default="E2026", help="Season to check match quality against (rosters table).")
    args = parser.parse_args()

    text = fetch_pool_csv()
    rows = parse_pool_csv(text)

    conn = get_connection()
    n = replace_fantasy_pool(conn, rows)
    print(f"{n} players synced to fantasy_pool")

    roster_rows = load_roster(conn, args.season)
    if roster_rows:
        _eligible, diag = eligible_player_ids(rows, roster_rows)
        print(
            f"Match check against {args.season} rosters: {diag['matched']}/{len(roster_rows)} "
            f"current roster players matched to the fantasy pool"
        )
        if diag["unmatched"]:
            print(f"{len(diag['unmatched'])} fantasy-pool rows didn't match any current roster player:")
            for label in diag["unmatched"][:30]:
                print(f"  - {label}")
            if len(diag["unmatched"]) > 30:
                print(f"  ... and {len(diag['unmatched']) - 30} more")
        if diag["ambiguous"]:
            print(f"{len(diag['ambiguous'])} fantasy-pool rows matched more than one same-surname/same-team player, skipped:")
            for label in diag["ambiguous"]:
                print(f"  - {label}")
    else:
        print(f"No {args.season} roster synced yet (run sync_rosters.py first) - skipping match check.")

    conn.close()


if __name__ == "__main__":
    main()
