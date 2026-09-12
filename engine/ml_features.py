"""
Feature engineering for the regression-model pivot.

Converts the flat per-game box-score rows (engine.data / engine.db) into
either (a) a labeled training table - one row per (player, season, round)
with enough prior history, features from strictly-prior games, label = that
game's actual pir_official - or (b) the equivalent "live" feature vectors for
a specific projection cutoff (the direct counterpart to what
engine.projections.build_projections returns). Both share one low-level
routine (_feature_dict_from_history) so training and serving features can
never skew apart.

Leakage-safety mirrors build_projections exactly: only games strictly before
the row/cutoff being featurized feed the rolling stats. home_away/team/
position describe the game being projected, not an outcome, so reading them
for the target round is not leakage - it's the same kind of schedule
metadata a real manager already knows before a round starts.

Legacy-sourced rows (source == "legacy") lack round/game_date and are
excluded up front - they can't support these temporal features.

History restarts at each season boundary, matching build_projections's
single-season design - this also avoids a spurious ~120-day "rest days"
artifact every off-season, which would be noise, not signal.

DNP rows (played=False) are excluded as training TARGETS (a player who
didn't play isn't a "PIR-if-they-play" data point) but are also excluded
from history, same as build_projections's own history_rows filter.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from datetime import datetime

from engine.projections import (
    MIN_GAMES_FOR_PROJECTION,
    ROLLING_WINDOW,
    TEAM_WIN_WINDOW,
    parse_date,
    recency_weighted_mean,
    stdev,
)

NUMERIC_FEATURE_COLUMNS = [
    "pir_mean", "minutes_mean", "points_mean",
    "oreb_mean", "dreb_mean", "assists_mean", "steals_mean", "turnovers_mean",
    "blocks_mean", "blocks_against_mean",
    "fouls_committed_mean", "fouls_drawn_mean", "plus_minus_mean",
    "fg2a_mean", "fg2m_mean", "fg3a_mean", "fg3m_mean", "fta_mean", "ftm_mean",
    "fg3_rate",
    "starter_rate", "pir_volatility", "team_win_rate", "rest_days",
]
CATEGORICAL_FEATURE_COLUMNS = ["position", "home_away", "team"]
FEATURE_COLUMNS = NUMERIC_FEATURE_COLUMNS + CATEGORICAL_FEATURE_COLUMNS

# raw box-score field -> the rolling-mean feature name it feeds
_STAT_TO_FEATURE = {
    "pir_official": "pir_mean",
    "minutes_seconds": "minutes_mean",
    "points": "points_mean",
    "oreb": "oreb_mean",
    "dreb": "dreb_mean",
    "assists": "assists_mean",
    "steals": "steals_mean",
    "turnovers": "turnovers_mean",
    "blocks": "blocks_mean",
    "blocks_against": "blocks_against_mean",
    "fouls_committed": "fouls_committed_mean",
    "fouls_drawn": "fouls_drawn_mean",
    "plus_minus": "plus_minus_mean",
    "fg2a": "fg2a_mean",
    "fg2m": "fg2m_mean",
    "fg3a": "fg3a_mean",
    "fg3m": "fg3m_mean",
    "fta": "fta_mean",
    "ftm": "ftm_mean",
}


class _TeamWinRates:
    """Per-team trailing win rate, looked up as-of a specific game_code -
    the same trailing-window win-rate concept as build_projections, just
    generalized to per-game rather than per-round granularity (training
    rows are one per game, not one per round). Built from a superset of
    rows is safe: trailing_rate always excludes games at/after the
    requested cutoff, regardless of what the object was constructed from.
    """

    def __init__(self, rows: list[dict], window: int) -> None:
        self._window = window
        by_team: dict[str, dict[int, bool | None]] = {}
        for r in rows:
            by_team.setdefault(r["team"], {}).setdefault(r["game_code"], r.get("team_win"))

        self._game_codes: dict[str, list[int]] = {}
        self._results: dict[str, list[tuple[int, bool | None]]] = {}
        for team, games in by_team.items():
            ordered = sorted(games.items())
            self._game_codes[team] = [gc for gc, _ in ordered]
            self._results[team] = ordered

    def trailing_rate(self, team: str | None, before_game_code: float) -> float | None:
        if team is None:
            return None
        ordered = self._results.get(team)
        codes = self._game_codes.get(team)
        if not ordered:
            return None
        idx = bisect.bisect_left(codes, before_game_code)
        window = [win for _gc, win in ordered[max(0, idx - self._window):idx] if win is not None]
        return (sum(window) / len(window)) if window else None


def _feature_dict_from_history(
    history: list[dict],
    team_win_rate: float | None,
    upcoming_home_away: str | None,
    upcoming_team: str | None,
    upcoming_position: str | None,
    rest_days: float | None,
    rolling_window: int,
) -> dict:
    recent = history[-rolling_window:]
    features: dict = {}

    for stat, feature_name in _STAT_TO_FEATURE.items():
        values = [float(r[stat]) for r in recent]
        features[feature_name] = recency_weighted_mean(values) if values else float("nan")

    features["starter_rate"] = (
        recency_weighted_mean([1.0 if r["is_starter"] else 0.0 for r in recent]) if recent else float("nan")
    )

    pir_values = [float(r["pir_official"]) for r in recent]
    features["pir_volatility"] = stdev(pir_values) if len(pir_values) >= 2 else 0.0

    fg2a, fg3a = features["fg2a_mean"], features["fg3a_mean"]
    total_fga = fg2a + fg3a
    features["fg3_rate"] = fg3a / total_fga if total_fga > 1e-6 else float("nan")

    features["team_win_rate"] = team_win_rate if team_win_rate is not None else float("nan")
    features["rest_days"] = rest_days if rest_days is not None else float("nan")

    features["position"] = upcoming_position or "unknown"
    features["home_away"] = upcoming_home_away or "unknown"
    features["team"] = upcoming_team or "unknown"
    return features


def _usable_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("source") != "legacy" and r.get("round") is not None]


def _sort_key(r: dict):
    return (parse_date(r["game_date"]) or datetime.min, r["game_code"])


def build_feature_table(
    rows: list[dict],
    rolling_window: int = ROLLING_WINDOW,
    team_win_window: int = TEAM_WIN_WINDOW,
    min_games: int = MIN_GAMES_FOR_PROJECTION,
) -> list[dict]:
    """All seasons' rows (e.g. engine.db.load_rows(conn) with no season arg)
    -> one labeled row per (player, season, round) with >= min_games of
    that player's own prior history within the same season."""
    usable = _usable_rows(rows)

    by_season: dict[str, list[dict]] = {}
    for r in usable:
        by_season.setdefault(r["season_code"], []).append(r)

    table: list[dict] = []
    for season_code, season_rows in by_season.items():
        win_rates = _TeamWinRates(season_rows, team_win_window)

        by_player: dict[str, list[dict]] = {}
        for r in season_rows:
            if r.get("played"):
                by_player.setdefault(r["player_id"], []).append(r)

        for player_id, prows in by_player.items():
            prows_sorted = sorted(prows, key=_sort_key)

            for i in range(min_games, len(prows_sorted)):
                target = prows_sorted[i]
                history = prows_sorted[:i]
                team = target["team"]

                last_date = parse_date(history[-1]["game_date"])
                target_date = parse_date(target["game_date"])
                rest_days = (target_date - last_date).days if last_date and target_date else None

                feats = _feature_dict_from_history(
                    history,
                    team_win_rate=win_rates.trailing_rate(team, target["game_code"]),
                    upcoming_home_away=target.get("home_away"),
                    upcoming_team=team,
                    upcoming_position=target.get("position"),
                    rest_days=rest_days,
                    rolling_window=rolling_window,
                )
                feats.update(
                    season_code=season_code,
                    round=target["round"],
                    player_id=player_id,
                    player_name=target.get("player_name"),
                    y=float(target["pir_official"]),
                )
                table.append(feats)

    return table


