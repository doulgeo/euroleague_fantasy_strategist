"""Date-safe game win probabilities and Phase 4 game context.

Fitting lives in win_prob_eval.py; runtime uses explicit frozen coefficients,
never a fitted estimator or a network request. Dates are calendar cutoffs:
results on the prediction date are never used as form inputs.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import exp

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
