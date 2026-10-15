"""
p_play: the probability a player plays at all in a round he's scheduled for
(docs/projection_overhaul_plan.md, Phase 3). Projections are conditional on
playing; 21.5% of projected player-rounds actually scored 0, which made every
projection ~+1.0 PIR optimistic. In the real game a no-show scores 0 (no
auto-substitution - user-confirmed 2026-10-15), so decisions should use
expected_value = projected_pir_with_bonus * p_play.

Model: over the player's current-club stint (team games since he last joined
that club), the share of the club's most recent `window` games he played in,
shrunk toward `prior_mean` by `prior_strength` pseudo-games. Games before he
joined the club don't count against him. Players with no stint (new to the
club / league) get UNKNOWN_P_PLAY. An injury report status then scales it
(INJURY_MULTIPLIERS).
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.rounds import date_only


@dataclass(frozen=True)
class AvailabilityConfig:
    # Tuned on E2024 by Brier score, confirmed on E2025 (docs/testing_log.md): absences
    # persist (injuries), so a short, steeply recency-weighted window wins.
    window: int = 5  # club games looked back over
    prior_mean: float = 0.8  # league-wide share of club games a rostered player plays
    prior_strength: float = 1.0  # pseudo-games of prior_mean mixed in
    half_life: float | None = 2.0  # exponential recency weighting over the window; None = uniform


DEFAULT_AVAILABILITY = AvailabilityConfig()

# No history on the current club (debutant / just transferred). Not measurable
# from this DB (rostered-but-never-played players aren't in it), so a judgment
# value, low on purpose: these players are by definition unproven.
UNKNOWN_P_PLAY = 0.5

# Injury-report status -> multiplier on p_play. No historical injury data exists to
# fit these; user-approved 2026-10-15 (Questionable lowered from the proposed 0.75 to 0.6).
INJURY_MULTIPLIERS: dict[str, float] = {
    "Out": 0.0,
    "Doubtful": 0.4,
    "Questionable": 0.6,
    "Uncertain": 0.75,
    "Game-time": 0.75,
    "Expected": 0.95,
    "Ready": 0.95,
}


def injury_multiplier(status: str | None) -> float:
    return INJURY_MULTIPLIERS.get(status, 1.0) if status else 1.0


def estimate_p_play(
    history: list[dict],
    cutoff_date: str | None = None,
    current_team: dict[str, str] | None = None,
    config: AvailabilityConfig = DEFAULT_AVAILABILITY,
) -> dict[str, float]:
    """player_id -> p_play for every player with a stint on his current club.

    `history` is every player-game row (played or dressed-DNP) of the
    timeline; `cutoff_date` drops games on/after it. `current_team` (e.g. the
    roster merge) overrides each player's last-seen club."""
    rows = [
        r for r in history
        if r.get("game_date") and (cutoff_date is None or date_only(r["game_date"]) < cutoff_date)
    ]
    team_games: dict[str, dict] = {}
    by_player: dict[str, list[dict]] = {}
    for r in rows:
        team_games.setdefault(r["team"], {}).setdefault(r["game_code"], r["game_date"])
        by_player.setdefault(r["player_id"], []).append(r)
    ordered_games = {
        team: sorted(((d, code) for code, d in games.items())) for team, games in team_games.items()
    }

    out: dict[str, float] = {}
    for pid, prows in by_player.items():
        prows.sort(key=lambda r: (r["game_date"], r["game_code"]))
        team = (current_team or {}).get(pid, prows[-1]["team"])
        stint_start = None
        for r in reversed(prows):
            if r["team"] != team:
                break
            stint_start = r["game_date"]
        if stint_start is None:
            continue  # never appeared for the current club
        played = {r["game_code"] for r in prows if r["team"] == team and r.get("played")}
        games = [(d, c) for d, c in ordered_games.get(team, []) if d >= stint_start][-config.window:]
        if not games:
            continue
        n = len(games)
        if config.half_life:
            w = [0.5 ** ((n - 1 - i) / config.half_life) for i in range(n)]
        else:
            w = [1.0] * n
        num = sum(wi for wi, (_d, c) in zip(w, games) if c in played)
        out[pid] = (num + config.prior_strength * config.prior_mean) / (sum(w) + config.prior_strength)
    return out