def build_current_features(
    rows: list[dict],
    as_of_round: int,
    player_ids: Iterable[str],
    rolling_window: int = ROLLING_WINDOW,
    team_win_window: int = TEAM_WIN_WINDOW,
) -> dict[str, dict]:
    """Single season's rows (same contract as build_projections) -> one
    feature dict per requested player_id, as of as_of_round. Typically
    called with build_projections(...).keys() as player_ids, so the ML path
    projects the exact same eligible-player set as the heuristic.

    Reads home_away/team/position for as_of_round itself from `rows` (safe:
    schedule metadata, not an outcome) - if that round's row isn't present
    yet for a player's team (a real "hasn't been played yet" live scenario,
    as opposed to backtesting against already-recorded history), those
    fields fall back to the player's most recent known values.
    """
    usable = _usable_rows(rows)
    history_rows = [r for r in usable if r["round"] < as_of_round and r.get("played")]
    win_rates = _TeamWinRates(history_rows, team_win_window)

    by_player_history: dict[str, list[dict]] = {}
    for r in history_rows:
        by_player_history.setdefault(r["player_id"], []).append(r)

    upcoming_by_player: dict[str, dict] = {}
    for r in usable:
        if r["round"] == as_of_round:
            upcoming_by_player.setdefault(r["player_id"], r)

    out: dict[str, dict] = {}
    for player_id in player_ids:
        history = sorted(by_player_history.get(player_id, []), key=_sort_key)
        upcoming = upcoming_by_player.get(player_id)

        team = (upcoming or {}).get("team") or (history[-1]["team"] if history else None)
        team_win_rate = win_rates.trailing_rate(team, float("inf")) if team else None

        rest_days = None
        if upcoming and history:
            target_date = parse_date(upcoming.get("game_date"))
            last_date = parse_date(history[-1]["game_date"])
            if target_date and last_date:
                rest_days = (target_date - last_date).days

        out[player_id] = _feature_dict_from_history(
            history,
            team_win_rate=team_win_rate,
            upcoming_home_away=(upcoming or {}).get("home_away"),
            upcoming_team=team,
            upcoming_position=(upcoming or {}).get("position") or (history[-1]["position"] if history else None),
            rest_days=rest_days,
            rolling_window=rolling_window,
        )

    return out
