"""
The real EuroLeague Fantasy game's live draftable player pool, sourced from
a user-maintained Google Sheet (not the EuroLeague API) - see
sync_fantasy_pool.py. As of 2026-09-15 this is the PRIMARY source of roster
composition (who's on which team, at what position) for the app's player
pool - see app.py's get_pool and engine.rosters.merge_roster. It replaced
EuroLeague's own /people endpoint (engine.db `rosters`) in that role after
the user found /people significantly incomplete pre-season for some clubs
(ASVEL: 4 of ~15-20 real players registered; Barcelona: 7 - confirmed
directly against the raw API, not a bug in this project's sync). `rosters`
stays synced and is still used here, as a fallback identity source (see
resolve_pool_rows) and directly when the sheet itself hasn't been synced
yet (app.py falls back to the old /people-driven composition then).

The sheet has no player ID, only Name/Surname/Position/Team, so matching it
to this project's own player_id happens here, live, by normalized name +
team (not at sync time - resolve_pool_rows). A Credits/price column exists
in the sheet but is intentionally never read; draft-credit tracking is out
of scope for this project (see CLAUDE.md).

Matching is conservative by design: an unmatched or ambiguous row falls
back to a synthetic placeholder ID rather than guessing a real one,
consistent with this project's existing "flag rather than silently guess"
approach (see the NEW/GONE badges in engine.rosters) - a wrong match would
silently merge two different players' histories together.
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


def _synthetic_id(team: str, surname_norm: str, first_norm: str) -> str:
    slug = lambda s: s.lower().replace(" ", "-")
    return f"sheet:{team.lower()}:{slug(surname_norm)}:{slug(first_norm)}"


def resolve_pool_rows(
    pool_rows: list[dict], roster_rows: list[dict], historical_players: list[dict]
) -> tuple[list[dict], dict]:
    """Resolves each Fantasy-sheet row (name/surname/position/team, no
    player_id) to this project's own player_id, so the sheet's team/
    position can drive roster composition (engine.rosters.merge_roster)
    while still reusing a real, existing ID whenever the player is
    already known - keeping their box-score-based projection linked,
    rather than starting them over as a zero-value placeholder just
    because EuroLeague's own /people endpoint hasn't caught up (the
    problem this was built for - ASVEL/Barcelona had only 4-7 of their
    real ~15-20 players registered there pre-season; see
    docs/testing_log.md, 2026-09-15).

    Two-tier lookup, most confident first:
    1. (team, normalized surname) against `roster_rows` (this season's
       EuroLeague-sourced roster) - correct current team, when
       EuroLeague's own data has the player at all.
    2. Only if that finds nothing: (normalized surname, normalized first
       name) against EVERY player_id this project has ever synced
       box-score data for (`historical_players`, any season, any team) -
       catches a player whose club hasn't re-registered them with
       EuroLeague yet this season, but who has history from a prior one.

    A row matching neither gets a stable synthetic ID
    (f"sheet:{team}:{surname}:{first}") instead of a real one - genuinely
    new to this project's data, same zero-value NEW-badge placeholder
    treatment as always. Either tier's match is skipped (not guessed) if
    ambiguous - a wrong match would silently merge two different players'
    histories, worse than falling back to a placeholder.

    Returns (resolved_rows, diagnostics); each resolved row is
    {player_id, player_name, position, team}, ready for
    engine.rosters.merge_roster as-is."""
    by_team_surname: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for r in roster_rows:
        name = r["player_name"]
        if "," not in name:
            continue
        surname_part, _, first_part = name.partition(",")
        key = (r["team"], _normalize_name(surname_part))
        by_team_surname.setdefault(key, []).append((r["player_id"], _normalize_name(first_part), name))

    by_full_name: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in historical_players:
        name = r["player_name"]
        if "," not in name:
            continue
        surname_part, _, first_part = name.partition(",")
        key = (_normalize_name(surname_part), _normalize_name(first_part))
        by_full_name.setdefault(key, []).append((r["player_id"], name))

    resolved: list[dict] = []
    matched_current = 0
    matched_historical = 0
    synthetic: list[str] = []

    for row in pool_rows:
        surname_norm = _normalize_name(row["surname"])
        first_norm = _normalize_name(row["name"])
        label = f"{row['name']} {row['surname']} ({row['team']})"

        pid = player_name = None

        candidates = by_team_surname.get((row["team"], surname_norm), [])
        if len(candidates) == 1:
            pid, _, player_name = candidates[0]
        elif len(candidates) > 1:
            narrowed = [c for c in candidates if c[1] == first_norm or c[1].startswith(first_norm)]
            if len(narrowed) == 1:
                pid, _, player_name = narrowed[0]

        if pid is not None:
            matched_current += 1
        else:
            hist_candidates = by_full_name.get((surname_norm, first_norm), [])
            if len(hist_candidates) == 1:
                pid, player_name = hist_candidates[0]
                matched_historical += 1

        if pid is None:
            pid = _synthetic_id(row["team"], surname_norm, first_norm)
            player_name = f"{row['surname'].upper()}, {row['name'].upper()}"
            synthetic.append(label)

        resolved.append({
            "player_id": pid,
            "player_name": player_name,
            "position": row["position"],
            "team": row["team"],
        })

    diagnostics = {
        "total": len(pool_rows),
        "matched_current_roster": matched_current,
        "matched_historical": matched_historical,
        "synthetic": synthetic,
    }
    return resolved, diagnostics
