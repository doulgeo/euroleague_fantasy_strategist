"""Date-safe game win probabilities and Phase 4 game context.

Fitting lives in win_prob_eval.py; runtime uses explicit frozen coefficients,
never a fitted estimator or a network request. Dates are calendar cutoffs:
results on the prediction date are never used as form inputs.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import exp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.projections import Projection

from engine.rounds import date_only

FORM_WINDOW = 20
WIN_BONUS = 0.10


@dataclass(frozen=True)
class WinModel:
    intercept: float
    form_coef: float
    margin_coef: float = 0.0  # mean points differential / 10
    trained_through: str = ""  # exclusive availability: predictions must be later

    def home_probability(self, form_diff: float, margin_diff: float = 0.0) -> float:
        z = self.intercept + self.form_coef * form_diff + self.margin_coef * margin_diff
        return 1.0 / (1.0 + exp(-max(-35.0, min(35.0, z))))


# win_prob_eval.py, 2026-10-21: feature choice validated on E2024 (fit
# E2023); final coefficients fit E2023/E2024, never E2025/E2026 outcomes.
# E2025 Brier 0.222764 vs home-only 0.231301 and trailing-home 0.261001.
DEFAULT_WIN_MODEL = WinModel(
    intercept=0.6179601270351004,
    form_coef=0.07280283018377834,
    margin_coef=0.9529646343175686,
    trained_through="2025-05-25",
)


@dataclass(frozen=True)
class Game:
    season: str
    code: int
    round: int
    date: str
    home: str
    away: str
    home_won: bool
    margin: float


def games_from_rows(rows: list[dict]) -> list[Game]:
    """One complete game from player rows; season is part of the identity.

    Dressed DNPs have zero points and are harmless. Deduplicating players
    prevents repeated input rows from counting points twice. Missing sides,
    dates or outcomes are excluded rather than manufactured as losses.
    """
    grouped: dict[tuple, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        if not date_only(r.get("game_date")) or r.get("team_win") is None:
            continue
        if r.get("home_away") not in ("home", "away"):
            continue
        grouped[(r.get("season_code", ""), r["game_code"])][r["home_away"]][r["player_id"]] = r
    out = []
    for (season, code), sides in grouped.items():
        if not sides.get("home") or not sides.get("away"):
            continue
        home, away = list(sides["home"].values()), list(sides["away"].values())
        h, a = home[0], away[0]
        if h["team"] == a["team"] or bool(h["team_win"]) == bool(a["team_win"]):
            continue
        out.append(Game(
            season, code, h["round"], date_only(h["game_date"]), h["team"], a["team"],
            bool(h["team_win"]),
            sum(float(r.get("points") or 0) for r in home) - sum(float(r.get("points") or 0) for r in away),
        ))
    return sorted(out, key=lambda g: (g.date, g.season, g.code))


def team_form(games: list[Game], cutoff_date: str | None, window: int = FORM_WINDOW) -> dict[str, tuple[float, float]]:
    """team -> (win share, mean point differential / 10), oldest first.

    Carry up to `window` games across season boundaries for early-season
    form. Unknown teams get the explicit 0.5 / 0.0 prior at call sites.
    """
    history: dict[str, list[tuple[bool, float]]] = defaultdict(list)
    for g in sorted(games, key=lambda g: (g.date, g.season, g.code)):
        if cutoff_date is not None and g.date >= cutoff_date:
            continue
        history[g.home].append((g.home_won, g.margin / 10))
        history[g.away].append((not g.home_won, -g.margin / 10))
    return {
        team: (sum(w for w, _ in rs[-window:]) / len(rs[-window:]),
               sum(m for _, m in rs[-window:]) / len(rs[-window:]))
        for team, rs in history.items()
    }


def game_features(games: list[Game], game: Game) -> tuple[float, float]:
    form = team_form(games, game.date)
    h, a = form.get(game.home, (0.5, 0.0)), form.get(game.away, (0.5, 0.0))
    return h[0] - a[0], h[1] - a[1]


def fit_win_model(games: list[Game], training_seasons: tuple[str, ...], include_margin: bool = False) -> WinModel:
    """Fit only selected seasons, building every feature from prior dates.

    The fixed L2 regularization (C=1) avoids tuning on holdout seasons.
    sklearn is imported only by this offline fitting path.
    """
    from sklearn.linear_model import LogisticRegression

    train = [g for g in games if g.season in training_seasons]
    if not train or len({g.home_won for g in train}) < 2:
        raise ValueError("Win-model fitting needs both home wins and losses")
    features = [game_features(games, g)[:2 if include_margin else 1] for g in train]
    fitted = LogisticRegression(C=1.0, max_iter=1000, random_state=0).fit(features, [g.home_won for g in train])
    return WinModel(float(fitted.intercept_[0]), float(fitted.coef_[0][0]),
                    float(fitted.coef_[0][1]) if include_margin else 0.0,
                    max(g.date for g in train))


def schedule_from_games(games: list[Game]) -> list[dict]:
    """Historical schedule shape without outcomes (for backtest/evaluation)."""
    return [{"season_code": g.season, "game_code": g.code, "round": g.round,
             "game_date": g.date, "local_team_code": g.home, "road_team_code": g.away}
            for g in games]


def model_available(rows: list[dict], cutoff_date: str | None, model: WinModel = DEFAULT_WIN_MODEL) -> bool:
    """Frozen coefficients are never available during their training period."""
    if not model.trained_through:
        return True
    if cutoff_date is not None:
        return cutoff_date > model.trained_through
    # No explicit cutoff is a live pool over all history. Empty/older
    # datasets must not acquire a model fitted using their future results.
    return max((date_only(r.get("game_date")) or "" for r in rows), default="") > model.trained_through


def neutral_probabilities(rows: list[dict], cutoff_date: str | None = None,
                          model: WinModel = DEFAULT_WIN_MODEL) -> dict[str, float]:
    """Home/away-neutral chance against an average (50%, zero-margin) club."""
    form = team_form(games_from_rows(rows), cutoff_date)
    return {team: (model.home_probability(w - 0.5, m)
                   + 1 - model.home_probability(0.5 - w, -m)) / 2
            for team, (w, m) in form.items()}


def apply_neutral_context(pool: dict[str, Projection], rows: list[dict], cutoff_date: str | None = None,
                          model: WinModel = DEFAULT_WIN_MODEL) -> dict[str, Projection]:
    """Recompute after roster/override merge, so the *current* team wins.

    The compatibility field team_win_rate now carries the model's neutral
    probability for contextual projections. Older cutoffs retain legacy.
    """
    if not model_available(rows, cutoff_date, model):
        return dict(pool)
    return apply_team_probabilities(pool, neutral_probabilities(rows, cutoff_date, model))


def apply_team_probabilities(pool: dict[str, Projection], probabilities: dict[str, float]) -> dict[str, Projection]:
    """Reprice conditional PIR after overrides/team changes without scaling p_play."""
    from dataclasses import replace

    return {pid: replace(p, team_win_rate=probabilities.get(p.team, 0.5),
                         projected_pir_with_bonus=p.projected_pir * (1 + WIN_BONUS * probabilities.get(p.team, 0.5)))
            for pid, p in pool.items()}


# E2024-only residual fit in phase4_eval.py. Explicit for reproducibility.
HOME_PIR_ADJUSTMENT = 0.3405906328577615  # E2024 residual fit, phase4_eval.py (2026-10-21)


def round_projections(pool: dict[str, Projection], schedule_rows: list[dict], round_no: int, history: list[dict],
                      cutoff_date: str | None = None, model: WinModel = DEFAULT_WIN_MODEL,
                      home_pir: float = HOME_PIR_ADJUSTMENT, enabled: bool = True) -> dict[str, Projection]:
    """Copies with schedule-specific conditional PIR/bonus and unchanged p_play.

    Excludes postponed games using the same round window as lineup dates.
    All probabilities use the round START, even for games on later dates.
    A missing scheduled game is zero; no historical result is consulted to
    decide whether a future game is eligible. Input projections stay neutral.
    """
    from dataclasses import replace

    from engine.rounds import in_main_window, round_start_dates, round_windows

    scheduled = [r for r in schedule_rows if r.get("round") == round_no]
    round_start = round_start_dates(scheduled).get(round_no)
    # Even a caller supplying a later date cannot expose earlier results
    # from this round. An earlier cutoff can deliberately freeze more history.
    cutoffs = [d for d in (cutoff_date, round_start) if d is not None]
    cutoff = min(cutoffs) if cutoffs else None
    windows = round_windows(scheduled)
    context = {}
    use_model = enabled and model_available(history, cutoff, model)
    form = team_form(games_from_rows(history), cutoff) if use_model else {}
    seen = set()
    for r in scheduled:
        if not date_only(r.get("game_date")) or not in_main_window(r.get("game_date"), windows.get(round_no)):
            continue
        key = (r.get("season_code"), r.get("game_code"))
        if key in seen:
            continue
        seen.add(key)
        home, away = r.get("local_team_code"), r.get("road_team_code")
        if not home or not away:
            continue
        h, a = form.get(home, (0.5, 0.0)), form.get(away, (0.5, 0.0))
        probability = model.home_probability(h[0] - a[0], h[1] - a[1]) if use_model else None
        context.setdefault(home, []).append((probability, home_pir if use_model else 0.0))
        context.setdefault(away, []).append((1 - probability if use_model else None, -home_pir if use_model else 0.0))
    out = {}
    for pid, p in pool.items():
        games = context.get(p.team, [])
        if not games:
            out[pid] = replace(p, projected_pir_with_bonus=0.0)
            continue
        if not use_model:
            out[pid] = replace(p, projected_pir_with_bonus=p.projected_pir_with_bonus * len(games))
            continue
        # A genuinely unranked placeholder has no PIR estimate to adjust.
        # A modeled zero (with history) still gets the fitted location effect.
        estimated = p.games_sampled > 0 or p.projected_pir != 0.0
        conditional = sum(p.projected_pir + (adjustment if estimated else 0.0) for _, adjustment in games)
        bonus_inclusive = sum((p.projected_pir + (adjustment if estimated else 0.0)) * (1 + WIN_BONUS * prob)
                              for prob, adjustment in games)
        out[pid] = replace(p, projected_pir=conditional, projected_pir_with_bonus=bonus_inclusive,
                           team_win_rate=sum(prob for prob, _ in games) / len(games))
    return out


def round_values(pool: dict[str, Projection], schedule_rows: list[dict], round_no: int, history: list[dict], **kwargs) -> dict[str, float]:
    """Shared per-round decision values; availability and win bonus apply once."""
    return {pid: p.expected_value for pid, p in round_projections(pool, schedule_rows, round_no, history, **kwargs).items()}
