"""
Heuristic per-player fantasy projections.

Deliberately simple and transparent: recency-weighted rolling PIR, a minutes
trend, a volatility measure, and a team win-rate stand-in for the +10% win
bonus. No ML - the point is to have a readable baseline to validate before
ever considering something fancier.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from engine.rounds import date_only

MIN_GAMES_FOR_PROJECTION = 3
ROLLING_WINDOW = 10
TEAM_WIN_WINDOW = 10
WIN_BONUS_FRACTION = 0.10

# Form ticker (display only - never feeds into projected_pir or any lineup
# decision). FORM_WINDOW is the "lately" window; a player is hot/cold when
# their average over it beats/trails their projection by at least
# FORM_MIN_GAP PIR and FORM_MIN_FRACTION of the projection, whichever is
# larger, so a 3-PIR bench player doesn't flip hot on one 6-PIR game.
FORM_WINDOW = 3
FORM_MIN_GAP = 3.0
FORM_MIN_FRACTION = 0.20


def actual_fantasy_score(pir: float, team_won: bool | None) -> float:
    """Real PIR plus the official +10% team-win bonus, applied only when
    the game's real outcome is known (team_won True/False - a per-game
    fact from engine.data, not the trailing win-RATE estimate used above
    for pre-game projection). Confirmed 2026-09-22: the official scoring
    rule is "+10% of that round's fantasy score if their team won," and
    this was previously applied only inside the projection (as an expected
    value based on recent win rate, appropriate before the game's played)
    - engine.lineup.compute_round_score's *actual* scoring path ignored it
    entirely, silently understating every real/backtested round score by
    up to 10% for any player on a winning team."""
    return pir * (1 + WIN_BONUS_FRACTION) if team_won else pir


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
    # Form ticker inputs - defaulted so placeholder Projections (new
    # players, tracker snapshots) show no form rather than needing them.
    # recent_pir: plain mean PIR over the last FORM_WINDOW games.
    # projection_change: projected_pir now minus what it was before the
    # player's most recent game (0.0 when there was no earlier projection).
    recent_pir: float | None = None
    projection_change: float = 0.0
    # Kish effective sample size behind projected_pir (0.0 for placeholders)
    n_eff: float = 0.0

    @property
    def form(self) -> str | None:
        """"hot", "cold", or None - recent scoring vs the projection."""
        if self.recent_pir is None:
            return None
        gap = self.recent_pir - self.projected_pir
        threshold = max(FORM_MIN_GAP, FORM_MIN_FRACTION * self.projected_pir)
        if gap >= threshold:
            return "hot"
        if gap <= -threshold:
            return "cold"
        return None

    def __repr__(self) -> str:
        return (
            f"Projection({self.player_name!r}, {self.position}, {self.team}, "
            f"pir={self.projected_pir:.1f}, +bonus={self.projected_pir_with_bonus:.1f}, "
            f"n={self.games_sampled})"
        )


@dataclass
class SeasonPoints:
    """A player's real fantasy points so far this season - PIR plus the
    real +10% win bonus per game (actual_fantasy_score), before any slot
    multiplier (no captain 1.5x, no bench halving). Display only."""

    games: int
    total: float
    last: float  # most recent played game's points
    last_round: int | None

    @property
    def avg(self) -> float:
        return self.total / self.games if self.games else 0.0


def season_points(rows: list[dict]) -> dict[str, SeasonPoints]:
    """player_id -> SeasonPoints over the played games in `rows` (pass one
    season's rows). DNPs don't count as games."""
    played = sorted(
        (r for r in rows if r.get("played") and r.get("round") is not None),
        key=lambda r: (r["round"], r.get("game_date") or "", str(r.get("game_code"))),
    )
    out: dict[str, SeasonPoints] = {}
    for r in played:
        pts = actual_fantasy_score(float(r["pir_official"]), r.get("team_win"))
        sp = out.get(r["player_id"])
        if sp is None:
            out[r["player_id"]] = SeasonPoints(games=1, total=pts, last=pts, last_round=r["round"])
        else:
            sp.games += 1
            sp.total += pts
            sp.last = pts
            sp.last_round = r["round"]
    return out


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


SEASON_GAME_CODE_OFFSET = 100_000


def blend_season_rows(prior_rows: list[dict], current_rows: list[dict]) -> tuple[list[dict], int]:
    """(rows, as_of_round) that let build_projections treat the prior
    season and the current one as a single continuous timeline - the
    current season's rounds are renumbered to come after the prior
    season's last round, and its game codes shifted past any prior-season
    code (build_projections orders a team's games by game_code for the
    win-rate window, and both seasons' codes restart from 1).

    Found 2026-09-26: switching the live pool to the current season as soon
    as it had ANY box score left almost every player with fewer than
    MIN_GAMES_FOR_PROJECTION games, i.e. a flat 0.0, the day after round 1
    was synced. Blending instead means an early-season projection is mostly
    last season's form, shifting toward this season's game by game through
    the ROLLING_WINDOW - no cliff at the season boundary. Input rows are
    not mutated."""
    prior_max_round = max((r["round"] for r in prior_rows if r.get("round") is not None), default=0)
    shifted = [
        {**r, "round": r["round"] + prior_max_round, "game_code": r["game_code"] + SEASON_GAME_CODE_OFFSET}
        for r in current_rows
        if r.get("round") is not None
    ]
    current_max_round = max((r["round"] for r in shifted), default=prior_max_round)
    return list(prior_rows) + shifted, current_max_round + 1



@dataclass(frozen=True)
class ProjectionConfig:
    """Every knob of the PIR estimator (docs/projection_overhaul_plan.md, Phase 2).

    weighting "ramp": linear 1..n weights over the last `window_games` games
    (the original estimator). "exp": 0.5 ** (age_in_games / half_life_games),
    age counted back from the player's most recent game. Either way a game
    from a season `b` seasons older than the newest one in the history is
    scaled by season_age_weight ** b.

    shrink_k: pseudo-games of the position mean (x shrink_target_scale) mixed
    into the weighted mean: (n_eff * mean + k * target) / (n_eff + k), n_eff
    being the Kish effective sample size. 0 turns shrinkage off.
    """

    weighting: str = "ramp"
    half_life_games: float = 10.0
    window_games: int = ROLLING_WINDOW
    season_age_weight: float = 1.0
    shrink_k: float = 0.0
    shrink_target_scale: float = 1.0
    min_games: int = MIN_GAMES_FOR_PROJECTION
    exclude_prior_playoffs: bool = False


# Reproduces the pre-2026-10-15 estimator exactly (checked in tests).
LEGACY_CONFIG = ProjectionConfig()
# Tuned 2026-10-15 on E2024 only (tune_projection.py; E2023 as prior), then
# evaluated once on E2025 / E2026 (see docs/testing_log.md). Shrinkage k=1 (the
# plan's start was 2); a 0.6x shrink target and a longer window gained <0.02
# MAE and were not adopted (prefer the simpler config inside the noise).
TUNED_CONFIG = ProjectionConfig(
    weighting="exp", half_life_games=10, window_games=60, season_age_weight=0.25,
    shrink_k=1.0, shrink_target_scale=1.0, min_games=1,
)
DEFAULT_CONFIG = TUNED_CONFIG  # switched from LEGACY_CONFIG 2026-10-15 (docs/testing_log.md, Phase 2)


def _season_ranks(rows: list[dict]) -> dict[str, int]:
    """season_code -> how many seasons older than the newest one in `rows`."""
    seasons = sorted({r["season_code"] for r in rows if r.get("season_code")}, reverse=True)
    return {s: i for i, s in enumerate(seasons)}


def _weights(prows: list[dict], config: ProjectionConfig, ranks: dict[str, int]) -> list[float]:
    n = len(prows)
    if config.weighting == "ramp":
        base = [float(i + 1) for i in range(n)]
    elif config.weighting == "exp":
        base = [0.5 ** ((n - 1 - i) / config.half_life_games) for i in range(n)]
    else:
        raise ValueError(f"unknown weighting {config.weighting!r}")
    return [b * config.season_age_weight ** ranks.get(r.get("season_code"), 0) for b, r in zip(base, prows)]


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def _kish(weights: list[float]) -> float:
    return sum(weights) ** 2 / sum(w * w for w in weights)


def _weighted_var(values: list[float], weights: list[float]) -> float:
    n_eff = _kish(weights)
    if n_eff <= 1.0:
        return 0.0
    mean = _weighted_mean(values, weights)
    var = _weighted_mean([(v - mean) ** 2 for v in values], weights)
    return var * n_eff / (n_eff - 1)  # unbiased for weighted samples


@dataclass
class _PositionStats:
    mean: dict[str | None, float]
    var: dict[str | None, float]


def _position_stats(by_player: dict[str, list[dict]], config: ProjectionConfig, ranks: dict[str, int]) -> _PositionStats:
    """Per-position mean PIR of a game played (season-age weighted) and the
    average within-player variance - the shrinkage targets."""
    sums: dict[str | None, list[float]] = {}
    var_acc: dict[str | None, list[float]] = {}
    for prows in by_player.values():
        pos = prows[-1].get("position")
        for r in prows:
            w = config.season_age_weight ** ranks.get(r.get("season_code"), 0)
            acc = sums.setdefault(pos, [0.0, 0.0])
            acc[0] += w * float(r["pir_official"])
            acc[1] += w
        if len(prows) >= 3:
            vals = [float(r["pir_official"]) for r in prows[-config.window_games:]]
            var_acc.setdefault(pos, []).append(_weighted_var(vals, [1.0] * len(vals)))
    mean = {pos: a[0] / a[1] for pos, a in sums.items() if a[1] > 0}
    total = [sum(a[0] for a in sums.values()), sum(a[1] for a in sums.values())]
    mean[None] = total[0] / total[1] if total[1] else 0.0
    var = {pos: sum(v) / len(v) for pos, v in var_acc.items()}
    var[None] = sum(sum(v) for v in var_acc.values()) / max(1, sum(len(v) for v in var_acc.values()))
    return _PositionStats(mean, var)


def _estimate(prows: list[dict], config: ProjectionConfig, ranks: dict[str, int], pos: _PositionStats):
    """(projected_pir, minutes_trend, volatility, n_eff, n_games) from one
    player's date-sorted played rows (oldest first)."""
    recent = prows[-config.window_games:]
    pir = [float(r["pir_official"]) for r in recent]
    w = _weights(recent, config, ranks)
    mean = _weighted_mean(pir, w)
    n_eff = _kish(w)
    position = recent[-1].get("position")
    target = pos.mean.get(position, pos.mean[None]) * config.shrink_target_scale
    projected = (n_eff * mean + config.shrink_k * target) / (n_eff + config.shrink_k) if config.shrink_k else mean
    minutes = _weighted_mean([float(r["minutes_seconds"]) for r in recent], w)
    if config.weighting == "ramp" and config.season_age_weight == 1.0:
        vol = stdev(pir)  # the original unweighted sample SD
    else:
        var = _weighted_var(pir, w)
        pvar = pos.var.get(position, pos.var[None])
        if config.shrink_k:
            var = (n_eff * var + config.shrink_k * pvar) / (n_eff + config.shrink_k)
        vol = var ** 0.5
    return projected, minutes, vol, n_eff, len(recent)


def blend_seasons(rows_by_season: dict[str, list[dict]]) -> tuple[list[dict], int]:
    """N-season generalization of blend_season_rows: seasons in code order,
    each one's rounds renumbered after the previous one's and its game codes
    shifted into its own range. Returns (rows, as_of_round) like
    blend_season_rows. With a date cutoff (build_projections(cutoff_date=...))
    the renumbering only matters for the as_of_round compatibility filter."""
    out: list[dict] = []
    round_offset = 0
    for i, season in enumerate(sorted(rows_by_season)):
        rows = [r for r in rows_by_season[season] if r.get("round") is not None]
        out += [
            {**r, "round": r["round"] + round_offset, "game_code": r["game_code"] + i * SEASON_GAME_CODE_OFFSET}
            for r in rows
        ]
        round_offset = max([round_offset] + [r["round"] + round_offset for r in rows])
    return out, round_offset + 1


def build_projections(
    rows: list[dict],
    as_of_round: int | None = None,
    rolling_window: int | None = None,
    team_win_window: int = TEAM_WIN_WINDOW,
    min_games: int | None = None,
    cutoff_date: str | None = None,
    config: ProjectionConfig | None = None,
) -> dict[str, Projection]:
    """Build one projection per player using only games strictly before as_of_round.

    `rows` is the flat player-game table from engine.data.fetch_season (one
    season, or several via blend_season_rows). Rounds are assumed comparable
    as integers within a season.

    `cutoff_date` (YYYY-MM-DD, optional) additionally drops every game on or
    after that date. Round numbers alone leak: a postponed game keeps its
    nominal round but is played months later (see engine.rounds), so a
    round-based cutoff shows a projection for round 20 a game from round 14
    that was played after round 20 started. Callers that know the target
    round's start date (engine.rounds.round_start_dates) should pass it;
    rows with no date are dropped when it's set. `as_of_round` may then be
    None.

    `config` (ProjectionConfig, default DEFAULT_CONFIG) selects the
    estimator. The legacy `rolling_window`/`min_games` arguments, when given,
    override the config's window_games/min_games.
    """
    config = config or DEFAULT_CONFIG
    overrides = {}
    if rolling_window is not None:
        overrides["window_games"] = rolling_window
    if min_games is not None:
        overrides["min_games"] = min_games
    if overrides:
        config = replace(config, **overrides)
    min_games = config.min_games

    history_rows = [
        r
        for r in rows
        if r.get("round") is not None
        and (as_of_round is None or r["round"] < as_of_round)
        and r.get("played")
        and (cutoff_date is None or (date_only(r.get("game_date")) or "9999") < cutoff_date)
    ]
    if config.exclude_prior_playoffs and history_rows:
        history_rows = _drop_prior_playoffs(history_rows)
    ranks = _season_ranks(history_rows)

    by_player: dict[str, list[dict]] = {}
    for r in history_rows:
        by_player.setdefault(r["player_id"], []).append(r)
    for prows in by_player.values():
        prows.sort(key=lambda r: (parse_date(r["game_date"]) or datetime.min, r["game_code"]))
    pos_stats = _position_stats(by_player, config, ranks)

    team_games: dict[str, list[dict]] = {}
    for r in history_rows:
        team_games.setdefault(r["team"], []).append(r)

    # de-dup team-level rows down to one row per (team, game) for win-rate calc
    team_game_results: dict[str, list[tuple]] = {}
    for team, trows in team_games.items():
        seen_games: dict[int, tuple[str, bool | None]] = {}
        for r in trows:
            seen_games.setdefault(r["game_code"], (r.get("game_date") or "", r.get("team_win")))
        # chronological by date (game_code breaks ties): game_code alone is
        # not date-ordered when a game was postponed
        ordered = [
            (code, win) for code, (_d, win) in sorted(seen_games.items(), key=lambda kv: (kv[1][0], kv[0]))
        ]
        team_game_results[team] = ordered

    projections: dict[str, Projection] = {}

    for player_id, prows_sorted in by_player.items():
        if len(prows_sorted) < min_games:
            continue

        projected_pir, minutes_trend, volatility, n_eff, n_games = _estimate(prows_sorted, config, ranks, pos_stats)
        pir_values = [float(r["pir_official"]) for r in prows_sorted[-config.window_games:]]
        recent_pir = sum(pir_values[-FORM_WINDOW:]) / len(pir_values[-FORM_WINDOW:])
        earlier = prows_sorted[:-1]
        if len(earlier) >= min_games:
            previous_pir = _estimate(earlier, config, ranks, pos_stats)[0]
            projection_change = projected_pir - previous_pir
        else:
            projection_change = 0.0

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
            games_sampled=n_games,
            projected_pir=projected_pir,
            minutes_trend_seconds=minutes_trend,
            volatility=volatility,
            team_win_rate=team_win_rate,
            projected_pir_with_bonus=projected_pir + bonus,
            recent_pir=recent_pir,
            projection_change=projection_change,
            n_eff=n_eff,
        )

    return projections


def _drop_prior_playoffs(history_rows: list[dict]) -> list[dict]:
    """Drop playoff-round rows of every season but the newest one in the
    history (engine.rounds.classify_phase decides what a playoff round is)."""
    from engine.rounds import classify_phase

    newest = max((r["season_code"] for r in history_rows if r.get("season_code")), default=None)
    phases: dict[str, dict[int, str]] = {}
    for s in {r.get("season_code") for r in history_rows}:
        if s and s != newest:
            phases[s] = classify_phase([r for r in history_rows if r.get("season_code") == s])
    return [r for r in history_rows if phases.get(r.get("season_code"), {}).get(r["round"]) != "playoffs"]
