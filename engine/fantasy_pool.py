"""
The real EuroLeague Fantasy game's live draftable player pool, sourced from
a user-maintained Google Sheet (not the EuroLeague API) - see
sync_fantasy_pool.py. This is a better ground-truth for "is this player
actually draftable right now" than what engine.rosters derives on its own
from box scores + club rosters: a player can be on a EuroLeague club roster
(engine.rosters' source) without being part of the real Fantasy game's pool
(e.g. Head Coaches, who this league's rules don't include at all - see
docs/game_rules.md), or vice versa.

The sheet has no player ID, only Name/Surname/Position/Team, so matching it
to this project's own player_id (from engine.db `rosters`, which does have
one) happens here, live, by normalized name + team - not at sync time. A
Credits/price column exists in the sheet but is intentionally never read;
draft-credit tracking is out of scope for this project (see CLAUDE.md).

Matching is conservative by design: an unmatched or ambiguous row is
skipped rather than guessed, consistent with this project's existing
"flag rather than silently guess" approach (see the NEW/GONE badges in
engine.rosters). A false "not eligible" would hide a real, draftable
player - worse than an occasional missed match, which just means a legit
player briefly doesn't get the extra eligibility check applied to them.
"""

from __future__ import annotations

import csv
import io
import unicodedata

import requests

SHEET_ID = "1bAs-I9HhsdFh3pj2J7vXYuKZv09AEPcgbMOlow4g0Cc"
SHEET_GID = "0"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"

# The sheet and this project's own `rosters` table (sourced from the
# EuroLeague v2 /people endpoint) use different 3-letter codes for the same
# 20 clubs - confirmed by cross-checking every code against real 2026-27
# EuroLeague rosters. Keyed by the sheet's code; value is this project's
# code (engine.db `rosters.team`).
TEAM_CODE_MAP = {
    "ASV": "ASV",  # LDLC ASVEL Villeurbanne - same code both sides
    "BAR": "BAR",  # FC Barcelona - same code both sides
    "BAY": "MUN",  # FC Bayern Munich
    "BJK": "BES",  # Besiktas
    "CZV": "RED",  # Crvena Zvezda Meridianbet Belgrade (Red Star)
    "DUB": "DUB",  # Dubai Basketball - same code both sides
    "EFS": "IST",  # Anadolu Efes Istanbul
    "FBT": "ULK",  # Fenerbahce Beko Istanbul
    "HTA": "HTA",  # Hapoel Tel Aviv - same code both sides
    "KBA": "BAS",  # Baskonia Vitoria-Gasteiz
    "MIL": "MIL",  # EA7 Emporio Armani Milano - same code both sides
    "MTA": "TEL",  # Maccabi Playtika Tel Aviv
    "OLY": "OLY",  # Olympiacos Piraeus - same code both sides
    "PAO": "PAN",  # Panathinaikos AKTOR Athens
    "PAR": "PAR",  # Partizan Mozzart Bet Belgrade - same code both sides
    "PBB": "PRS",  # Paris Basketball
    "RMB": "MAD",  # Real Madrid
    "VBC": "PAM",  # Valencia Basket (this project's code is the club's older "Pamesa" sponsor name)
    "VIR": "VIR",  # Virtus Segafredo Bologna - same code both sides
    "ZAL": "ZAL",  # Zalgiris Kaunas - same code both sides
}

# The real Fantasy game includes Head Coaches as a draftable category; this
# league's rules don't (see docs/game_rules.md: 5 Guard / 5 Forward /
# 3 Center, no head coach) - filtered out at parse time.
_SKIPPED_POSITIONS = {"Head Coach"}


def fetch_pool_csv() -> str:
    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_pool_csv(text: str) -> list[dict]:
    """Rows ready for engine.db.replace_fantasy_pool: name, surname,
    position, team (mapped to this project's code where known - kept as
    the raw sheet code, unmapped, if a new/unrecognized code shows up, so
    it's still visible rather than silently dropped)."""
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        position = (r.get("Position") or "").strip()
        if position in _SKIPPED_POSITIONS:
            continue
        team_raw = (r.get("Team") or "").strip()
        rows.append({
            "name": (r.get("Name") or "").strip(),
            "surname": (r.get("Surname") or "").strip(),
            "position": position,
            "team": TEAM_CODE_MAP.get(team_raw, team_raw),
        })
    return rows


_SUFFIX_TOKENS = {"JR", "SR", "II", "III", "IV", "V"}


def _normalize_name(s: str) -> str:
    """Uppercase, strip accents, drop everything but letters/spaces
    (hyphens included - treated as spaces, since the two sides disagree on
    them for at least one real player: DB has 'HORTON TUCKER, TALEN' for
    sheet's 'Horton-Tucker'), collapse whitespace, and drop a trailing
    generational suffix (Jr/Sr/II/III/IV/V) - the two sides don't always
    agree on whether/how one is included (e.g. DB 'BACOT JR., ARMANDO' vs
    sheet 'Armando Bacot Jr', or the reverse, DB 'BALDWIN, PATRICK' vs
    sheet 'Patrick Baldwin Jr'). Matches how engine.data already normalizes
    names from the EuroLeague API (accents stripped, e.g. 'LUWAWU-CABARROT,
    TIMOTHE' for 'Timothé Luwawu-Cabarrot')."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    kept = "".join(c if c.isalpha() or c in " -" else " " for c in stripped.upper()).replace("-", " ")
    tokens = kept.split()
    if tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens = tokens[:-1]
    return " ".join(tokens)


def eligible_player_ids(pool_rows: list[dict], roster_rows: list[dict]) -> tuple[set[str], dict]:
    """Matches fantasy_pool rows (name/surname/team, no player_id) against
    engine.db `rosters` rows (player_id + 'SURNAME, FIRST' player_name +
    team) by normalized surname + team, falling back to a first-name check
    only to break a tie among same-surname/same-team teammates. Returns
    (eligible_player_ids, diagnostics) - diagnostics has counts and the
    unmatched sheet rows, meant for sync_fantasy_pool.py's printed summary
    so match quality is visible, not just trusted blindly."""
    by_team_surname: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in roster_rows:
        name = r["player_name"]
        if "," not in name:
            continue
        surname_part, _, first_part = name.partition(",")
        key = (r["team"], _normalize_name(surname_part))
        by_team_surname.setdefault(key, []).append((r["player_id"], _normalize_name(first_part)))

    eligible: set[str] = set()
    unmatched: list[str] = []
    ambiguous: list[str] = []

    for row in pool_rows:
        key = (row["team"], _normalize_name(row["surname"]))
        candidates = by_team_surname.get(key, [])
        label = f"{row['name']} {row['surname']} ({row['team']})"

        if len(candidates) == 1:
            eligible.add(candidates[0][0])
        elif len(candidates) > 1:
            first_norm = _normalize_name(row["name"])
            narrowed = [pid for pid, cand_first in candidates if cand_first == first_norm or cand_first.startswith(first_norm)]
            if len(narrowed) == 1:
                eligible.add(narrowed[0])
            else:
                ambiguous.append(label)
        else:
            unmatched.append(label)

    diagnostics = {
        "total": len(pool_rows),
        "matched": len(eligible),
        "unmatched": unmatched,
        "ambiguous": ambiguous,
    }
    return eligible, diagnostics
