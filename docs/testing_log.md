# Testing log

Running record of validation performed on this project: what was tested, how,
and the result. Append new entries at the bottom, most recent last. This is
about *validation activity* (smoke tests, sanity checks, backtest runs) - not
a changelog of code changes (git history covers that).

---

## 2026-09-10 — Rate-limit sanity check before full backfill

**What**: fetched 15 game box scores from `E2020` at `min_interval=2.5`s
(down from the original 6.5s) to check for 429s before committing to a
multi-hour backfill run.

**How**: `EuroleagueClient(min_interval=2.5)`, 15 sequential
`game_stats_v2` calls.

**Result**: all 15 succeeded, no 429/5xx responses. 15 requests took 41.3s
(~2.75s/request average, consistent with the configured interval). Proceeded
with 2.5s as the backfill pacing.

---

## 2026-09-10 — `backtest_eval.py` logic smoke test

**What**: verified the new multi-round backtest aggregator (`backtest_eval.py`)
produces sane output and doesn't violate the structural invariant, before
running it at scale.

**How**: loaded already-cached `E2025` rows for rounds 8-23 (no network
calls), ran `run_one_trial` across rounds 15-20 with 3 random-seed trials per
round (18 trials total), fed through `print_summary`.

**Result**:
- 18/18 rounds evaluated, 0 skipped.
- Mean no-swap 110.64, mean recommended 122.03, mean best-possible 137.58.
- Recommended beat no-swap in 14/18 (78%), tied in 4/18 (22%), never lost.
- No invariant violations (no-swap never exceeded best-possible).
- Avg 50% of the available swap upside (best-possible minus no-swap) was
  captured by the projection-based recommendation.

Logic confirmed sound; consistent with the smaller 4-round spot-check
recorded earlier in `technical_notes.md`.

---

## 2026-09-10 — SQLite persistence layer (`engine/db.py`) round-trip test

**What**: verified upsert idempotency and that rows loaded back from SQLite
are usable by the existing engine code unchanged.

**How**: fetched cached `E2025` rows (rounds 8-23, 3810 rows) into a scratch
DB (`test_euroleague.db`, deleted after the test - not the real
`euroleague.db`), upserted once, upserted the *same* rows again, loaded rows
back out, and ran `build_projections` on the loaded rows.

**Result**:
- First upsert: 3810 rows written, `row_count == 3810`.
- Second upsert (identical rows): `row_count` still 3810 - confirms the
  `(season_code, game_code, player_id)` upsert key prevents duplicates.
- Loaded rows matched the original shape/values (spot-checked one row).
- `build_projections(loaded_rows, as_of_round=20)` produced 271 player
  projections, same as running it on the freshly-fetched rows directly - DB
  round-trip is transparent to downstream engine code.

---

## 2026-09-11 — Backfill scope changed to E2023-E2025

Originally kicked off for E2020-E2025 (six seasons); the user decided 2023
was a sufficient historical cutoff (partial E2020 data left on disk under
`raw/` from before the change - harmless, just unused now, cache is
gitignored anyway). Stopped the in-flight six-season backfill and restarted
scoped to `E2023 E2024 E2025`. Defaults in `backfill.py`, `sync_db.py`, and
`backtest_eval.py` updated to match.

## 2026-09-11 — Full backfill (E2023-E2025) and DB load

**What**: backfilled complete box-score data for E2023, E2024, E2025 (all
played games, not round-windowed), loaded into `euroleague.db` via
`sync_db.py`.

**How**: `python backfill.py --seasons E2023 E2024 E2025 --min-interval 2.5`
(ran in background, ~39 min for the ~1000 previously-uncached games), then
`python sync_db.py --seasons E2023 E2024 E2025`.

**Result**: 25,286 player-game rows total (E2023: 7883, E2024: 7863, E2025:
9540), 0 v2-source fallbacks to the legacy endpoint across all three
seasons. No rate-limit (429) issues at 2.5s/request. DB upsert counts match
the fetched row counts exactly.

## 2026-09-11 — Broader multi-season backtest (`backtest_eval.py`)

**What**: the actual broader-evaluation run the smaller E2025-only smoke
test (above) was previewing - the full engine pipeline (projections ->
sample roster/active squad -> lineup -> day-1 swap -> score) run across
every eligible cutoff round in all three backfilled seasons, 5 random
roster-sample trials per round.

**How**: `python backtest_eval.py --seasons E2023 E2024 E2025 --min-round 6
--trials-per-round 5 --seed 1`.

**Result**: 118 rounds evaluated (0 skipped), 590 trials total, no
structural-invariant violations (no-swap never exceeded best-possible in any
trial, across all three seasons).

| Season | Rounds | Mean no-swap | Mean recommended | Mean best-possible | Beat / tie / lose vs no-swap | == best-possible | Avg swap-upside captured |
|---|---|---|---|---|---|---|---|
| E2023 | 38 | 76.16 | 85.16 | 94.93 | 64% / 32% / 4% | 15% | 47% |
| E2024 | 38 | 78.26 | 86.17 | 96.61 | 62% / 34% / 4% | 13% | 46% |
| E2025 | 42 | 80.32 | 86.64 | 98.93 | 52% / 45% / 3% | 12% | 33% |
| **All combined** | **118** | **78.32** | **86.01** | **96.89** | **59% / 37% / 4%** | **13%** | **42%** |

**Reading**: consistent with the earlier 4-round spot-check in
`technical_notes.md`, now on ~30x the sample size. The heuristic-driven
day-1 swap beats or ties no-swap in 96% of trials; the 4% loss rate is the
documented expected case (decision-time projection was simply wrong that
round - see `compute_round_score`'s note in `engine/lineup.py` and
`poc_run.py`'s "Note: recommended underperformed no-swap" case), not a logic
bug. E2025 shows a lower capture rate (33% vs ~46-47%) - plausible since it's
the current in-progress season with less prior-round history available at
early cutoffs within it; not investigated further yet.

This result supersedes the 4-round spot-check as the primary evidence the
heuristic is worth building on.
