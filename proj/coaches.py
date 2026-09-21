"""Per-team-season head coach identity - spec §3.3's `coach_changed`
feature and §3.6's coach-concentration index.

Sourced from the `coach` field already present in every cached per-game
stats response under raw/v2_stats/ (confirmed live 2026-09-21 - see
reports/data_audit.md §2) but discarded by engine.data.normalize_v2 before
it reaches the DB. This reads the raw JSON cache directly rather than
duplicating engine's ingestion pipeline for one extra field engine/ itself
doesn't need.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd

from engine.data import EuroleagueClient

RAW_STATS_ROOT = Path(__file__).parent.parent / "raw" / "v2_stats"


def _game_side_teams(games: pd.DataFrame, season: str) -> dict[int, dict[str, str]]:
    """game_code -> {"local": team_code, "road": team_code}, derived from
    player_game_stats' own team/home_away columns - NOT from the stats
    payload's "team" key, which is a stats-totals row, not club identity
    (confirmed while building this: engine.data.normalize_v2 gets the club
    code from the games-list response, not the per-game stats response
    that's cached under raw/v2_stats - so that's what has to be joined
    against here too)."""
    df = games[(games["season_code"] == season) & (games["played"])]
    df = df.drop_duplicates(["game_code", "home_away"])
    out: dict[int, dict[str, str]] = {}
    side_key = {"home": "local", "away": "road"}
    for _, row in df.iterrows():
        out.setdefault(row["game_code"], {})[side_key[row["home_away"]]] = row["team"]
    return out


def coach_per_game(games: pd.DataFrame, season: str, raw_stats_root: Path = RAW_STATS_ROOT) -> pd.DataFrame:
    records = []
    season_dir = raw_stats_root / "E" / season
    if not season_dir.exists():
        return pd.DataFrame(columns=["season", "game_code", "team", "coach_code", "coach_name"])
    side_teams = _game_side_teams(games, season)
    for f in season_dir.glob("*.json"):
        game_code = int(f.stem)
        teams = side_teams.get(game_code)
        if not teams:
            continue
        with open(f) as fh:
            d = json.load(fh)
        for side in ("local", "road"):
            team = teams.get(side)
            coach = (d.get(side) or {}).get("coach") or {}
            if team and coach.get("name"):
                records.append({
                    "season": season, "game_code": game_code, "team": team,
                    "coach_code": coach.get("code"), "coach_name": coach.get("name"),
                })
    return pd.DataFrame(records)


def season_primary_coach(games: pd.DataFrame, season: str, raw_stats_root: Path = RAW_STATS_ROOT) -> dict[str, str]:
    """team -> the coach who coached the most games that season - handles
    an in-season firing without needing to track exact swap dates. This is
    a retrospective label (the season's *dominant* coach); a true live
    prediction made before a season starts would instead use the
    club-announced incoming coach, which this can't reconstruct from
    historical box scores alone - noted as a limitation, not silently
    assumed away."""
    df = coach_per_game(games, season, raw_stats_root)
    if df.empty:
        return {}
    return {team: Counter(g["coach_name"]).most_common(1)[0][0] for team, g in df.groupby("team")}


def current_season_coaches(competition: str, season_code: str) -> dict[str, str]:
    """Live /people, type == 'E' (Coach) - confirmed live 2026-09-21
    (20/20 E2026 teams)."""
    client = EuroleagueClient()
    payload = client.list_people(competition, season_code)
    out: dict[str, str] = {}
    for p in payload:
        if p.get("type") == "E" and p.get("active"):
            team = (p.get("club") or {}).get("code")
            name = (p.get("person") or {}).get("name")
            if team and name:
                out[team] = name
    return out
