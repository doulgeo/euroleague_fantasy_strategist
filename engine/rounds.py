"""
Round windows: which dates a round "really" spans, ignoring postponed games.

EuroLeague rounds are normally 2-3 consecutive days, but a postponed game
keeps its nominal round number while being played weeks or months later
(E2025 game 134: round 14, played 2026-03-17 when the rest of round 14 was
on 2025-12-04/05). Filtering history by round number alone therefore leaks
future data into any projection for a round between the two dates, and
makes a rescheduled game look like day 1 or day 2 of the wrong round.

round_main_window finds the dense cluster of a round's game dates (within
OUTLIER_DAYS of the median game date) and calls everything else an outlier.
Everything date-sensitive (projection cutoffs, day-1/day-2 assignment,
phase classification) uses the main window instead of raw round numbers.
"""

from __future__ import annotations

from datetime import date, timedelta

OUTLIER_DAYS = 3  # a game further than this from the round's median game date is "rescheduled"


def date_only(iso_str: str | None) -> str | None:
    if not iso_str:
        return None
    return iso_str.split("T")[0]


def _ordinal(d: str) -> int:
    return date.fromisoformat(d).toordinal()


def round_main_window(game_dates: list[str]) -> tuple[str | None, str | None, list[str]]:
    """(start, end, outlier_dates) from one date per game of a round (YYYY-MM-DD;
    duplicates expected and make the median robust). start/end are None for no dates."""
    ds = sorted(d for d in (date_only(x) for x in game_dates) if d)
    if not ds:
        return None, None, []
    median = _ordinal(ds[len(ds) // 2])
    main = [d for d in ds if abs(_ordinal(d) - median) <= OUTLIER_DAYS]
    outliers = sorted({d for d in ds if abs(_ordinal(d) - median) > OUTLIER_DAYS})
    return main[0], main[-1], outliers


def _game_dates_by_round(rows: list[dict], game_key: str) -> dict[int, list[str]]:
    seen: dict[tuple, str] = {}
    for r in rows:
        rnd, d = r.get("round"), date_only(r.get("game_date"))
        if rnd is None or not d:
            continue
        seen.setdefault((rnd, r.get(game_key)), d)
    out: dict[int, list[str]] = {}
    for (rnd, _g), d in seen.items():
        out.setdefault(rnd, []).append(d)
    return out


def round_windows(rows: list[dict], game_key: str = "game_code") -> dict[int, tuple[str, str, list[str]]]:
    """round -> (start, end, outliers) from player-game rows (one season) or
    schedule rows (same `round`/`game_date`/`game_code` keys)."""
    return {rnd: round_main_window(ds) for rnd, ds in _game_dates_by_round(rows, game_key).items()}


def round_start_dates(rows: list[dict]) -> dict[int, str]:
    """round -> main-window start date. Use as the projection cutoff for that round."""
    return {rnd: w[0] for rnd, w in round_windows(rows).items() if w[0]}


def in_main_window(game_date: str | None, window: tuple[str | None, str | None, list[str]] | None) -> bool:
    """True if the date is inside the round's main window (or the window is unknown)."""
    d = date_only(game_date)
    if window is None or window[0] is None or d is None:
        return True
    return window[0] <= d <= window[1]


def next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()
