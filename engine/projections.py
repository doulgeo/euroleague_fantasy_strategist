"""
Heuristic per-player fantasy projections.

Deliberately simple and transparent: recency-weighted rolling PIR, a minutes
trend, a volatility measure, and a team win-rate stand-in for the +10% win
bonus. No ML - the point is to have a readable baseline to validate before
ever considering something fancier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MIN_GAMES_FOR_PROJECTION = 3
ROLLING_WINDOW = 10
TEAM_WIN_WINDOW = 10
WIN_BONUS_FRACTION = 0.10


@dataclass
class Projection:
    player_id: str
    player_name: str
    position: str | None
    team: str
    games_sampled: int
    projected_pir: float
    minutes_trend_seconds: float
    volatility: float
    team_win_rate: float | None
    projected_pir_with_bonus: float

    def __repr__(self) -> str:
        return (
            f"Projection({self.player_name!r}, {self.position}, {self.team}, "
            f"pir={self.projected_pir:.1f}, +bonus={self.projected_pir_with_bonus:.1f}, "
            f"n={self.games_sampled})"
        )


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def recency_weighted_mean(values: list[float]) -> float:
    """Most-recent-last list of values -> weighted mean, more weight on recent.

    Linear ramp weights (1, 2, 3, ... n) rather than exponential decay - simple,
    transparent, and enough to reflect recent form without over-reacting to a
    single game.
    """
    n = len(values)
    weights = list(range(1, n + 1))
    total_weight = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total_weight


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance**0.5


def build_projections(
    rows: list[dict],
    as_of_round: int,
    rolling_window: int = ROLLING_WINDOW,
    team_win_window: int = TEAM_WIN_WINDOW,
    min_games: int = MIN_GAMES_FOR_PROJECTION,
) -> dict[str, Projection]:
    """Build one projection per player using only games strictly before as_of_round.

    `rows` is the flat player-game table from engine.data.fetch_season for a
    single season. Rounds are assumed comparable as integers within that season
    (true for the v2 source, which is what this is built against).
    """
    history_rows = [
        r for r in rows if r.get("round") is not None and r["round"] < as_of_round and r.get("played")
    ]

    by_player: dict[str, list[dict]] = {}
    for r in history_rows:
        by_player.setdefault(r["player_id"], []).append(r)

    team_games: dict[str, list[dict]] = {}
    for r in history_rows:
        team_games.setdefault(r["team"], []).append(r)

    # de-dup team-level rows down to one row per (team, game) for win-rate calc
    team_game_results: dict[str, list[tuple]] = {}
    for team, trows in team_games.items():
        seen_games: dict[int, bool | None] = {}
        for r in trows:
            seen_games.setdefault(r["game_code"], r.get("team_win"))
        # sort by game_code as a proxy for chronological order within a team
        ordered = sorted(seen_games.items(), key=lambda kv: kv[0])
        team_game_results[team] = ordered

    projections: dict[str, Projection] = {}

    for player_id, prows in by_player.items():
        prows_sorted = sorted(prows, key=lambda r: (parse_date(r["game_date"]) or datetime.min, r["game_code"]))

        if len(prows_sorted) < min_games:
            continue

        recent = prows_sorted[-rolling_window:]
        pir_values = [float(r["pir_official"]) for r in recent]
        minutes_values = [float(r["minutes_seconds"]) for r in recent]

        projected_pir = recency_weighted_mean(pir_values)
        minutes_trend = recency_weighted_mean(minutes_values)
        volatility = stdev(pir_values)

        team = prows_sorted[-1]["team"]
        team_results = team_game_results.get(team, [])[-team_win_window:]
        known_results = [win for _game, win in team_results if win is not None]
        team_win_rate = (sum(known_results) / len(known_results)) if known_results else None

        bonus = (team_win_rate or 0.0) * WIN_BONUS_FRACTION * projected_pir

        projections[player_id] = Projection(
            player_id=player_id,
            player_name=prows_sorted[-1]["player_name"],
            position=prows_sorted[-1].get("position"),
            team=team,
            games_sampled=len(recent),
            projected_pir=projected_pir,
            minutes_trend_seconds=minutes_trend,
            volatility=volatility,
            team_win_rate=team_win_rate,
            projected_pir_with_bonus=projected_pir + bonus,
        )

    return projections
