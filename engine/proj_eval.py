"""
Projection-accuracy harness (docs/projection_overhaul_plan.md, Phase 0).

backtest_eval.py measures swap gain, which cancels projection quality (both
arms use the same projections). This measures projections directly: for
every regular-season round of a target season, build the projections the
live app would have had at that round's start - prior seasons plus the
current one up to the round's main-window start date (engine.rounds), the
same date cutoff the live pool uses - and pair each with what the player
actually scored in the round.

An estimator is a callable (history_rows, cutoff_date) -> {player_id: value}.
`history_rows` is already the blended timeline (prior seasons then the
current season, game codes shifted so they don't collide); build_projections
applies the date cutoff itself, other estimators must use `visible_rows`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from engine.projections import SEASON_GAME_CODE_OFFSET, build_projections
from engine.rounds import classify_phase, date_only, in_main_window, round_start_dates, round_windows

Estimator = Callable[[list[dict], str], dict[str, float]]

TOP_N = (10, 30)


@dataclass
class Target:
    season: str
    round: int
    player_id: str
    position: str | None
    team: str  # team in the round (or last team if absent)
    team_changed: bool
    games_before: int
    actual: float  # 0.0 when didn't play
    played: bool
    absent: bool  # no row at all although on the same club before and after
    proj: dict[str, float | None] = field(default_factory=dict)


def visible_rows(history: list[dict], cutoff_date: str) -> list[dict]:
    return [r for r in history if r.get("played") and (date_only(r.get("game_date")) or "9999") < cutoff_date]


def blended_history(rows_by_season: dict[str, list[dict]], season: str, cutoff_date: str, mode: str = "blended") -> list[dict]:
    """Rows visible before `cutoff_date` for projecting `season`: every earlier
    season in rows_by_season (mode "blended" - what the live app does) or none
    (mode "single"), then the season's own rows. Seasons get distinct
    game_code ranges, as engine.projections.blend_season_rows does."""
    seasons = sorted(rows_by_season)
    chosen = [s for s in seasons if s < season] if mode == "blended" else []
    out: list[dict] = []
    for i, s in enumerate(chosen + [season]):
        off = i * SEASON_GAME_CODE_OFFSET
        out += [{**r, "game_code": r["game_code"] + off} for r in rows_by_season[s]]
    return out


# --- estimators -----------------------------------------------------------

def live_estimator(history: list[dict], cutoff_date: str) -> dict[str, float]:
    return {pid: p.projected_pir for pid, p in build_projections(history, cutoff_date=cutoff_date).items()}


def _by_player(history: list[dict], cutoff_date: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in sorted(visible_rows(history, cutoff_date), key=lambda r: (r["game_date"] or "", r["game_code"])):
        out.setdefault(r["player_id"], []).append(r)
    return out


def season_mean_estimator(history: list[dict], cutoff_date: str) -> dict[str, float]:
    """Mean over the latest season the player has played in."""
    out = {}
    for pid, rs in _by_player(history, cutoff_date).items():
        latest = rs[-1]["season_code"]
        vals = [float(r["pir_official"]) for r in rs if r["season_code"] == latest]
        out[pid] = sum(vals) / len(vals)
    return out


def last5_estimator(history: list[dict], cutoff_date: str) -> dict[str, float]:
    return {
        pid: sum(float(r["pir_official"]) for r in rs[-5:]) / len(rs[-5:])
        for pid, rs in _by_player(history, cutoff_date).items()
    }


ESTIMATORS: dict[str, Estimator] = {
    "live": live_estimator,
    "season_mean": season_mean_estimator,
    "last5": last5_estimator,
}


# --- targets --------------------------------------------------------------

def build_targets(
    rows_by_season: dict[str, list[dict]],
    seasons: list[str],
    estimators: dict[str, Estimator] | None = None,
    mode: str = "blended",
    min_round: int = 1,
) -> list[Target]:
    estimators = estimators or {"live": live_estimator}
    targets: list[Target] = []
    for season in seasons:
        rows = rows_by_season[season]
        phase = classify_phase(rows)
        starts = round_start_dates(rows)
        windows = round_windows(rows)
        by_round: dict[int, list[dict]] = {}
        for r in rows:
            if r.get("round") is not None and in_main_window(r.get("game_date"), windows.get(r["round"])):
                by_round.setdefault(r["round"], []).append(r)
        for rnd in sorted(by_round):
            if rnd < min_round or phase.get(rnd) != "regular" or rnd not in starts:
                continue
            cutoff = starts[rnd]
            history = blended_history(rows_by_season, season, cutoff, mode)
            visible = _by_player(history, cutoff)
            estimates = {name: est(history, cutoff) for name, est in estimators.items()}
            round_rows: dict[str, list[dict]] = {}
            for r in by_round[rnd]:
                round_rows.setdefault(r["player_id"], []).append(r)
            after_team = _teams_after(rows, windows, rnd)
            for pid in set(round_rows) | set(visible):
                hist = visible.get(pid, [])
                rr = round_rows.get(pid, [])
                played_rows = [r for r in rr if r.get("played")]
                if rr:
                    team = rr[0]["team"]
                elif hist and pid in after_team:
                    team = hist[-1]["team"]
                else:
                    continue  # not on a roster this round (left, or unseen)
                absent = not rr
                if absent and after_team.get(pid) != team:
                    continue  # moved clubs / gone: not a clean absence
                if not hist and not played_rows:
                    continue  # unknown and didn't play: nothing to score
                actual = sum(float(r["pir_official"]) for r in played_rows)
                targets.append(Target(
                    season=season, round=rnd, player_id=pid,
                    position=(rr or hist)[0].get("position"), team=team,
                    team_changed=bool(hist) and hist[-1]["team"] != team,
                    games_before=len(hist), actual=actual, played=bool(played_rows), absent=absent,
                    proj={name: est.get(pid) for name, est in estimates.items()},
                ))
    return targets


def _teams_after(rows: list[dict], windows: dict, rnd: int) -> dict[str, str]:
    """player_id -> team of their first played row in a later round (main window only)."""
    out: dict[str, str] = {}
    for r in sorted((r for r in rows if r.get("round") is not None and r["round"] > rnd and r.get("played")),
                    key=lambda r: (r["round"], r["game_date"] or "")):
        if in_main_window(r.get("game_date"), windows.get(r["round"])):
            out.setdefault(r["player_id"], r["team"])
    return out


# --- metrics --------------------------------------------------------------

def _pairs(targets: list[Target], name: str, played_only: bool) -> list[tuple[float, float]]:
    return [
        (t.proj[name], t.actual) for t in targets
        if t.proj.get(name) is not None and (t.played or not played_only)
    ]


def error_metrics(pairs: list[tuple[float, float]]) -> dict[str, float]:
    n = len(pairs)
    if not n:
        return {"n": 0, "mae": float("nan"), "rmse": float("nan"), "bias": float("nan")}
    errs = [p - a for p, a in pairs]
    return {
        "n": n,
        "mae": sum(abs(e) for e in errs) / n,
        "rmse": (sum(e * e for e in errs) / n) ** 0.5,
        "bias": sum(errs) / n,
    }


def calibration(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """(slope, intercept) of actual regressed on projected."""
    n = len(pairs)
    mx = sum(p for p, _ in pairs) / n
    my = sum(a for _, a in pairs) / n
    var = sum((p - mx) ** 2 for p, _ in pairs)
    slope = sum((p - mx) * (a - my) for p, a in pairs) / var if var else 0.0
    return slope, my - slope * mx


def decile_table(pairs: list[tuple[float, float]]) -> list[tuple[float, float, int]]:
    """[(mean projected, mean actual, n)] over projection deciles, low to high."""
    ordered = sorted(pairs)
    n = len(ordered)
    out = []
    for d in range(10):
        chunk = ordered[d * n // 10:(d + 1) * n // 10]
        if chunk:
            out.append((sum(p for p, _ in chunk) / len(chunk), sum(a for _, a in chunk) / len(chunk), len(chunk)))
    return out


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = _rank(xs), _rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / (vx * vy) ** 0.5 if vx and vy else 0.0


def ranking_metrics(targets: list[Target], name: str) -> dict[str, float]:
    """Per-round Spearman, top-N hit rate (share of the projected top N that
    land in the actual top N) and mean actual PIR of the projected top N,
    averaged over rounds. Played targets only; hindsight ceiling = mean of
    the actual top N."""
    by_round: dict[tuple[str, int], list[Target]] = {}
    for t in targets:
        if t.played and t.proj.get(name) is not None:
            by_round.setdefault((t.season, t.round), []).append(t)
    acc: dict[str, list[float]] = {"spearman": []}
    for n in TOP_N:
        acc[f"top{n}_hit"], acc[f"top{n}_actual"], acc[f"top{n}_ceiling"] = [], [], []
    for ts in by_round.values():
        if len(ts) < 40:
            continue
        acc["spearman"].append(spearman([t.proj[name] for t in ts], [t.actual for t in ts]))
        by_proj = sorted(ts, key=lambda t: -t.proj[name])
        by_act = sorted(ts, key=lambda t: -t.actual)
        for n in TOP_N:
            top_p, top_a = by_proj[:n], by_act[:n]
            ids_a = {t.player_id for t in top_a}
            acc[f"top{n}_hit"].append(sum(t.player_id in ids_a for t in top_p) / n)
            acc[f"top{n}_actual"].append(sum(t.actual for t in top_p) / n)
            acc[f"top{n}_ceiling"].append(sum(t.actual for t in top_a) / n)
    return {k: (sum(v) / len(v) if v else float("nan")) for k, v in acc.items()}


def games_bucket(g: int) -> str:
    return "0" if g == 0 else "1-2" if g <= 2 else "3-5" if g <= 5 else "6-10" if g <= 10 else "10+"


def segment_report(targets: list[Target], name: str, played_only: bool = True) -> dict[str, dict[str, float]]:
    segs: dict[str, list[Target]] = {}
    for t in targets:
        if t.proj.get(name) is None or (played_only and not t.played):
            continue
        segs.setdefault(f"games_before {games_bucket(t.games_before)}", []).append(t)
        segs.setdefault("rounds 1-5" if t.round <= 5 else "rounds 6+", []).append(t)
        segs.setdefault(f"pos {t.position}", []).append(t)
        segs.setdefault("team-changer" if t.team_changed else "same team", []).append(t)
    return {k: error_metrics([(t.proj[name], t.actual) for t in v]) for k, v in sorted(segs.items())}


def zeroed_cost(targets: list[Target], name: str) -> dict[str, float]:
    z = [t for t in targets if t.proj.get(name) == 0.0 and t.played]
    return {"n_played_projected_0": len(z), "their_mean_actual": sum(t.actual for t in z) / len(z) if z else float("nan")}


def paired_bootstrap(
    targets: list[Target], a: str, b: str, metric: Callable[[list[Target], str], float],
    n_boot: int = 1000, seed: int = 0,
) -> tuple[float, float, float]:
    """metric(a) - metric(b), resampling whole (season, round) groups, over
    the targets both estimators cover. Returns (delta, ci_low, ci_high)."""
    common = [t for t in targets if t.proj.get(a) is not None and t.proj.get(b) is not None]
    groups: dict[tuple[str, int], list[Target]] = {}
    for t in common:
        groups.setdefault((t.season, t.round), []).append(t)
    keys = list(groups)
    delta = metric(common, a) - metric(common, b)
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        sample = [t for k in (rng.choice(keys) for _ in keys) for t in groups[k]]
        boots.append(metric(sample, a) - metric(sample, b))
    boots.sort()
    return delta, boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]


def mae_metric(played_only: bool = True) -> Callable[[list[Target], str], float]:
    return lambda ts, name: error_metrics(_pairs(ts, name, played_only))["mae"]


def top_actual_metric(n: int = 30) -> Callable[[list[Target], str], float]:
    return lambda ts, name: ranking_metrics(ts, name)[f"top{n}_actual"]


def common_targets(targets: list[Target], names: list[str]) -> list[Target]:
    return [t for t in targets if all(t.proj.get(n) is not None for n in names)]
