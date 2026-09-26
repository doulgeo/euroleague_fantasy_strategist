"""
Points tracker: the user's REAL per-round lineups (as opposed to /lineup's
suggestion), scored against real box scores.

Per round the user saves up to two lineups - the day-1 lock and the final,
post-swap one - each a slot assignment for all 13 roster players (see
SLOTS). Alongside each, app.py snapshots what /lineup suggested at that
moment (tool_day1 / tool_final in engine.db's tracked_lineups). Scores are
never stored: score_round recomputes them from player_game_stats every
time, so a late box-score sync or a scoring-rule fix flows through to the
whole history automatically.

"Best possible" here is exact, not the backtest's heuristic ceiling: any
valid final lineup is achievable in the real game simply by locking it in
on day 1 and never swapping, so the hindsight optimum is just the best
static lineup given everyone's real scores - brute-forced over all 286
exclusion combos x 3 formations (see best_possible_lineup).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from engine.lineup import (
    BENCH_SCORE_MULTIPLIER,
    CAPTAIN_SCORE_MULTIPLIER,
    VALID_FORMATIONS,
    Lineup,
    availability_label,
    compute_round_score,
)
from engine.projections import Projection, actual_fantasy_score
from engine.roster import EXCLUDED_COUNT

SLOTS = ("captain", "starter", "sixth", "bench", "excluded")
SLOT_LABELS = {
    "captain": "Captain",
    "starter": "Starter",
    "sixth": "6th man",
    "bench": "Bench",
    "excluded": "Excluded",
}
SLOT_MULTIPLIERS = {
    "captain": CAPTAIN_SCORE_MULTIPLIER,
    "starter": 1.0,
    "sixth": 1.0,
    "bench": BENCH_SCORE_MULTIPLIER,
    "excluded": 0.0,
}
POSITIONS = ("Guard", "Forward", "Center")


def formation_str(formation: tuple[int, int, int]) -> str:
    return "-".join(str(n) for n in formation)


def lineup_from_slots(players: list[Projection], slots: dict[str, str]) -> tuple[Lineup, list[Projection]]:
    """(lineup, excluded) from a slot assignment for every roster player.
    Raises ValueError with a user-readable message on any rule violation:
    exactly 1 captain + 4 other starters forming a valid formation, 1 sixth
    man, 4 bench, EXCLUDED_COUNT excluded (see docs/game_rules.md)."""
    missing = [p.player_name for p in players if slots.get(p.player_id) not in SLOTS]
    if missing:
        raise ValueError(f"No slot chosen for: {', '.join(missing)}")

    by_slot: dict[str, list[Projection]] = {s: [] for s in SLOTS}
    for p in players:
        by_slot[slots[p.player_id]].append(p)

    expected = {"captain": 1, "starter": 4, "sixth": 1, "bench": 4, "excluded": EXCLUDED_COUNT}
    wrong = [
        f"{SLOT_LABELS[s]}: {len(by_slot[s])} (need {n})" for s, n in expected.items() if len(by_slot[s]) != n
    ]
    if wrong:
        raise ValueError("Wrong slot counts - " + "; ".join(wrong))

    starters = by_slot["captain"] + by_slot["starter"]
    shape = tuple(sum(1 for p in starters if p.position == pos) for pos in POSITIONS)
    if shape not in VALID_FORMATIONS:
        valid = ", ".join(formation_str(f) for f in VALID_FORMATIONS)
        raise ValueError(f"Starters (incl. captain) form {formation_str(shape)} (G-F-C) - must be one of {valid}")

    lineup = Lineup(
        starters=starters,
        sixth_man=by_slot["sixth"][0],
        bench=by_slot["bench"],
        captain=by_slot["captain"][0],
    )
    return lineup, by_slot["excluded"]


def lineup_to_slots(lineup: Lineup, excluded: list[Projection]) -> dict[str, str]:
    slots = {p.player_id: "starter" for p in lineup.starters}
    slots[lineup.captain.player_id] = "captain"
    slots[lineup.sixth_man.player_id] = "sixth"
    slots.update({p.player_id: "bench" for p in lineup.bench})
    slots.update({p.player_id: "excluded" for p in excluded})
    return slots


def lineup_formation(lineup: Lineup) -> tuple[int, int, int]:
    return tuple(sum(1 for p in lineup.starters if p.position == pos) for pos in POSITIONS)


def snapshot_rows(players: list[Projection], slots: dict[str, str]) -> list[dict]:
    """Rows for engine.db.save_tracked_lineup - one per roster player."""
    return [
        {
            "player_id": p.player_id,
            "player_name": p.player_name,
            "team": p.team,
            "position": p.position,
            "slot": slots[p.player_id],
            "projected_value": p.projected_pir_with_bonus,
        }
        for p in players
    ]


def players_from_snapshot(snapshot: dict) -> tuple[list[Projection], dict[str, str]]:
    """(players, slots) back out of a load_tracked_lineups entry. Players are
    minimal Projection objects carrying the projected value saved at the
    time, so the lineup can be rebuilt even if a player has since left the
    roster (or the league)."""
    players = [
        Projection(
            player_id=r["player_id"],
            player_name=r["player_name"],
            position=r["position"],
            team=r["team"],
            games_sampled=0,
            projected_pir=r["projected_value"] or 0.0,
            minutes_trend_seconds=0.0,
            volatility=0.0,
            team_win_rate=None,
            projected_pir_with_bonus=r["projected_value"] or 0.0,
        )
        for r in snapshot["players"]
    ]
    return players, {r["player_id"]: r["slot"] for r in snapshot["players"]}


def final_rule_warnings(
    day1_slots: dict[str, str],
    final_slots: dict[str, str],
    players: list[Projection],
    team_dates: dict[str, str],
) -> list[str]:
    """Soft checks of a final lineup against its day-1 lock (see
    docs/game_rules.md, "The turn-based substitution mechanic"). Warnings,
    not errors: the real game already enforces these, so a mismatch most
    likely means a typo in what was entered here - but it's the user's
    record to keep."""
    warnings = []
    names = {p.player_id: p.player_name for p in players}

    day1_excluded = {pid for pid, s in day1_slots.items() if s == "excluded"}
    final_excluded = {pid for pid, s in final_slots.items() if s == "excluded"}
    if day1_excluded != final_excluded:
        warnings.append("The excluded players changed after day 1 - the real game locks exclusions for the round.")

    day1_players = {p.player_id for p in players if availability_label(p, team_dates) == "day1"}
    full = {"captain", "starter", "sixth"}
    promoted = [
        names.get(pid, pid)
        for pid in day1_players
        if day1_slots.get(pid) == "bench" and final_slots.get(pid) in full
    ]
    if promoted:
        warnings.append(
            f"Promoted from the bench after already playing on day 1: {', '.join(sorted(promoted))} "
            f"- the real game only lets you move a player up before their game starts."
        )

    day1_captain = next((pid for pid, s in day1_slots.items() if s == "captain"), None)
    final_captain = next((pid for pid, s in final_slots.items() if s == "captain"), None)
    if final_captain != day1_captain and final_captain in day1_players:
        warnings.append(
            f"Captaincy moved to {names.get(final_captain, final_captain)}, who already played on day 1 "
            f"- a new captain must be a starter who hasn't played yet."
        )
    return warnings


def actual_scores(round_rows: list[dict]) -> dict[str, float]:
    """player_id -> real fantasy score for a round (PIR + the real +10%
    team-win bonus, see engine.projections.actual_fantasy_score). Summed
    per player in case a round ever holds two games for one team."""
    scores: dict[str, float] = {}
    for r in round_rows:
        scores[r["player_id"]] = scores.get(r["player_id"], 0.0) + actual_fantasy_score(
            r["pir_official"], r.get("team_win")
        )
    return scores


def best_possible_lineup(
    players: list[Projection], actual: dict[str, float]
) -> tuple[Lineup, list[Projection]] | None:
    """The exact hindsight-optimal (lineup, excluded) for this roster given
    everyone's real score - see the module docstring for why a static
    lineup is enough. For a fixed exclusion + formation, taking the top
    players per position as starters, the best leftover as sixth man and
    the top starter as captain is optimal; brute force covers the rest.
    None if no exclusion leaves a fillable formation."""
    value = lambda p: actual.get(p.player_id, 0.0)  # noqa: E731
    best: tuple[float, Lineup, list[Projection]] | None = None

    for excluded in itertools.combinations(players, EXCLUDED_COUNT):
        excluded_ids = {p.player_id for p in excluded}
        active = [p for p in players if p.player_id not in excluded_ids]
        for formation in VALID_FORMATIONS:
            starters: list[Projection] = []
            for pos, count in zip(POSITIONS, formation):
                at_pos = sorted((p for p in active if p.position == pos), key=value, reverse=True)
                if len(at_pos) < count:
                    break
                starters.extend(at_pos[:count])
            else:
                starter_ids = {p.player_id for p in starters}
                rest = sorted((p for p in active if p.player_id not in starter_ids), key=value, reverse=True)
                lineup = Lineup(
                    starters=starters,
                    sixth_man=rest[0],
                    bench=rest[1:],
                    captain=max(starters, key=value),
                )
                score = compute_round_score(lineup, actual)
                if best is None or score > best[0]:
                    best = (score, lineup, list(excluded))

    return (best[1], best[2]) if best else None


@dataclass
class PlayerLine:
    player_id: str
    player_name: str
    team: str
    position: str | None
    slot: str
    played: bool
    fantasy: float  # real fantasy score (PIR + win bonus), before the slot multiplier
    points: float  # what it contributed to the round total


@dataclass
class RoundScore:
    round_no: int
    complete: bool  # every game of the round has been played + synced
    has_box_scores: bool  # at least one game of the round is synced
    my_final: float | None = None
    my_no_swap: float | None = None  # the day-1 lock, never swapped
    tool: float | None = None
    best: float | None = None
    official: float | None = None
    breakdown: list[PlayerLine] = field(default_factory=list)
    best_slots: dict[str, str] = field(default_factory=dict)

    @property
    def official_diff(self) -> float | None:
        if self.official is None or self.my_final is None:
            return None
        return self.official - self.my_final

    @property
    def swap_gain(self) -> float | None:
        if self.my_final is None or self.my_no_swap is None:
            return None
        return self.my_final - self.my_no_swap

    @property
    def pct_of_best(self) -> float | None:
        if self.my_final is None or not self.best:
            return None
        return self.my_final / self.best


def _score_snapshot(snapshot: dict | None, actual: dict[str, float]) -> tuple[float | None, list[PlayerLine]]:
    if snapshot is None:
        return None, []
    players, slots = players_from_snapshot(snapshot)
    try:
        lineup, _excluded = lineup_from_slots(players, slots)
    except ValueError:
        return None, []
    lines = [
        PlayerLine(
            player_id=p.player_id,
            player_name=p.player_name,
            team=p.team,
            position=p.position,
            slot=slots[p.player_id],
            played=p.player_id in actual,
            fantasy=actual.get(p.player_id, 0.0),
            points=actual.get(p.player_id, 0.0) * SLOT_MULTIPLIERS[slots[p.player_id]],
        )
        for p in players
    ]
    lines.sort(key=lambda line: (SLOTS.index(line.slot), -line.points))
    return compute_round_score(lineup, actual), lines


def score_round(
    round_no: int,
    lineups: dict[str, dict],
    actual: dict[str, float],
    complete: bool,
    official: float | None = None,
) -> RoundScore:
    """Score one round. `lineups` maps kind (my_day1 / my_final / tool_final)
    to a load_tracked_lineups entry; `actual` comes from actual_scores. The
    final lineup falls back to the day-1 one if only day 1 was saved (i.e.
    no swap was made)."""
    result = RoundScore(round_no=round_no, complete=complete, has_box_scores=bool(actual), official=official)
    if not actual:
        return result

    final_snapshot = lineups.get("my_final") or lineups.get("my_day1")
    result.my_final, result.breakdown = _score_snapshot(final_snapshot, actual)
    result.my_no_swap, _ = _score_snapshot(lineups.get("my_day1"), actual)
    result.tool, _ = _score_snapshot(lineups.get("tool_final") or lineups.get("tool_day1"), actual)

    if final_snapshot is not None:
        players, _slots = players_from_snapshot(final_snapshot)
        best = best_possible_lineup(players, actual)
        if best is not None:
            result.best = compute_round_score(best[0], actual)
            result.best_slots = lineup_to_slots(*best)
    return result
