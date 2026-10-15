from engine.lineup import team_dates_for_round, team_dates_from_schedule
from engine.projections import build_projections
from engine.rounds import in_main_window, round_main_window, round_start_dates


def test_main_window_ignores_postponed_game():
    # E2025 round 14 shape: nine games on 12-04/05 and one postponed to March
    dates = ["2025-12-04"] * 4 + ["2025-12-05"] * 5 + ["2026-03-17"]
    assert round_main_window(dates) == ("2025-12-04", "2025-12-05", ["2026-03-17"])


def test_main_window_early_outlier():
    # E2025 round 34 shape: one game played early (03-10), rest 03-26/27
    dates = ["2026-03-10"] + ["2026-03-26"] * 5 + ["2026-03-27"] * 4
    assert round_main_window(dates) == ("2026-03-26", "2026-03-27", ["2026-03-10"])


def test_main_window_empty_and_in_window():
    assert round_main_window([]) == (None, None, [])
    w = round_main_window(["2025-12-04", "2025-12-05", "2026-03-17"] * 2)
    assert in_main_window("2025-12-05T20:00:00", w) and not in_main_window("2026-03-17T20:00:00", w)
    assert in_main_window(None, w)


def _row(pid, team, rnd, code, date, pir=10.0):
    return {
        "player_id": pid, "player_name": pid, "position": "Guard", "team": team,
        "round": rnd, "game_code": code, "game_date": f"{date}T20:00:00",
        "played": True, "pir_official": pir, "minutes_seconds": 1200, "team_win": True,
    }


def test_postponed_game_does_not_leak_into_later_round_projection():
    rows = [_row("P", "AAA", 1, 1, "2025-10-01", 5.0)]
    rows += [_row("P", "AAA", 2, 2, "2025-10-08", 5.0)]
    rows += [_row("P", "AAA", 3, 3, "2025-10-15", 5.0)]
    # round 2's postponed game, played after round 4 began
    rows += [_row("P", "AAA", 2, 4, "2025-11-05", 40.0)]
    by_round = {r: [x for x in rows if x["round"] == r] for r in (1, 2, 3)}
    assert len(by_round[2]) == 2
    starts = round_start_dates(rows + [_row("Q", "BBB", 4, 5, "2025-10-29")])
    leaky = build_projections(rows, as_of_round=4, min_games=1)["P"]
    fixed = build_projections(rows, as_of_round=4, min_games=1, cutoff_date=starts[4])["P"]
    assert leaky.projected_pir > 10 and fixed.projected_pir == 5.0


def test_team_dates_drop_postponed_games():
    rows = []
    for i, team in enumerate(["A", "B", "C", "D"]):
        rows.append(_row(f"p{i}", team, 5, i, "2025-12-04" if i < 2 else "2025-12-05"))
    rows.append(_row("p0", "A", 5, 99, "2026-03-17"))  # A's postponed second game
    rows.append(_row("pE", "E", 5, 98, "2026-03-17"))  # E only plays the postponed game
    td = team_dates_for_round(rows, 5)
    assert td == {"A": "2025-12-04", "B": "2025-12-04", "C": "2025-12-05", "D": "2025-12-05"}

    sched = [
        {"round": 5, "game_code": 1, "game_date": "2025-12-04T20:00:00", "local_team_code": "A", "road_team_code": "B"},
        {"round": 5, "game_code": 2, "game_date": "2025-12-05T20:00:00", "local_team_code": "C", "road_team_code": "D"},
        {"round": 5, "game_code": 3, "game_date": "2025-12-05T20:00:00", "local_team_code": "F", "road_team_code": "G"},
        {"round": 5, "game_code": 4, "game_date": "2026-03-17T20:00:00", "local_team_code": "E", "road_team_code": "A"},
    ]
    assert "E" not in team_dates_from_schedule(sched, 5)
    assert team_dates_from_schedule(sched, 5)["A"] == "2025-12-04"
