"""
Merges the current-season club roster (engine.db `rosters` table, synced via
sync_rosters.py) into a projections pool built from box-score history
(engine.projections.build_projections).

Three problems this solves that build_projections alone can't:
- A transferred player still shows their OLD team in a projection (team is
  inferred from their most recent box-score row) until they've played a game
  for the new club - the roster table has the authoritative current club
  immediately, independent of games played.
- A player with no EuroLeague box-score history at all - new to the league,
  or just too few games so far this season - is silently excluded from
  build_projections' output (MIN_GAMES_FOR_PROJECTION), so they'd be
  invisible on the draft board. This adds them back in as a placeholder
  Projection (zero projected value - there's nothing to base one on yet)
  so they're visible and markable rather than missing outright.
- The mirror-image problem: a player WITH history who's fallen off every
  current club roster entirely (their club left the competition - e.g.
  Monaco, absent from the confirmed 2026-27 club list - or they were
  released/unsigned) still shows up with their stale old team and a real-
  looking projected value, with nothing marking them as no longer
  acquirable. Flagged as `gone_player_ids` so callers can exclude them from
  anything draftable while still surfacing them (visibly marked) on a
  manager's roster if already owned - see app.py's /draft (filters these
  out) vs. manager_roster route (shows them greyed out, prompting a drop).
"""

from __future__ import annotations

from dataclasses import replace

from engine.projections import Projection


def merge_roster(
    pool: dict[str, Projection],
    roster_rows: list[dict],
    known_player_ids: set[str],
) -> tuple[dict[str, Projection], set[str], set[str]]:
    """Returns (merged_pool, new_player_ids, gone_player_ids).

    new_player_ids = roster players absent from known_player_ids - i.e. no
    box-score history anywhere in the local DB (see engine.db.known_player_ids
    for the "new to the league" caveat: scoped to what's locally synced,
    E2023+, not literally every EuroLeague season ever played).

    gone_player_ids = the opposite case: players already in `pool` (so they
    have box-score history) who are NOT on any club's roster this season
    (`roster_rows`) - no longer part of the competition at all, as far as
    the current roster sync can tell.
    """
    merged = dict(pool)
    new_player_ids: set[str] = set()
    roster_ids = {r["player_id"] for r in roster_rows}

    for r in roster_rows:
        pid = r["player_id"]

        if pid in merged:
            existing = merged[pid]
            if existing.team != r["team"]:
                merged[pid] = replace(existing, team=r["team"], position=existing.position or r["position"])
        else:
            merged[pid] = Projection(
                player_id=pid,
                player_name=r["player_name"],
                position=r["position"],
                team=r["team"],
                games_sampled=0,
                projected_pir=0.0,
                minutes_trend_seconds=0.0,
                volatility=0.0,
                team_win_rate=None,
                projected_pir_with_bonus=0.0,
            )

        if pid not in known_player_ids:
            new_player_ids.add(pid)

    gone_player_ids = {pid for pid in pool if pid not in roster_ids}

    return merged, new_player_ids, gone_player_ids
