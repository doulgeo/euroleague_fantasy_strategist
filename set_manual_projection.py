"""
Set (or clear) a manual projection override for a player with no
EuroLeague box-score history anywhere in the local DB - e.g. a mid-season
transfer from another league, where engine.projections.build_projections
has nothing to compute a real projection from and engine.rosters.merge_roster
would otherwise show a flat 0.0.

This project deliberately has no ML/automated scoring for this case (see
CLAUDE.md, "Explicitly NOT done yet" - new-player valuation). The intended
workflow is: ask a Claude Code session to research the player (recent form,
role, minutes expectation - e.g. via web search on news/other-league stats)
and propose a projected PIR + reasoning, then run this script to persist
whatever number you settle on. It's a plain data-entry tool - it has no
research logic of its own.

Player lookup matches by case-insensitive substring against player_name in
this season's synced roster (`rosters`, sync_rosters.py) and any
historically-known player_id (player_game_stats) - the same identity
sources engine.rosters.merge_roster itself draws on. An ambiguous or
no-match query lists what it found and exits without writing anything,
consistent with this project's "flag rather than silently guess" approach
to player identity.

Usage:
    python set_manual_projection.py "PAVLOVIC, DUSAN" 14.5 --note "Averaged 16 PIR/36min in the Turkish league before mid-season EuroLeague move; projecting a discount for the role/system change (sourced 2026-09-20)."
    python set_manual_projection.py "PAVLOVIC" 14.5   # substring match, case-insensitive
    python set_manual_projection.py --clear "PAVLOVIC, DUSAN"
    python set_manual_projection.py --list
"""

from __future__ import annotations

import argparse

from engine.db import (
    all_known_players,
    clear_manual_projection,
    get_connection,
    load_manual_projections,
    load_roster,
    set_manual_projection,
)

CURRENT_SEASON = "E2026"


def find_candidates(conn, query: str) -> list[dict]:
    q = query.strip().lower()
    by_id: dict[str, dict] = {}

    for r in load_roster(conn, CURRENT_SEASON):
        if q in r["player_name"].lower():
            by_id[r["player_id"]] = {"player_id": r["player_id"], "player_name": r["player_name"], "team": r.get("team")}

    for r in all_known_players(conn):
        if q in r["player_name"].lower() and r["player_id"] not in by_id:
            by_id[r["player_id"]] = {"player_id": r["player_id"], "player_name": r["player_name"], "team": None}

    return sorted(by_id.values(), key=lambda r: r["player_name"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="?", help="Player name (or substring) to match")
    parser.add_argument("projected_pir", nargs="?", type=float, help="Projected PIR estimate to set")
    parser.add_argument("--note", default=None, help="Reasoning/sources for this estimate")
    parser.add_argument("--clear", action="store_true", help="Remove an existing manual override instead of setting one")
    parser.add_argument("--list", action="store_true", help="List all current manual overrides and exit")
    args = parser.parse_args()

    conn = get_connection()

    if args.list:
        overrides = load_manual_projections(conn)
        if not overrides:
            print("No manual projection overrides set.")
        for pid, row in sorted(overrides.items(), key=lambda kv: kv[1]["player_name"]):
            note = f" - {row['note']}" if row["note"] else ""
            print(f"{row['player_name']} ({pid}): {row['projected_pir']:.1f}{note} [set {row['set_at'][:10]}]")
        conn.close()
        return

    if not args.query:
        parser.error("query is required unless --list is given")

    candidates = find_candidates(conn, args.query)
    if not candidates:
        print(f"No player found matching {args.query!r}.")
        conn.close()
        return
    if len(candidates) > 1:
        print(f"{len(candidates)} players match {args.query!r} - be more specific:")
        for c in candidates:
            print(f"  {c['player_name']} ({c['player_id']}) - {c['team'] or 'no current team on file'}")
        conn.close()
        return

    player = candidates[0]

    if args.clear:
        removed = clear_manual_projection(conn, player["player_id"])
        print(f"{'Cleared' if removed else 'No override found for'} {player['player_name']} ({player['player_id']}).")
        conn.close()
        return

    if args.projected_pir is None:
        parser.error("projected_pir is required unless --clear or --list is given")

    set_manual_projection(conn, player["player_id"], player["player_name"], args.projected_pir, args.note)
    print(f"Set {player['player_name']} ({player['player_id']}) to a manual projection of {args.projected_pir:.1f}.")
    if args.note:
        print(f"Note: {args.note}")
    conn.close()


if __name__ == "__main__":
    main()
