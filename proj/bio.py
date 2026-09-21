"""Player biographical data (birth date, height, weight) for age-based
features in proj/minutes.py.

Sourced two ways (see reports/data_audit.md §2 - this is the "age" gap
that audit flagged as resolvable without a user-supplied roster CSV):
- For anyone with box-score history: every already-cached per-game stats
  file under raw/v2_stats/ carries `player.person.birthDate` - immutable
  data, so scanning the cache once and keeping the first value seen per
  player_id is exact, not an approximation.
- For a true newcomer with zero box-score history anywhere (so never
  appears in raw/v2_stats/), one live /people call (confirmed live
  2026-09-21: 307/307 active E2026 players had birthDate) fills the rest.
  This is the only network call in this module, and it's cached to
  parquet afterward - a birth date never changes, so there's never a
  reason to refetch someone already in the cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from engine.data import EuroleagueClient

RAW_STATS_ROOT = Path(__file__).parent.parent / "raw" / "v2_stats"


def bio_from_raw_cache(raw_stats_root: Path = RAW_STATS_ROOT) -> pd.DataFrame:
    records: dict[str, dict] = {}
    if not raw_stats_root.exists():
        return pd.DataFrame(columns=["player_id", "birth_date", "height", "weight"])

    for json_path in raw_stats_root.glob("*/*/*.json"):
        with open(json_path) as fh:
            payload = json.load(fh)
        for side in ("local", "road"):
            for p in (payload.get(side) or {}).get("players", []):
                person = (p.get("player") or {}).get("person") or {}
                player_id = person.get("code")
                if player_id and player_id not in records:
                    records[player_id] = {
                        "player_id": player_id,
                        "birth_date": person.get("birthDate"),
                        "height": person.get("height"),
                        "weight": person.get("weight"),
                    }
    return pd.DataFrame(records.values())


def bio_from_live_people(competition: str, season_code: str, known_ids: set[str]) -> pd.DataFrame:
    client = EuroleagueClient()
    payload = client.list_people(competition, season_code)
    records = []
    for p in payload:
        if p.get("type") != "J" or not p.get("active"):
            continue
        person = p.get("person") or {}
        player_id = person.get("code")
        if player_id and player_id not in known_ids:
            records.append({
                "player_id": player_id,
                "birth_date": person.get("birthDate"),
                "height": person.get("height"),
                "weight": person.get("weight"),
            })
    return pd.DataFrame(records)


def build_player_bio(
    cache_path: Path | str | None = None,
    competition: str = "E",
    season_code: str = "E2026",
    fetch_live: bool = True,
) -> pd.DataFrame:
    bio = bio_from_raw_cache()
    if fetch_live:
        known_ids = set(bio["player_id"]) if not bio.empty else set()
        live = bio_from_live_people(competition, season_code, known_ids)
        bio = pd.concat([bio, live], ignore_index=True) if not live.empty else bio
    bio = bio.drop_duplicates("player_id").reset_index(drop=True)
    bio["birth_date"] = pd.to_datetime(bio["birth_date"])
    if cache_path:
        bio.to_parquet(cache_path, index=False)
    return bio


def age_at(bio: pd.DataFrame, player_id: str, as_of) -> float | None:
    row = bio[bio["player_id"] == player_id]
    if row.empty or pd.isna(row.iloc[0]["birth_date"]):
        return None
    birth_date = row.iloc[0]["birth_date"]
    return (pd.Timestamp(as_of) - birth_date).days / 365.25
