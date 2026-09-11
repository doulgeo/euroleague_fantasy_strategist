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

## Pending / not yet run

- Full `backtest_eval.py` across the three backfilled seasons (E2023-E2025)
  once `backfill.py` finishes - this is the actual broader-evaluation result
  the smoke test above was only a preview of.
