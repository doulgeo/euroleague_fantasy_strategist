"""
Sync the EuroLeague injury report (euroleague.db `injuries` table) from
basketnews.com - a third-party page (not the EuroLeague API), updated daily,
tracking each club's current player availability (Out/Doubtful/
Questionable/Uncertain/Game-time/Expected/Ready). See engine/injuries.py for
how this feeds into the draft board/lineup builder.

Not disk-cached, like sync_rosters.py/sync_fantasy_pool.py - every run
re-fetches live, since the whole point is that it changes often.

Run this whenever you want current availability info - no fixed cadence,
but daily-ish if you're checking before finalizing a lineup.

Usage:
    python sync_injuries.py
"""

from __future__ import annotations

import argparse

from engine.db import all_known_players, get_connection, load_roster, replace_injuries
from engine.injuries import fetch_injury_report_html, parse_injury_report, resolve_injury_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default="E2026", help="Season to resolve player IDs against (rosters table).")
    args = parser.parse_args()

    html = fetch_injury_report_html()
    rows = parse_injury_report(html)

    conn = get_connection()
    n = replace_injuries(conn, rows)
    print(f"{n} injury report rows synced")

    roster_rows = load_roster(conn, args.season)
    historical_players = all_known_players(conn)
    _resolved, diag = resolve_injury_rows(rows, roster_rows, historical_players)
    print(
        f"Resolved {diag['total']} listed players: "
        f"{diag['matched_current_roster']} matched this season's synced roster, "
        f"{diag['matched_historical']} matched an existing player_id from a prior season, "
        f"{len(diag['synthetic'])} unresolved"
    )
    if diag["synthetic"]:
        for label in diag["synthetic"][:30]:
            print(f"  - {label}")
        if len(diag["synthetic"]) > 30:
            print(f"  ... and {len(diag['synthetic']) - 30} more")

    conn.close()


if __name__ == "__main__":
    main()
