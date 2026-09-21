"""Team allocation of minutes (spec §3.5) - the "core fix": minutes are a
zero-sum allocation of ~200 per team-game (+25/overtime), not independent
per-player forecasts - plus coach rotation concentration (§3.6).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def allocate_minutes(raw, cap, total):
    """Spec §3.5's water-filling allocator, generalized to accept either a
    scalar `cap` (identical behavior to the spec's version) or a per-player
    cap array (needed here since caps differ by position group - see
    `position_group_cap` below). The iteration itself is unchanged:
    scale every uncapped player's raw minutes together so the free group
    sums to what's left of `total`, clamp anyone who crosses their cap,
    repeat until stable.
    """
    raw = np.asarray(raw, float)
    cap = np.broadcast_to(np.asarray(cap, float), raw.shape).copy()
    if cap.sum() < total:
        raise ValueError("infeasible: cap * n_players < total")
    m = raw.copy()
    capped = np.zeros(len(m), bool)
    for _ in range(50):
        free = ~capped
        m[free] *= (total - cap[capped].sum()) / m[free].sum()
        over = free & (m > cap)
        if not over.any():
            break
        m[over] = cap[over]
        capped |= over
    return m


def position_group_cap(stint_tables: dict[str, pd.DataFrame], train_seasons: list[str], percentile: int = 99) -> dict[str, float]:
    pooled = pd.concat([stint_tables[s] for s in train_seasons], ignore_index=True)
    pooled = pooled[pooled["games_active"] > 0]
    return {pos: float(np.percentile(g["mpg_active"], percentile)) for pos, g in pooled.groupby("pos_group")}


def team_total_minutes(games: pd.DataFrame, train_seasons: list[str], team: str | None = None, min_team_games: int = 10) -> float:
    """League-wide (or team-specific, if enough sample) average team-game
    total minutes including overtime - the allocation target."""
    df = games[games["season_code"].isin(train_seasons) & games["played"]]
    totals = df.groupby(["season_code", "game_code", "team"])["minutes"].sum()
    if team is not None:
        team_totals = totals[totals.index.get_level_values("team") == team]
        if len(team_totals) >= min_team_games:
            return float(team_totals.mean())
    return float(totals.mean())


def minutes_given_available(raw_minutes, available_mask, cap, total) -> np.ndarray:
    """Reallocate `total` team minutes only among available players -
    spec §3.5's scenario allocator, `minutes_given_available(team,
    available_mask)`. Unavailable players get 0."""
    raw = np.asarray(raw_minutes, float)
    mask = np.asarray(available_mask, bool)
    cap_arr = np.broadcast_to(np.asarray(cap, float), raw.shape)
    out = np.zeros(len(raw))
    if mask.sum() == 0:
        return out
    available_raw = raw[mask]
    if available_raw.sum() <= 0:
        available_raw = np.ones(int(mask.sum()))  # no signal among the available players - split evenly, not by zero
    out[mask] = allocate_minutes(available_raw, cap_arr[mask], min(total, cap_arr[mask].sum()))
    return out


def expected_minutes_monte_carlo(raw_minutes, p_active, cap, total, draws: int = 2000, seed: int = 1) -> np.ndarray:
    """Monte Carlo average of `minutes_given_available` over availability
    sampled from p_active per player - spec §3.5's `exp_minutes`. Fixed
    seed for determinism (ground rules)."""
    rng = np.random.default_rng(seed)
    raw = np.asarray(raw_minutes, float)
    p = np.asarray(p_active, float)
    n = len(raw)
    accum = np.zeros(n)
    for _ in range(draws):
        available = rng.random(n) < p
        accum += minutes_given_available(raw, available, cap, total)
    return accum / draws


# --- Position-group sanity check (spec §3.5) ---

def position_group_historical_bands(games: pd.DataFrame, train_seasons: list[str]) -> dict[str, tuple[float, float]]:
    """p10-p90 band of team-season-average position-group minute totals
    per game, from train-season history."""
    df = games[games["season_code"].isin(train_seasons) & games["played"]]
    per_game = df.groupby(["season_code", "game_code", "team", "pos_group"])["minutes"].sum().reset_index()
    per_team_season = per_game.groupby(["season_code", "team", "pos_group"])["minutes"].mean().reset_index()
    bands = {}
    for pos, g in per_team_season.groupby("pos_group"):
        bands[pos] = (float(g["minutes"].quantile(0.10)), float(g["minutes"].quantile(0.90)))
    return bands


def apply_position_group_sanity(allocated_minutes, pos_groups, bands: dict[str, tuple[float, float]]):
    """If a position group's allocated total falls outside its historical
    p10-p90 band, shift minutes proportionally within that group toward
    the nearest band edge and log the adjustment (spec §3.5). NOTE: this
    intentionally moves minutes into/out of one position group without
    compensating elsewhere, so it can shift the team's grand total by a
    small amount when triggered - a real tension with the "sums to total"
    invariant `allocate_minutes` itself guarantees. Documented, not
    silently resolved - see reports/milestone3_minutes.md."""
    allocated = np.asarray(allocated_minutes, float).copy()
    pos_groups = np.asarray(pos_groups)
    adjustments = []
    for pos, (low, high) in bands.items():
        mask = pos_groups == pos
        if not mask.any():
            continue
        group_total = allocated[mask].sum()
        target = low if group_total < low else (high if group_total > high else None)
        if target is not None and group_total > 0:
            scale = target / group_total
            allocated[mask] *= scale
            adjustments.append({"pos_group": pos, "before": float(group_total), "after": float(target), "scale": float(scale)})
    return allocated, adjustments


# --- Coach rotation concentration (spec §3.6) ---

def compute_n_eff(minutes) -> float:
    minutes = np.asarray(minutes, float)
    total = minutes.sum()
    if total <= 0:
        return 0.0
    shares = minutes / total
    return float(1.0 / np.sum(shares ** 2))


def team_season_n_eff(games: pd.DataFrame, season: str, team: str) -> float | None:
    df = games[(games["season_code"] == season) & (games["team"] == team) & (games["played"])]
    if df.empty:
        return None
    n_effs = [compute_n_eff(g["minutes"].to_numpy()) for _, g in df.groupby("game_code")]
    return float(np.mean(n_effs)) if n_effs else None


def coach_n_eff_targets(games: pd.DataFrame, coach_by_season: dict[str, dict[str, str]], seasons: list[str]) -> tuple[dict[str, float], float]:
    """coach_name -> mean N_eff across every team-season that coach
    actually coached. Returns (per_coach, league_mean) - league_mean is
    the fallback for a coach with no history (spec §3.6)."""
    records = []
    for season in seasons:
        for team, coach in coach_by_season.get(season, {}).items():
            n_eff = team_season_n_eff(games, season, team)
            if n_eff is not None:
                records.append({"coach": coach, "n_eff": n_eff})
    if not records:
        return {}, 9.0
    df = pd.DataFrame(records)
    league_mean = float(df["n_eff"].mean())
    per_coach = df.groupby("coach")["n_eff"].mean().to_dict()
    return per_coach, league_mean


def solve_tau(raw_shares, target_n_eff: float, tau_min: float = 0.7, tau_max: float = 1.5, steps: int = 25) -> float:
    """Grid search for tau in [tau_min, tau_max] such that raising shares
    to the tau power (then renormalizing) hits `target_n_eff` as closely
    as possible - spec §3.6. Applied BEFORE capping, i.e. on raw
    (pre-`allocate_minutes`) shares."""
    raw_shares = np.asarray(raw_shares, float)
    best_tau, best_diff = 1.0, float("inf")
    for tau in np.linspace(tau_min, tau_max, steps):
        transformed = raw_shares ** tau
        transformed_sum = transformed.sum()
        if transformed_sum <= 0:
            continue
        shares = transformed / transformed_sum
        n_eff = 1.0 / np.sum(shares ** 2)
        diff = abs(n_eff - target_n_eff)
        if diff < best_diff:
            best_diff, best_tau = diff, tau
    return best_tau


def apply_tau(raw_minutes, tau: float) -> np.ndarray:
    raw_minutes = np.asarray(raw_minutes, float)
    total = raw_minutes.sum()
    if total <= 0:
        return raw_minutes
    shares = raw_minutes / total
    transformed = shares ** tau
    transformed_shares = transformed / transformed.sum()
    return transformed_shares * total
