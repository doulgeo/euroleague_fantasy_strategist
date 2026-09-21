"""CLI entrypoint for proj/ - spec §6:
    python -m proj.run --season 2026 --as-of YYYY-MM-DD --config config.yaml

Milestone 2: runs the minutes-projection backtest harness (baselines only,
no modeling yet - see proj/backtest.py) and writes results under
reports/ and config.cache_dir. --season/--as-of are accepted now for
forward compatibility with the live-projection CLI shape the spec asks
for, but are not consumed until milestone 5 (proj/minutes.py and
proj/team_strength.py don't exist yet).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from proj.backtest import run_backtest
from proj.config import load_config
from proj.data import build_all_stint_tables, load_games


def main() -> None:
    parser = argparse.ArgumentParser(description="proj/ - projected minutes + team strength")
    parser.add_argument("--season", default=None, help="Target season for a live projection (unused until milestone 5)")
    parser.add_argument("--as-of", default=None, help="Cutoff date for a live projection (unused until milestone 5)")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    seasons = cfg["seasons"]["all"]
    cache_dir = Path(cfg["cache_dir"])
    reports_dir = Path(cfg["reports_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    games = load_games(cfg["db_path"], seasons)
    games.to_parquet(cache_dir / "games.parquet", index=False)

    stints = build_all_stint_tables(games, seasons)
    for season, df in stints.items():
        df.to_parquet(cache_dir / f"stints_{season}.parquet", index=False)

    weights = tuple(cfg["minutes"]["prior"]["season_weights"])
    shrink_games = cfg["minutes"]["prior"]["shrink_games"]
    details, summary = run_backtest(stints, weights, shrink_games)

    details.to_parquet(cache_dir / "backtest_minutes_baselines_details.parquet", index=False)
    summary.to_csv(reports_dir / "backtest_minutes_baselines_summary.csv", index=False)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)
    print(summary.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
