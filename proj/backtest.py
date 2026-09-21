"""Minutes-projection backtest harness + baselines (spec §3.9) - built and
run BEFORE any modeling, per the spec's own ordering. Milestone 2.

Folds (only 2 possible - see reports/data_audit.md §3 on sample size):
  fold 1: train=[E2023],          test=E2024
  fold 2: train=[E2023, E2024],   test=E2025

Baselines:
  B0 = last train season's mpg_active for that player.
  B1 = weighted prior across up to 3 train seasons, WITHOUT allocation -
       spec §3.3's mpg_prior formula in isolation (the real model adds a
       ridge correction + team allocation on top of this at milestone 3).
  B2 = role-class mean, where "role class" is an mpg_active tertile within
       position group, pooled over train seasons (there is no depth-chart
       data for E2023-E2025 - see reports/data_audit.md §2 - so this is a
       minutes-based proxy tier, not a roster-assigned role).

Evaluated against players with >=1 active game in the test season (a
player who's on an opening-day roster but never plays has no meaningful
"minutes when active" target - predicting *whether* they play at all is
the availability/p_active model's job, not this baseline's).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FOLDS = [
    {"train": ["E2023"], "test": "E2024"},
    {"train": ["E2023", "E2024"], "test": "E2025"},
]


def _player_history(stint_tables: dict[str, pd.DataFrame], player_id: str, train_seasons: list[str]) -> list[tuple[str, pd.Series]]:
    """(season, row) pairs for a player across train seasons, oldest first.
    A player with two stints in one season (mid-season transfer) is folded
    to one games-weighted row for that season - baselines predict a single
    next-season number, not per-stint."""
    out = []
    for season in train_seasons:
        st = stint_tables[season]
        rows = st[st["player_id"] == player_id]
        if rows.empty:
            continue
        if len(rows) == 1:
            out.append((season, rows.iloc[0]))
            continue
        games_active = rows["games_active"].sum()
        mpg = (rows["mpg_active"] * rows["games_active"]).sum() / games_active if games_active > 0 else 0.0
        out.append((season, pd.Series({
            "games_active": games_active,
            "mpg_active": mpg,
            "pos_group": rows.iloc[0]["pos_group"],
            "team": "MULTI",
        })))
    return out


def baseline_b0(history: list[tuple[str, pd.Series]]) -> float | None:
    if not history:
        return None
    return float(history[-1][1]["mpg_active"])


def baseline_b1(
    history: list[tuple[str, pd.Series]],
    season_weights: tuple[float, ...] = (0.6, 0.3, 0.1),
    shrink_games: int = 8,
) -> float | None:
    if not history:
        return None
    recent = list(reversed(history))[:3]  # most recent first
    num, den = 0.0, 0.0
    for w, (_season, row) in zip(season_weights, recent):
        games_active = float(row["games_active"])
        r = games_active / (games_active + shrink_games)
        num += w * r * float(row["mpg_active"])
        den += w * r
    return num / den if den > 0 else None


def _role_tier(mpg: float, low: float, high: float) -> str:
    if mpg >= high:
        return "starter"
    if mpg >= low:
        return "rotation"
    return "bench"


def build_role_tiers(stint_tables: dict[str, pd.DataFrame], train_seasons: list[str]) -> dict[str, tuple[float, float]]:
    pooled = pd.concat([stint_tables[s] for s in train_seasons], ignore_index=True)
    pooled = pooled[pooled["games_active"] > 0]
    cuts: dict[str, tuple[float, float]] = {}
    for pos, g in pooled.groupby("pos_group"):
        low, high = g["mpg_active"].quantile([1 / 3, 2 / 3])
        cuts[pos] = (float(low), float(high))
    return cuts


def build_role_class_means(
    stint_tables: dict[str, pd.DataFrame], train_seasons: list[str], tiers: dict[str, tuple[float, float]]
) -> dict[tuple[str, str], float]:
    pooled = pd.concat([stint_tables[s] for s in train_seasons], ignore_index=True)
    pooled = pooled[pooled["games_active"] > 0].copy()
    pooled["tier"] = [
        _role_tier(mpg, *tiers.get(pos, (0.0, 1e9)))
        for mpg, pos in zip(pooled["mpg_active"], pooled["pos_group"])
    ]
    means = pooled.groupby(["pos_group", "tier"])["mpg_active"].mean()
    return {k: float(v) for k, v in means.items()}


def baseline_b2(
    history: list[tuple[str, pd.Series]],
    pos_group: str,
    tiers: dict[str, tuple[float, float]],
    role_means: dict[tuple[str, str], float],
) -> float | None:
    if history:
        last_mpg = float(history[-1][1]["mpg_active"])
        tier = _role_tier(last_mpg, *tiers.get(pos_group, (0.0, 1e9)))
        key = (pos_group, tier)
        if key in role_means:
            return role_means[key]
    group_vals = [v for (p, _t), v in role_means.items() if p == pos_group]
    return float(np.mean(group_vals)) if group_vals else None


def _actual_role_tiers(test_st: pd.DataFrame) -> dict[str, tuple[float, float]]:
    tiers: dict[str, tuple[float, float]] = {}
    for pos, g in test_st.groupby("pos_group"):
        low, high = g["mpg_active"].quantile([1 / 3, 2 / 3])
        tiers[pos] = (float(low), float(high))
    return tiers


def evaluate_fold(
    stint_tables: dict[str, pd.DataFrame],
    train: list[str],
    test: str,
    season_weights: tuple[float, ...] = (0.6, 0.3, 0.1),
    shrink_games: int = 8,
) -> pd.DataFrame:
    test_st = stint_tables[test]
    test_st = test_st[test_st["games_active"] > 0].copy()

    tiers = build_role_tiers(stint_tables, train)
    role_means = build_role_class_means(stint_tables, train, tiers)
    actual_tiers = _actual_role_tiers(test_st)

    records = []
    for _, row in test_st.iterrows():
        history = _player_history(stint_tables, row["player_id"], train)
        last_team = history[-1][1]["team"] if history else None
        records.append({
            "player_id": row["player_id"],
            "player_name": row["player_name"],
            "team": row["team"],
            "pos_group": row["pos_group"],
            "actual_mpg": row["mpg_active"],
            "actual_games_active": row["games_active"],
            "b0": baseline_b0(history),
            "b1": baseline_b1(history, season_weights, shrink_games),
            "b2": baseline_b2(history, row["pos_group"], tiers, role_means),
            "is_newcomer": len(history) == 0,
            "is_team_changer": bool(history) and last_team not in (None, "MULTI") and last_team != row["team"],
            "is_stayer": bool(history) and last_team == row["team"],
            "actual_tier": _role_tier(row["mpg_active"], *actual_tiers.get(row["pos_group"], (0.0, 1e9))),
        })
    return pd.DataFrame(records)


def weighted_mae(df: pd.DataFrame, pred_col: str, weight_col: str = "actual_games_active") -> float | None:
    sub = df.dropna(subset=[pred_col])
    if sub.empty:
        return None
    err = (sub[pred_col] - sub["actual_mpg"]).abs()
    w = sub[weight_col].clip(lower=1)
    return float(np.average(err, weights=w))


def weighted_bias(df: pd.DataFrame, pred_col: str, weight_col: str = "actual_games_active") -> float | None:
    sub = df.dropna(subset=[pred_col])
    if sub.empty:
        return None
    err = sub[pred_col] - sub["actual_mpg"]
    w = sub[weight_col].clip(lower=1)
    return float(np.average(err, weights=w))


SEGMENTS = {
    "all": lambda df: df,
    "stayers": lambda df: df[df["is_stayer"]],
    "team_changers": lambda df: df[df["is_team_changer"]],
    "newcomers": lambda df: df[df["is_newcomer"]],
    "starters": lambda df: df[df["actual_tier"] == "starter"],
    "bench": lambda df: df[df["actual_tier"] == "bench"],
}


def summarize_fold(fold_df: pd.DataFrame, pred_cols: tuple[str, ...] = ("b0", "b1", "b2")) -> pd.DataFrame:
    rows = []
    for seg_name, seg_fn in SEGMENTS.items():
        seg = seg_fn(fold_df)
        if seg.empty:
            continue
        row = {"segment": seg_name, "n": len(seg)}
        for pred in pred_cols:
            row[f"{pred}_mae"] = weighted_mae(seg, pred)
            row[f"{pred}_bias"] = weighted_bias(seg, pred)
            row[f"{pred}_coverage"] = float(seg[pred].notna().mean())
        rows.append(row)
    return pd.DataFrame(rows)


def run_backtest(stint_tables: dict[str, pd.DataFrame], season_weights=(0.6, 0.3, 0.1), shrink_games: int = 8):
    fold_details = []
    fold_summaries = []
    for fold in FOLDS:
        fdf = evaluate_fold(stint_tables, fold["train"], fold["test"], season_weights, shrink_games)
        fdf["fold_train"] = "+".join(fold["train"])
        fdf["fold_test"] = fold["test"]
        fold_details.append(fdf)
        summary = summarize_fold(fdf)
        summary.insert(0, "fold_test", fold["test"])
        fold_summaries.append(summary)
    return pd.concat(fold_details, ignore_index=True), pd.concat(fold_summaries, ignore_index=True)
