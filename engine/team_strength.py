"""
Per-team defensive form: how much PIR a team's opponents tend to score
against them, computed the same leakage-safe way as engine.projections
(only games strictly before a cutoff round) and gated by a minimum-games
threshold before being trusted.

This is deliberately a standalone module, not yet wired into
engine.projections.build_projections. Per the project's established
discipline (see the ML-vs-heuristic comparison in docs/technical_notes.md -
nothing gets added just because it's plausible, it has to demonstrably
improve on a backtest first), team_strength_backtest.py checks whether
knowing this actually improves next-game PIR prediction before this touches
the real projection pipeline at all.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_GAMES_FOR_TEAM_STRENGTH = 5
TEAM_STRENGTH_WINDOW = 10


@dataclass
class TeamStrength:
    team: str
    games_sampled: int
    pir_allowed: float
    win_rate: float | None

    def __repr__(self) -> str:
        return (
            f"TeamStrength({self.team!r}, pir_allowed={self.pir_allowed:.1f}, "
            f"win_rate={self.win_rate}, n={self.games_sampled})"
        )


def build_team_strength(
    rows: list[dict],
    as_of_round: int,
    window: int = TEAM_STRENGTH_WINDOW,
    min_games: int = MIN_GAMES_FOR_TEAM_STRENGTH,
) -> dict[str, TeamStrength]:
    """One entry per team, using only games strictly before as_of_round.

    `rows` is the flat player-game table for a single season (same shape as
    engine.data.fetch_season / engine.db.load_rows).

    `pir_allowed` is a **per-game team total**: for each of a team's last
    `window` games, sum the PIR every opposing player put up against them
    that game, then take the flat mean across those games (matching
    engine.projections' team_win_rate convention - a flat mean over the
    window, not recency-weighted; team-level stats are already smoothed by
    being a per-game aggregate, unlike a single player's noisy game-to-game
    PIR).
    """
    history_rows = [
        r for r in rows if r.get("round") is not None and r["round"] < as_of_round and r.get("played")
    ]

    # PIR allowed: sum opposing players' PIR per (defending_team, game_code).
    allowed_totals: dict[str, dict[int, float]] = {}
    # Win rate: one result per (team, game_code), same dedup as engine.projections.
    team_results: dict[str, dict[int, bool | None]] = {}

    for r in history_rows:
        defending_team = r.get("opponent")
        if defending_team:
            game_totals = allowed_totals.setdefault(defending_team, {})
            game_totals[r["game_code"]] = game_totals.get(r["game_code"], 0.0) + float(r["pir_official"])

        team = r["team"]
        team_results.setdefault(team, {}).setdefault(r["game_code"], r.get("team_win"))

    all_teams = set(allowed_totals) | set(team_results)
    strengths: dict[str, TeamStrength] = {}

    for team in all_teams:
        games = sorted(allowed_totals.get(team, {}).items(), key=lambda kv: kv[0])  # game_code as chronology proxy
        recent = games[-window:]
        if len(recent) < min_games:
            continue

        pir_allowed = sum(v for _game, v in recent) / len(recent)

        results = sorted(team_results.get(team, {}).items(), key=lambda kv: kv[0])[-window:]
        known = [win for _game, win in results if win is not None]
        win_rate = (sum(known) / len(known)) if known else None

        strengths[team] = TeamStrength(
            team=team,
            games_sampled=len(recent),
            pir_allowed=pir_allowed,
            win_rate=win_rate,
        )

    return strengths


def league_average_pir_allowed(strengths: dict[str, TeamStrength]) -> float:
    values = [s.pir_allowed for s in strengths.values()]
    return sum(values) / len(values) if values else 0.0
