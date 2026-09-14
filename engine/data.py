"""
Shared data layer: talks to the public (undocumented) EuroLeague endpoints,
caches raw responses to disk, and normalizes box scores into a flat
one-row-per-player-game table.

v2 (`api-live.euroleague.net`) is the primary source - confirmed to cover
history back to E2000. The legacy `live.euroleague.net/api/Boxscore` route is
kept only as a same-season fallback/cross-check: probing showed it returns
HTTP 200 with an empty body for old seasons, so it is not a historical source.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

V2_BASE = "https://api-live.euroleague.net/v2/competitions"
LEGACY_BOXSCORE_URL = "https://live.euroleague.net/api/Boxscore"

RAW_DIR = Path(__file__).parent.parent / "raw"
MIN_REQUEST_INTERVAL = 6.5  # seconds between live network calls (be polite)

FIELDNAMES = [
    "source",
    "season_code",
    "game_code",
    "round",
    "game_date",
    "team",
    "opponent",
    "home_away",
    "team_win",
    "player_id",
    "player_name",
    "position",
    "is_starter",
    "played",
    "minutes_seconds",
    "points",
    "fg2m",
    "fg2a",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "reb",
    "assists",
    "steals",
    "turnovers",
    "blocks",
    "blocks_against",
    "fouls_committed",
    "fouls_drawn",
    "plus_minus",
    "pir_official",
    "pir_recomputed",
    "pir_diff",
]

POSITION_NAMES = {1: "Guard", 2: "Forward", 3: "Center"}


class EuroleagueClient:
    """Thin wrapper over the two public EuroLeague endpoint families.

    Every successful response is cached to disk under raw/, so re-running
    scripts does not re-hit the network for games already fetched.
    """

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "euroleague-fantasy-explore/0.1 (personal project)",
                "Accept": "application/json",
            }
        )
        self.min_interval = min_interval
        self._last_request = 0.0

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(6):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

            resp = self.session.get(url, params=params, timeout=30)
            self._last_request = time.monotonic()

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(60.0, 5.0 * (2**attempt))
                print(f"  [rate limited] sleeping {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                continue

            if resp.status_code >= 500:
                delay = min(60.0, 5.0 * (2**attempt))
                print(f"  [server error {resp.status_code}] sleeping {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                continue

            if resp.status_code == 404:
                return None

            resp.raise_for_status()

            if not resp.content:
                # Some endpoints (notably the legacy Boxscore route on very
                # old seasons) return HTTP 200 with an empty body instead of
                # 404 when the data doesn't exist.
                return None

            return resp.json()

        raise RuntimeError(f"Gave up after repeated failures: {url}")

    def _cached(self, cache_path: Path, fetch_fn) -> Any:
        if cache_path.exists():
            return json.loads(cache_path.read_text())

        data = fetch_fn()
        if data is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data))
        return data

    def list_games(self, competition: str, season_code: str, limit: int = 1000) -> list[dict]:
        cache_path = RAW_DIR / "v2_games" / competition / f"{season_code}_limit{limit}.json"
        url = f"{V2_BASE}/{competition}/seasons/{season_code}/games"
        data = self._cached(cache_path, lambda: self._get_json(url, params={"limit": limit}))
        return (data or {}).get("data", [])

    def game_stats_v2(self, competition: str, season_code: str, game_code: int) -> dict | None:
        cache_path = RAW_DIR / "v2_stats" / competition / season_code / f"{game_code}.json"
        url = f"{V2_BASE}/{competition}/seasons/{season_code}/games/{game_code}/stats"
        return self._cached(cache_path, lambda: self._get_json(url))

    def game_boxscore_legacy(self, season_code: str, game_code: int) -> dict | None:
        cache_path = RAW_DIR / "legacy_boxscore" / season_code / f"{game_code}.json"
        return self._cached(
            cache_path,
            lambda: self._get_json(
                LEGACY_BOXSCORE_URL, params={"gamecode": game_code, "seasoncode": season_code}
            ),
        )

    def list_people(self, competition: str, season_code: str, limit: int = 1000) -> list[dict]:
        """Current roster + staff for a season - club, position, active flag
        per person. Deliberately NOT disk-cached like everything else above:
        this is mutable current-state data (rosters change on transfers),
        not an immutable historical record, so every call re-fetches live
        (still paced the same as any other request)."""
        url = f"{V2_BASE}/{competition}/seasons/{season_code}/people"
        data = self._get_json(url, params={"limit": limit})
        return (data or {}).get("data", [])


def recompute_pir(row: dict) -> int:
    return (
        row["points"]
        + row["reb"]
        + row["assists"]
        + row["steals"]
        + row["blocks"]
        + row["fouls_drawn"]
        - ((row["fg2a"] + row["fg3a"]) - (row["fg2m"] + row["fg3m"]))
        - (row["fta"] - row["ftm"])
        - row["turnovers"]
        - row["blocks_against"]
        - row["fouls_committed"]
    )


def _finalize_row(row: dict) -> dict:
    row["pir_recomputed"] = recompute_pir(row)
    row["pir_diff"] = row["pir_official"] - row["pir_recomputed"]
    return row


def normalize_v2(
    payload: dict,
    season_code: str,
    game_code: int,
    round_no,
    game_date,
    local_code,
    road_code,
    team_win: dict[str, bool | None],
) -> list[dict]:
    rows: list[dict] = []
    sides = [("local", local_code, road_code), ("road", road_code, local_code)]

    for side_key, team_code, opp_code in sides:
        side = payload.get(side_key)
        if not side:
            continue

        for p in side.get("players", []):
            stats = p["stats"]
            person = p["player"]["person"]
            position_code = p["player"].get("position")

            row = {
                "source": "v2",
                "season_code": season_code,
                "game_code": game_code,
                "round": round_no,
                "game_date": game_date,
                "team": team_code,
                "opponent": opp_code,
                "home_away": "home" if side_key == "local" else "away",
                "team_win": team_win.get(team_code),
                "player_id": person.get("code"),
                "player_name": person.get("name"),
                "position": POSITION_NAMES.get(position_code, p["player"].get("positionName")),
                "is_starter": bool(stats.get("startFive")),
                "played": (stats.get("timePlayed") or 0) > 0,
                "minutes_seconds": int(stats.get("timePlayed") or 0),
                "points": stats.get("points") or 0,
                "fg2m": stats.get("fieldGoalsMade2") or 0,
                "fg2a": stats.get("fieldGoalsAttempted2") or 0,
                "fg3m": stats.get("fieldGoalsMade3") or 0,
                "fg3a": stats.get("fieldGoalsAttempted3") or 0,
                "ftm": stats.get("freeThrowsMade") or 0,
                "fta": stats.get("freeThrowsAttempted") or 0,
                "oreb": stats.get("offensiveRebounds") or 0,
                "dreb": stats.get("defensiveRebounds") or 0,
                "reb": stats.get("totalRebounds") or 0,
                "assists": stats.get("assistances") or 0,
                "steals": stats.get("steals") or 0,
                "turnovers": stats.get("turnovers") or 0,
                "blocks": stats.get("blocksFavour") or 0,
                "blocks_against": stats.get("blocksAgainst") or 0,
                "fouls_committed": stats.get("foulsCommited") or 0,
                "fouls_drawn": stats.get("foulsReceived") or 0,
                "plus_minus": stats.get("plusMinus") or 0,
                "pir_official": stats.get("valuation") or 0,
            }
            # normalize numeric floats coming from the API (e.g. 1.0) to ints
            for k in row:
                if isinstance(row[k], float) and row[k].is_integer():
                    row[k] = int(row[k])

            rows.append(_finalize_row(row))

    return rows


def normalize_legacy(payload: dict, season_code: str, game_code: int) -> list[dict]:
    rows: list[dict] = []
    teams = payload.get("Stats", [])
    team_codes = [t.get("Team") for t in teams]

    for i, team_block in enumerate(teams):
        team_code = team_block.get("Team")
        opp_code = team_codes[1 - i] if len(team_codes) == 2 else None

        for p in team_block.get("PlayersStats", []):
            player_id = (p.get("Player_ID") or "").strip() or None
            if not player_id:
                continue  # skip the blank "team totals" pseudo-row (tmr)

            minutes_raw = p.get("Minutes")
            if minutes_raw in (None, "", "DNP"):
                minutes_seconds = 0
            else:
                mm, ss = minutes_raw.split(":")
                minutes_seconds = int(mm) * 60 + int(ss)

            row = {
                "source": "legacy",
                "season_code": season_code,
                "game_code": game_code,
                "round": None,
                "game_date": None,
                "team": team_code,
                "opponent": opp_code,
                "home_away": "home" if i == 0 else "away",
                "team_win": None,  # not derived from this endpoint (rare fallback path)
                "player_id": player_id,
                "player_name": p.get("Player"),
                "position": None,  # not present in the legacy response
                "is_starter": bool(p.get("IsStarter")),
                "played": bool(p.get("IsPlaying")),
                "minutes_seconds": minutes_seconds,
                "points": p.get("Points") or 0,
                "fg2m": p.get("FieldGoalsMade2") or 0,
                "fg2a": p.get("FieldGoalsAttempted2") or 0,
                "fg3m": p.get("FieldGoalsMade3") or 0,
                "fg3a": p.get("FieldGoalsAttempted3") or 0,
                "ftm": p.get("FreeThrowsMade") or 0,
                "fta": p.get("FreeThrowsAttempted") or 0,
                "oreb": p.get("OffensiveRebounds") or 0,
                "dreb": p.get("DefensiveRebounds") or 0,
                "reb": p.get("TotalRebounds") or 0,
                "assists": p.get("Assistances") or 0,
                "steals": p.get("Steals") or 0,
                "turnovers": p.get("Turnovers") or 0,
                "blocks": p.get("BlocksFavour") or 0,
                "blocks_against": p.get("BlocksAgainst") or 0,
                "fouls_committed": p.get("FoulsCommited") or 0,
                "fouls_drawn": p.get("FoulsReceived") or 0,
                "plus_minus": p.get("Plusminus") or 0,
                "pir_official": p.get("Valuation") or 0,
            }
            rows.append(_finalize_row(row))

    return rows


def normalize_people(payload: list[dict], season_code: str) -> list[dict]:
    """Flatten a list_people() response down to one row per current player-
    club assignment. The /people endpoint returns every person type attached
    to a season (players, coaches, referees, team staff, scorers...) -
    `type == "J"` (`typeName == "Player"`) is the filter that keeps only
    actual players, confirmed against a live E2026 response.

    A player who transferred mid-window shows up **twice** - once per club
    (their old club's row has `active: False`, an `endDate` in the past),
    confirmed on a live E2026 response (e.g. player 012613, ULK inactive +
    MAD active). Filtering to `active` rows handles that in the normal case;
    a rare case also observed live had two simultaneous `active: True` rows
    for the same player (2 of 332 on that pull) - broken ties by latest
    `startDate` there rather than raising, since this is unauthenticated/
    undocumented upstream data (see docs/technical_notes.md) and a transient
    dual-active state during a transfer is more likely than a bug worth
    failing the whole sync over."""
    by_player: dict[str, tuple[str, dict]] = {}

    for p in payload:
        if p.get("type") != "J" or not p.get("active"):
            continue

        person = p.get("person") or {}
        club = p.get("club") or {}
        player_id = person.get("code")
        if not player_id:
            continue

        start_date = p.get("startDate") or ""
        prior = by_player.get(player_id)
        if prior is not None and prior[0] >= start_date:
            continue

        by_player[player_id] = (
            start_date,
            {
                "season_code": season_code,
                "player_id": player_id,
                "player_name": person.get("name"),
                "position": POSITION_NAMES.get(p.get("position"), p.get("positionName")),
                "team": club.get("code"),
                "team_name": club.get("name"),
                "dorsal": p.get("dorsal") or None,
                "active": True,
            },
        )

    return [row for _, row in by_player.values()]


def fetch_season(
    client: EuroleagueClient,
    competition: str,
    season_code: str,
    min_round: int | None = None,
    max_round: int | None = None,
    verbose: bool = True,
) -> list[dict]:
    """Fetch played games of a season as flat player-game rows.

    Each game's box score is its own API call (~6.5s paced), so for a POC
    pass min_round/max_round to bound the fetch to a round window instead of
    the whole ~400-game season (which would take ~40+ minutes on a cold
    cache). The games-list call itself is cheap (one request) and always
    covers the full season, so callers can inspect round ranges first.
    """
    games = client.list_games(competition, season_code, limit=1000)
    played_games = [g for g in games if g.get("played")]

    if min_round is not None:
        played_games = [g for g in played_games if g.get("round", 0) >= min_round]
    if max_round is not None:
        played_games = [g for g in played_games if g.get("round", 0) <= max_round]

    if verbose:
        print(f"{season_code}: {len(played_games)} played games to fetch/load")

    all_rows: list[dict] = []
    v2_failures = 0

    for i, g in enumerate(played_games):
        game_code = g["gameCode"]
        round_no = g.get("round")
        game_date = g.get("date")
        local_code = g["local"]["club"]["code"]
        road_code = g["road"]["club"]["code"]
        local_score = g["local"].get("score")
        road_score = g["road"].get("score")

        team_win: dict[str, bool | None] = {local_code: None, road_code: None}
        if isinstance(local_score, (int, float)) and isinstance(road_score, (int, float)) and local_score != road_score:
            team_win[local_code] = local_score > road_score
            team_win[road_code] = road_score > local_score

        stats_v2 = client.game_stats_v2(competition, season_code, game_code)
        if stats_v2:
            rows = normalize_v2(
                stats_v2, season_code, game_code, round_no, game_date, local_code, road_code, team_win
            )
        else:
            v2_failures += 1
            legacy = client.game_boxscore_legacy(season_code, game_code)
            if not legacy:
                if verbose:
                    print(f"  game {game_code}: no data from either source, skipping")
                continue
            rows = normalize_legacy(legacy, season_code, game_code)

        all_rows.extend(rows)

        if verbose and (i + 1) % 50 == 0:
            print(f"  ...{i + 1}/{len(played_games)} games processed")

    if verbose:
        print(f"{season_code}: {len(all_rows)} player-game rows, {v2_failures} v2 fallbacks")

    return all_rows
