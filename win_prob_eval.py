"""Reproduce Phase 4 training and holdout win-probability evaluation (local DB only)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from engine.db import DB_PATH, load_rows
from engine.win_prob import fit_win_model, game_features, games_from_rows, team_form


def main() -> None:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = load_rows(conn)
    games = games_from_rows(rows)
    # Feature selection uses E2024 only, fitted on E2023. E2025 is untouched
    # until both the feature set and final E2023/E2024 coefficients are fixed.
    candidates = {name: fit_win_model(games, ("E2023",), margin)
                  for name, margin in (("form", False), ("form_margin", True))}
    validation = [g for g in games if g.season == "E2024"]

    def model_brier(model, gs):
        return sum((model.home_probability(*game_features(games, g)) - g.home_won) ** 2 for g in gs) / len(gs)

    validation_scores = {name: model_brier(model, validation) for name, model in candidates.items()}
    print("E2024 feature selection (fit E2023):", json.dumps(validation_scores))
    # Require a nontrivial validation gain before adding another feature.
    include_margin = validation_scores["form_margin"] < validation_scores["form"] - 0.002
    model = fit_win_model(games, ("E2023", "E2024"), include_margin)
    print("Frozen model:", json.dumps(asdict(model), sort_keys=True))
    train_games = [g for g in games if g.season in ("E2023", "E2024")]
    home_rate = sum(g.home_won for g in train_games) / len(train_games)
    for season in ("E2023", "E2024", "E2025", "E2026"):
        gs = [g for g in games if g.season == season]
        if not gs:
            continue
        trailing = [team_form(games, g.date, window=10) for g in gs]
        trailing_brier = sum(
            (f.get(g.home, (0.5, 0))[0] - g.home_won) ** 2
            + (f.get(g.away, (0.5, 0))[0] - (not g.home_won)) ** 2
            for f, g in zip(trailing, gs)
        ) / (2 * len(gs))
        result = {
            "season": season, "games": len(gs),
            "trailing_team_brier": trailing_brier,
            "flat_brier": 0.25,
            "home_only_brier": sum((home_rate - g.home_won) ** 2 for g in gs) / len(gs),
            "model_brier": model_brier(model, gs) if season >= "E2025" else None,
        }
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
