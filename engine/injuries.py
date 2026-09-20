"""
Scrapes basketnews.com's EuroLeague injury report (a daily-updated page
tracking player availability - Out/Doubtful/Questionable/Uncertain/
Game-time/Expected/Ready per club) into euroleague.db `injuries`, so a
player who genuinely can't play this round can be flagged rather than the
draft board/lineup builder silently treating them like anyone else.

Not an official EuroLeague/Fantasy data source - a third-party page with no
API guarantee, so this is treated as best-effort/informational: only the
"Out" status feeds back into the lineup builder's actual decision (see
EXCLUDING_STATUSES and app.py's /lineup route, which zeroes an Out player's
decision value rather than hard-excluding them - a real "Out" player simply
can't outscore anyone, so the existing brute-force selection naturally
benches/excludes them on its own, including gracefully handling more Out
players than there are exclusion slots). Every other status is shown as an
informational badge only (draft board, manager roster) - there's no
defensible way to turn "50/50 game-time decision" into a specific PIR
discount, so this project doesn't try.

Like engine.fantasy_pool (the real Fantasy sheet), the raw scraped rows are
what's persisted (engine.db.replace_injuries) - resolving each row to this
project's own player_id happens live, at read time (resolve_injury_rows),
reusing engine.fantasy_pool.resolve_pool_rows exactly as engine.draft_import
does, rather than persisting a resolved ID that could go stale as the local
DB's own identity data improves.
"""

from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from engine.fantasy_pool import resolve_pool_rows

INJURY_REPORT_URL = "https://basketnews.com/news-212393-euroleague-injury-report-updated.html"

# basketnews club display name -> this project's team code (engine.db
# `rosters.team`). Confirmed by cross-checking every 2026-27 club against
# engine.db `rosters.team_name` (itself sourced from EuroLeague's /people
# endpoint, via sync_rosters.py) - identical strings except Fenerbahce,
# where the two sources disagree on the club's current shirt sponsor
# ("Beko" here vs "Tarfin" in EuroLeague's own data).
BASKETNEWS_TEAM_TO_CODE = {
    "Anadolu Efes Istanbul": "IST",
    "Armani Olimpia Milan": "MIL",
    "Besiktas Istanbul": "BES",
    "Crvena Zvezda Meridianbet Belgrade": "RED",
    "Dubai Basketball": "DUB",
    "FC Bayern Munich": "MUN",
    "FC Barcelona": "BAR",
    "Fenerbahce Beko Istanbul": "ULK",
    "Hapoel IBI Tel Aviv": "HTA",
    "Kosner Baskonia Vitoria-Gasteiz": "BAS",
    "LDLC ASVEL Villeurbanne": "ASV",
    "Maccabi Rapyd Tel Aviv": "TEL",
    "Olympiacos Piraeus": "OLY",
    "Panathinaikos AKTOR Athens": "PAN",
    "Paris Basketball": "PRS",
    "Partizan Mozzart Bet Belgrade": "PAR",
    "Real Madrid": "MAD",
    "Valencia Basket": "PAM",
    "Virtus Bologna": "VIR",
    "Zalgiris Kaunas": "ZAL",
}

# The only status that feeds into the lineup builder's actual decision (see
# module docstring) - everything else is informational.
EXCLUDING_STATUSES = {"Out"}

# Badge color bucket per status, for the draft board / manager roster /
# lineup templates - not used for anything value-affecting, just display.
STATUS_SEVERITY = {
    "Out": "high",
    "Doubtful": "medium",
    "Questionable": "medium",
    "Uncertain": "medium",
    "Game-time": "medium",
    "Expected": "low",
    "Ready": "low",
}


def fetch_injury_report_html(url: str = INJURY_REPORT_URL) -> str:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text


def parse_injury_report(html: str) -> list[dict]:
    """Raw scraped rows, ready for engine.db.replace_injuries: team_name
    (basketnews' own display name), team (this project's code, via
    BASKETNEWS_TEAM_TO_CODE - None if a club shows up that map doesn't
    recognize yet, kept rather than dropped so it's still visible),
    position (basketnews' own abbreviation, e.g. "SG" - display only, never
    used for matching), player_name ("First Last" order - basketnews'
    convention, unlike this project's own "SURNAME, FIRST"), status,
    round_text, comment.

    The page is a single <table id="injury-reports-table">, one row per
    team (a colspan=5 header cell naming the club) followed by that club's
    player rows - confirmed live, 2026-09-20."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find(id="injury-reports-table")
    if table is None:
        return []

    rows: list[dict] = []
    current_team_name: str | None = None
    current_team_code: str | None = None

    for tr in table.find_all("tr"):
        team_cell = tr.find("td", class_="injury_reports__team-cell")
        if team_cell is not None:
            link = team_cell.find("a")
            current_team_name = (link or team_cell).get_text(strip=True)
            current_team_code = BASKETNEWS_TEAM_TO_CODE.get(current_team_name)
            continue

        cells = tr.find_all("td")
        if len(cells) != 5 or current_team_name is None:
            continue  # the header row itself, or something outside the expected shape

        position = cells[0].get_text(strip=True)
        player_link = cells[1].find("a")
        player_name = (player_link or cells[1]).get_text(" ", strip=True)
        status = cells[2].get_text(strip=True)
        round_text = cells[3].get_text(strip=True)
        comment = cells[4].get_text(strip=True)

        if not player_name or not status:
            continue

        rows.append({
            "team_name": current_team_name,
            "team": current_team_code,
            "position": position,
            "player_name": player_name,
            "status": status,
            "round_text": round_text,
            "comment": comment,
        })

    return rows


def _split_full_name(full: str) -> tuple[str, str]:
    """(first, surname) from a single "First Last" field - the last
    whitespace-separated token is the surname (works for hyphenated
    surnames, since the hyphen isn't a space), any remaining tokens are the
    first name(s). Same convention as engine.draft_import._split_full_name
    (that CSV export uses the same "First Last" order)."""
    parts = full.strip().split()
    if len(parts) <= 1:
        return "", full.strip()
    return " ".join(parts[:-1]), parts[-1]


def resolve_injury_rows(
    rows: list[dict], roster_rows: list[dict], historical_players: list[dict]
) -> tuple[list[dict], dict]:
    """Resolves each scraped injury row to this project's own player_id,
    reusing engine.fantasy_pool.resolve_pool_rows (team, surname, first-name
    matching against this season's roster, then historical fallback) -
    exactly as engine.draft_import does for the CSV import. Returns
    (resolved, diagnostics); each resolved row is {player_id, player_name,
    team, status, round_text, comment, severity} - severity is
    STATUS_SEVERITY.get(status, "low"), for template badge styling.

    A row matching neither gets a synthetic placeholder ID like the other
    two resolvers, rather than being silently dropped - it still surfaces in
    sync_injuries.py's diagnostics as unresolved."""
    pool_rows = []
    for r in rows:
        first, surname = _split_full_name(r["player_name"])
        pool_rows.append({
            "name": first,
            "surname": surname,
            "position": r.get("position") or "",
            "team": r.get("team") or "",
        })

    resolved, diagnostics = resolve_pool_rows(pool_rows, roster_rows, historical_players)

    merged = []
    for original, res in zip(rows, resolved):
        merged.append({
            "player_id": res["player_id"],
            "player_name": res["player_name"],
            "team": original.get("team"),
            "status": original["status"],
            "round_text": original["round_text"],
            "comment": original["comment"],
            "severity": STATUS_SEVERITY.get(original["status"], "low"),
        })

    return merged, diagnostics
