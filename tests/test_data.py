"""Data-layer tests: crosswalk integrity, PIR recomputation, leakage
discipline, and determinism. See reports/data_audit.md for the manual
version of these checks this project ran before writing them as pytest
tests - this file makes them regression-proof rather than one-off.
"""

from __future__ import annotations

import pytest

from engine.data import recompute_pir
from engine.db import get_connection, load_rows
from proj.backtest import FOLDS, evaluate_fold
from proj.config import load_config
from proj.data import build_all_stint_tables, load_games

CONFIG = load_config()
SEASONS = CONFIG["seasons"]["all"]
DB_PATH = CONFIG["db_path"]


@pytest.fixture(scope="module")
def games():
    return load_games(DB_PATH, SEASONS)


@pytest.fixture(scope="module")
def stint_tables(games):
    return build_all_stint_tables(games, SEASONS)


def test_crosswalk_player_id_maps_to_one_identity(games):
    """Each player_id maps to exactly one player_name across every season
    in the local DB - spec §6 test requirement. Confirmed manually in
    reports/data_audit.md §1.2 (0 mismatches, 25,286 rows); this pins it."""
    per_id = games.groupby("player_id")["player_name"].nunique()
    offenders = per_id[per_id > 1]
    assert offenders.empty, f"player_ids mapping to >1 name: {offenders.to_dict()}"


def test_pir_recomputation_matches_recorded(games):
    """recompute_pir (engine.data) reproduces pir_official within
    tolerance - reuses the already-validated function rather than
    reimplementing the formula, since this project's data layer (not
    proj/) owns PIR computation."""
    conn = get_connection(DB_PATH)
    rows = []
    for season in SEASONS:
        rows.extend(load_rows(conn, season))
    conn.close()

    mismatches = 0
    for row in rows:
        recomputed = recompute_pir(row)
        if abs(recomputed - row["pir_official"]) > 0:
            mismatches += 1
    mismatch_rate = mismatches / len(rows)
    assert mismatch_rate <= 0.01, f"PIR mismatch rate {mismatch_rate:.4f} exceeds tolerance"


def test_stint_table_never_merges_transfer_stints(games, stint_tables):
    """A player with two teams in a season must appear as two stint rows,
    never merged into one - spec §1.2's "keep player-team stints, never
    merge." Uses a real known transfer rather than a synthetic fixture."""
    e2023 = games[games["season_code"] == "E2023"]
    transfer_counts = e2023.groupby("player_id")["team"].nunique()
    transferred_players = transfer_counts[transfer_counts > 1].index
    assert len(transferred_players) > 0, "expected at least one real E2023 mid-season transfer"

    stints = stint_tables["E2023"]
    for player_id in transferred_players:
        rows = stints[stints["player_id"] == player_id]
        assert len(rows) == transfer_counts[player_id], (
            f"player {player_id} has {transfer_counts[player_id]} team stints in the raw data "
            f"but {len(rows)} rows in the stint table"
        )


def test_leakage_train_dates_precede_test_season(games):
    """For every fold, every train-season game date is strictly before
    every test-season game date - the ground rule from the spec ("Anything
    predicting season S may use only data dated before season S starts").
    Season codes don't guarantee chronological order by themselves (a
    typo or a new season code could break the assumption silently), so
    this checks real dates, not just season-code ordering."""
    max_date_by_season = games.groupby("season_code")["game_date"].max()
    min_date_by_season = games.groupby("season_code")["game_date"].min()

    for fold in FOLDS:
        train_max = max(max_date_by_season[s] for s in fold["train"])
        test_min = min_date_by_season[fold["test"]]
        assert train_max < test_min, (
            f"leakage: fold train={fold['train']} max date {train_max} "
            f"is not before test={fold['test']} min date {test_min}"
        )


def test_backtest_determinism(stint_tables):
    """Same inputs -> byte-identical baseline predictions across two runs
    (no hidden randomness in the baseline path - spec §6's determinism
    requirement, checked here since it's cheap; the real randomness
    (Monte Carlo allocation) arrives at milestone 3 with its own seeded
    test)."""
    fold = FOLDS[0]
    run1 = evaluate_fold(stint_tables, fold["train"], fold["test"])
    run2 = evaluate_fold(stint_tables, fold["train"], fold["test"])
    for col in ("b0", "b1", "b2"):
        assert run1[col].equals(run2[col])
