"""
Bulk-import a completed draft from a third-party draft app's CSV export,
instead of logging 156 picks one at a time through /draft.

The export format this was built against (a friend's draft-room tool) is a
pick-by-pick log - one row per pick, columns including manager_id/manager
(the app's own manager identity, not this project's), player/team/pos, and
overall_pick - optionally followed by a second "Final rosters" section and
a blank-line separator. Both are ignored beyond the point where a data row
stops matching the header's column count or a required field goes blank,
so this only ever reads the always-present pick-log section and never needs
to special-case the trailer.

Player identity: the export has its own player_id (that app's UUID, not
this project's), so players are matched the same way as the Fantasy sheet
sync (engine.fantasy_pool.resolve_pool_rows) - by normalized (team, surname,
first name) against this season's synced roster, then against any
historical player_id this project has ever seen, falling back to a stable
synthetic ID (shared with the Fantasy sheet path, so the same real-world
player resolves to the same placeholder either way) rather than guessing.
The export's own team codes were confirmed to match the Fantasy sheet's
codes exactly (same 20 clubs, same abbreviations), so
engine.fantasy_pool.TEAM_CODE_MAP is reused as-is.
"""

from __future__ import annotations

import csv
import io

from engine.fantasy_pool import TEAM_CODE_MAP, resolve_pool_rows

REQUIRED_COLUMNS = {"manager_id", "manager", "player", "team", "pos", "overall_pick"}

_POSITION_MAP = {"G": "Guard", "F": "Forward", "C": "Center"}


class DraftCsvError(ValueError):
    """The uploaded file doesn't look like a draft-app pick export."""


def parse_draft_csv(text: str) -> list[dict]:
    """One dict per pick: manager_key, manager_name, player_name, team_raw,
    pos_raw, overall_pick (int) - in file order. Stops at the first row
    that doesn't have the same number of fields as the header (the blank
    separator line or the "Final rosters" trailer both fail this) or has a
    blank manager_id/non-numeric overall_pick, so only the pick-log section
    is ever read.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DraftCsvError("The uploaded file is empty.") from None

    header = [h.strip().lstrip("﻿") for h in header]
    missing = REQUIRED_COLUMNS - set(header)
    if missing:
        raise DraftCsvError(
            f"Missing expected column(s): {', '.join(sorted(missing))}. "
            f"This doesn't look like a draft pick export."
        )
    idx = {name: header.index(name) for name in header}

    rows = []
    for raw in reader:
        if len(raw) != len(header):
            break
        manager_key = raw[idx["manager_id"]].strip()
        if not manager_key:
            break
        try:
            overall_pick = int(raw[idx["overall_pick"]].strip())
        except ValueError:
            break
        rows.append({
            "manager_key": manager_key,
            "manager_name": raw[idx["manager"]].strip(),
            "player_name": raw[idx["player"]].strip(),
            "team_raw": raw[idx["team"]].strip(),
            "pos_raw": raw[idx["pos"]].strip(),
            "overall_pick": overall_pick,
        })

    if not rows:
        raise DraftCsvError("No pick rows found in the uploaded file.")
    return rows


def distinct_managers(rows: list[dict]) -> list[dict]:
    """One entry per distinct manager_key, in first-appearance order:
    {manager_key, manager_name, pick_count}."""
    seen: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        key = r["manager_key"]
        if key not in seen:
            seen[key] = {"manager_key": key, "manager_name": r["manager_name"], "pick_count": 0}
            order.append(key)
        seen[key]["pick_count"] += 1
    return [seen[k] for k in order]


def _split_full_name(full: str) -> tuple[str, str]:
    """(first, surname) from a single "First Last" field - the last
    whitespace-separated token is the surname (works for hyphenated
    surnames like "Horton-Tucker", since the hyphen isn't a space), any
    remaining tokens are the first name(s)."""
    parts = full.strip().split()
    if len(parts) <= 1:
        return "", full.strip()
    return " ".join(parts[:-1]), parts[-1]


def resolve_manager_picks(
    rows: list[dict],
    manager_key: str,
    roster_rows: list[dict],
    historical_players: list[dict],
) -> tuple[list[dict], dict]:
    """Resolves one CSV manager's picks to this project's own player_ids,
    reusing engine.fantasy_pool.resolve_pool_rows. Returns (resolved,
    diagnostics) - resolved rows are {player_id, player_name, position,
    team}, ready for engine.ownership.record_draft_pick. diagnostics adds a
    "skipped" list (rows with an unrecognized position) to
    resolve_pool_rows's usual diagnostics."""
    pool_rows = []
    skipped: list[str] = []
    for r in rows:
        if r["manager_key"] != manager_key:
            continue
        position = _POSITION_MAP.get(r["pos_raw"].upper())
        if position is None:
            skipped.append(f"{r['player_name']} - unrecognized position {r['pos_raw']!r}, skipped")
            continue
        first, surname = _split_full_name(r["player_name"])
        team = TEAM_CODE_MAP.get(r["team_raw"], r["team_raw"])
        pool_rows.append({"name": first, "surname": surname, "position": position, "team": team})

    resolved, diagnostics = resolve_pool_rows(pool_rows, roster_rows, historical_players)
    diagnostics["skipped"] = skipped
    return resolved, diagnostics
