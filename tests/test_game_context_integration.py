"""Exercise the shared game values at the live and backtest boundaries."""
from dataclasses import replace
import random

import pytest

from engine.projections import CONTEXT_CONFIG, TUNED_CONFIG, Projection, build_projections
from engine.win_prob import round_values


def _pool():
    pool = {}
    for pos, n in (("Guard", 5), ("Forward", 5), ("Center", 3)):
        for i in range(n):
            pid = f"{pos}{i}"
            team = "A" if i % 2 == 0 else "B"
            pool[pid] = Projection(pid, pid, pos, team, 10, 10 + i, 1200, 2, 0.9, (10 + i) * 1.09, p_play=0.8)
    return pool


def _schedule():
    return [{"season_code": "E2026", "game_code": 1, "round": 1,
             "game_date": "2026-10-01", "local_team_code": "A", "road_team_code": "B"}]


def test_live_recommendation_uses_shared_round_values_and_preserves_neutral_pool(monkeypatch):
    import app

    pool = _pool()
    monkeypatch.setattr(app, "get_pool", lambda conn, before_round: (pool, set(), set(), set(), {}))
    monkeypatch.setattr(app, "_load_pool_rows", lambda conn, before_round: ([], 1, "2026-10-01"))
    monkeypatch.setattr(app, "load_schedule", lambda *args, **kwargs: _schedule())
    monkeypatch.setattr(app, "load_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(app.ownership, "manager_roster_ids", lambda conn, manager: list(pool))
    rec = app.compute_recommendation(None, 1, 1, before_round=1)
    assert rec.schedule_error is None and rec.roster_error is None
    expected = round_values(pool, _schedule(), 1, [], cutoff_date="2026-10-01")
    assert {pid: p.expected_value for pid, p in rec.pool.items()} == pytest.approx(expected)
    assert rec.recommended_total is not None
    assert pool["Guard0"].team_win_rate == 0.9


def test_backtest_trial_uses_supplied_round_values(monkeypatch):
    import backtest_eval
    from engine.roster import sample_roster

    pool = _pool()
    roster = sample_roster(pool, random.Random(1))
    values = {pid: 50.0 - i for i, pid in enumerate(pool)}
    observed = []
    real_choose = backtest_eval.choose_active_squad

    def choose(roster, dates, value):
        observed.append({p.player_id: value(p.player_id) for p in roster.players})
        return real_choose(roster, dates, value)

    monkeypatch.setattr(backtest_eval, "sample_roster", lambda pool, rng: roster)
    monkeypatch.setattr(backtest_eval, "choose_active_squad", choose)
    result = backtest_eval.run_one_trial("E2026", 1, pool, [], {"A": "2026-10-01", "B": "2026-10-01"}, 1, values)
    assert result is not None and observed == [values]


def test_context_config_changes_bonus_only_and_older_config_stays_reproducible():
    rows = []
    for i in range(3):
        for pid, team, side, won in (("A", "A", "home", True), ("B", "B", "away", False)):
            rows.append({"season_code": "E2025", "game_code": i, "round": i + 1,
                         "game_date": f"2025-10-0{i + 1}", "player_id": pid, "player_name": pid,
                         "position": "Guard", "team": team, "home_away": side, "team_win": won,
                         "points": 80 if won else 70, "pir_official": 10, "minutes_seconds": 1200, "played": True})
    tuned = build_projections(rows, cutoff_date="2025-10-04", config=TUNED_CONFIG)
    contextual = build_projections(rows, cutoff_date="2025-10-04", config=CONTEXT_CONFIG)
    assert contextual["A"].team_win_rate != tuned["A"].team_win_rate
    for pid, p in contextual.items():
        assert replace(p, team_win_rate=tuned[pid].team_win_rate,
                       projected_pir_with_bonus=tuned[pid].projected_pir_with_bonus) == tuned[pid]


@pytest.mark.parametrize("captain_pir, keeps_captain", [(13, False), (40, True)])
def test_live_swap_uses_actual_captain_score_and_renders_it(monkeypatch, captain_pir, keeps_captain):
    import re
    import app
    from engine.lineup import compute_round_score
    from engine.tracker import snapshot_rows

    pool = _pool()
    pool = {pid: replace(p, team="A" if pid in {"Guard0", "Guard1"} else "B",
                         p_play=1.0, projected_pir_with_bonus=20.0) for pid, p in pool.items()}
    pool["Forward0"] = replace(pool["Forward0"], team="C")
    schedule = [
        {"round": 1, "game_date": "2026-10-01", "local_team_code": "A", "road_team_code": "X", "played": 1},
        {"round": 1, "game_date": "2026-10-02", "local_team_code": "B", "road_team_code": "Y", "played": 0},
        {"round": 1, "game_date": "2026-10-03", "local_team_code": "C", "road_team_code": "Z", "played": 0},
    ]
    for code, game in enumerate(schedule, 1):
        game["game_code"] = code
    slots = {"Guard0": "captain", "Guard1": "starter", "Forward0": "starter",
             "Forward1": "starter", "Center0": "starter", "Guard2": "sixth",
             "Guard3": "bench", "Forward2": "bench", "Forward3": "bench", "Center1": "bench",
             "Guard4": "excluded", "Forward4": "excluded", "Center2": "excluded"}
    snapshot = {"players": snapshot_rows(list(pool.values()), slots)}
    rows = [{"round": 1, "game_date": "2026-10-01", "player_id": "Guard0",
             "team": "A", "pir_official": captain_pir, "team_win": True},
            # Future synced results must not leak into this swap window.
            {"round": 1, "game_date": "2026-10-03", "player_id": "Forward0",
             "team": "C", "pir_official": 100, "team_win": True}]
    monkeypatch.setattr(app, "DEFAULT_CONFIG", TUNED_CONFIG)
    monkeypatch.setattr(app, "get_pool", lambda *args: (pool, set(), set(), set(), {}))
    monkeypatch.setattr(app, "load_schedule", lambda *args, **kwargs: schedule)
    monkeypatch.setattr(app, "load_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(app.ownership, "manager_roster_ids", lambda *args: list(pool))
    rec = app.compute_recommendation(None, 1, 1, before_round=1, day1_snapshot=snapshot)
    assert rec.schedule_error is None and rec.day1_from_tracker
    assert rec.used_day1_actuals
    assert rec.decision_scores["Guard0"] == pytest.approx(captain_pir * 1.1)
    assert rec.decision_scores["Guard1"] == 0.0  # finished team, no box-score row: DNP
    assert rec.decision_scores["Forward0"] == 20.0
    assert rec.actual_score_ids == {"Guard0", "Guard1"}
    assert (rec.recommended.captain.player_id == "Guard0") == keeps_captain
    assert rec.recommended_total == pytest.approx(compute_round_score(rec.recommended, rec.decision_scores))

    monkeypatch.setattr(app, "get_db", lambda: None)
    monkeypatch.setattr(app.ownership, "list_managers", lambda conn: [{"manager_id": 1, "name": "Test"}])
    monkeypatch.setattr(app, "next_unplayed_round", lambda *args: 1)
    monkeypatch.setattr(app, "_my_manager_id", lambda conn: 1)
    monkeypatch.setattr(app, "_lineups_by_round", lambda *args: {1: {"my_day1": snapshot}})
    monkeypatch.setattr(app, "get_season_points", lambda conn: {})
    response = app.app.test_client().get("/lineup?manager_id=1&round=1")
    assert response.status_code == 200
    html = response.get_data(as_text=True).split("Day 2: swap plan", 1)[1]
    row = next(row for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S) if "Guard0" in row)
    assert f"{captain_pir * 1.1:.1f}" in row and "Actual" in row
    assert "Projected" not in row and "20.0" not in row
    multiplier = 1.5 if keeps_captain else (0.5 if "Guard0" in {p.player_id for p in rec.recommended.bench} else 1.0)
    assert f"{captain_pir * 1.1 * multiplier:.2f}" in row
