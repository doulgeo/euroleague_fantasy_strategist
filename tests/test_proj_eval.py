import pytest

from engine.proj_eval import (
    build_targets, calibration, decile_table, error_metrics, live_estimator, paired_bootstrap, spearman,
    mae_metric, Target,
)
from engine.projections import build_projections
from engine.rounds import round_start_dates


def test_error_metrics_hand_computed():
    m = error_metrics([(10.0, 8.0), (4.0, 7.0), (6.0, 6.0)])  # errors +2, -3, 0
    assert m["n"] == 3
    assert m["mae"] == pytest.approx(5 / 3)
    assert m["rmse"] == pytest.approx((13 / 3) ** 0.5)
    assert m["bias"] == pytest.approx(-1 / 3)


def test_calibration_and_deciles():
    slope, intercept = calibration([(1, 3), (2, 5), (3, 7)])
    assert (slope, intercept) == pytest.approx((2.0, 1.0))
    table = decile_table([(float(i), float(i) * 2) for i in range(20)])
    assert len(table) == 10 and table[0][2] == 2 and table[-1][0] == pytest.approx(18.5)


def test_spearman_with_ties():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2, 2], [1, 2, 1, 2]) == pytest.approx(0.0)


def _row(pid, team, season, rnd, code, date, pir, played=True):
    return {
        "player_id": pid, "player_name": pid, "position": "Guard", "team": team, "season_code": season,
        "round": rnd, "game_code": code, "game_date": f"{date}T20:00:00", "played": played,
        "pir_official": pir, "minutes_seconds": 1200, "team_win": True,
    }


def _fixture():
    rows = []
    code = 0
    # 20 teams-worth of filler isn't needed: classify_phase is relative to the season's max team count
    for rnd in range(1, 7):
        for pid, team in (("A", "T1"), ("B", "T2")):
            code += 1
            date = f"2025-10-{rnd * 7:02d}" if rnd * 7 < 32 else f"2025-11-{rnd * 7 - 31:02d}"
            if pid == "B":  # a teammate who always plays, so every round has both teams
                rows.append(_row("D", team, "E2025", rnd, code + 1000, date, 6.0))
            # B misses round 4 entirely (absent), has a DNP row in round 5
            if pid == "B" and rnd == 4:
                continue
            rows.append(_row(pid, team, "E2025", rnd, code, date, 10.0 + rnd if pid == "A" else 5.0,
                             played=not (pid == "B" and rnd == 5)))
    # postponed round-2 game for A, played after round 5 started: must not be a round-2 actual
    # nor visible to round 3..5 projections
    rows.append(_row("A", "T1", "E2025", 2, 99, "2025-11-20", 90.0))
    return {"E2025": rows}


def test_targets_match_direct_build_projections_and_respect_cutoff():
    rbs = _fixture()
    targets = build_targets(rbs, ["E2025"], {"live": live_estimator}, min_round=1)
    rows = rbs["E2025"]
    starts = round_start_dates(rows)
    for t in targets:
        if t.player_id != "A" or t.proj["live"] is None:
            continue
        direct = build_projections(rows, cutoff_date=starts[t.round])["A"].projected_pir
        assert t.proj["live"] == pytest.approx(direct)
        assert t.proj["live"] < 30  # the postponed 90-PIR game is never in history before round 6's start
    # postponed game is not a round-2 actual
    a2 = [t for t in targets if t.player_id == "A" and t.round == 2][0]
    assert a2.actual == 12.0
    b = {t.round: t for t in targets if t.player_id == "B"}
    assert b[4].absent and not b[4].played and b[4].actual == 0.0
    assert not b[5].absent and not b[5].played


def test_paired_bootstrap_detects_a_better_estimator():
    ts = []
    for rnd in range(1, 11):
        for i in range(10):
            ts.append(Target("S", rnd, f"p{i}", "Guard", "T", False, 5, 10.0, True, False,
                             proj={"good": 10.0, "bad": 14.0}))
    d, lo, hi = paired_bootstrap(ts, "good", "bad", mae_metric(), n_boot=200)
    assert d == pytest.approx(-4.0) and hi < 0
