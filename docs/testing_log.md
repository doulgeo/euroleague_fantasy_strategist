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

---

## 2026-09-11 — Elaborate backtest: phase split, confidence intervals, loss analysis, hyperparameter sensitivity

**What**: a materially deeper pass than the run above — more trials for
tighter statistics, 95% confidence intervals (not just point estimates), a
loss-case breakdown, and a rolling-window hyperparameter sensitivity check.
Extended `backtest_eval.py` to support all of this (see its own docstring).

**A methodology bug found and fixed along the way**: the initial run
(30 trials/round, same E2023-E2025 scope as above) included every round in
each season's `rounds_present`, which turned out to include **playoff
rounds** (round 35+ in E2023/E2024, round 39+ in E2025 — detected from the
data itself: EuroLeague went from 18 to 20 teams for E2025, so hardcoding a
round number would have been wrong). Playoffs have as few as 2 of 18-20
teams still active. This POC's roster sampling (`sample_roster`) draws
randomly from the *entire* league pool every round with no concept of team
elimination — a real manager's roster wouldn't contain players from
already-eliminated teams by then, but a random sample doesn't know that.
Result: playoff-round trials were mostly players who simply don't play that
round, which craters the "beat no-swap" rate for a reason that has nothing
to do with the engine's actual quality.

This was caught by an early/mid/late calendar-tercile breakdown that showed
a suspicious cliff (80% → 73% → 28% beat-rate across the season) — dug into
it before writing the finding up as if it were real, since a cliff that
sharp is more likely a broken assumption than a genuine trend:

```
round 34 (E2023): 18 teams playing, 9 games   <- still regular season
round 35 (E2023):  4 teams playing, 2 games   <- playoffs start
round 36 (E2023):  2 teams playing, 1 game    <- Final Four
```

Fixed by adding `classify_phase()` (detects regular-season vs. playoff
rounds from actual team counts per round, not a hardcoded round number) and
excluding playoff rounds from the default evaluation (`--include-playoffs`
opt-in re-includes them, heavily caveated in the output). This is the
correct **default** behavior going forward, not just a one-off filter for
this run — a manager's real fantasy roster is fixed well before playoffs
start, so evaluating with a fresh random league-wide sample per round was
never a meaningful test of playoff-round performance to begin with, and
fixing that properly (persistent roster + elimination-aware sampling) is
out of scope for this POC.

**How** (final, corrected run):
```
python backtest_eval.py --seasons E2023 E2024 E2025 --min-round 6 \
    --trials-per-round 30 --seed 1 --csv backtest_trials.csv
```
(`backtest_trials.csv` — 2730 per-trial rows, gitignored — is the raw data
behind every number below, kept locally for direct inspection if needed.)

**Result — regular season only, 91 rounds, 2730 trials:**

| Season | Rounds evaluated | Mean no-swap | Mean recommended | Mean best-possible | Beat / tie / lose | Mean gain (95% CI) | Swap-upside captured (95% CI) |
|---|---|---|---|---|---|---|---|
| E2023 | 29 | 92.88 | 103.69 | 115.46 | 82% / 14% / 4% | +10.81 ± 0.77 | 53% ± 5% |
| E2024 | 29 | 98.14 | 108.28 | 119.94 | 77% / 18% / 5% | +10.14 ± 0.73 | 54% ± 5% |
| E2025 | 33 | 92.44 | 101.12 | 114.45 | 69% / 27% / 4% | +8.69 ± 0.68 | 44% ± 4% |
| **All combined** | **91** | **94.40** | **104.22** | **116.52** | **76% / 20% / 4%** | **+9.83 ± 0.42** | **50% ± 3%** |

No structural-invariant violations across any trial (no-swap never exceeded
best-possible). This table is the number to cite going forward — it
supersedes both the earlier 4-round spot-check and the playoff-contaminated
118-round/590-trial run from the previous log entry above.

**Loss-case analysis** (the 4.2% of trials where recommended < no-swap):
mean deficit 1.56 PIR, max deficit 6.0 PIR (out of ~90-115 PIR typical round
scores — a small, bounded downside). Losses are spread across all three
seasons roughly proportionally to trial count, not concentrated in any one
season. This matches the documented expected case (a decision-time
projection turned out wrong for that specific round) rather than indicating
a logic defect — the separately-checked structural invariant
(no-swap ≤ best-possible) is what would flag an actual bug, and it held
everywhere.

**Rolling-window hyperparameter sensitivity** (`--sensitivity`, 15
trials/round, same regular-season-only scope):

| `rolling_window` | Beat no-swap | Swap-upside captured |
|---|---|---|
| 5 | 77% | 50% |
| 10 (current default) | 75% | 49% |
| 15 | 75% | 50% |
| 20 | 75% | 48% |

Stable within a couple of points across a 4x range of window sizes — the
current default of 10 isn't a fragile or cherry-picked choice; the
heuristic's usefulness doesn't hinge on this particular hyperparameter.

**Bottom line**: on the honest (regular-season-only) evaluation, the
engine-recommended lineup beats or ties a no-swap baseline in 96% of
trials, gains +9.83 PIR on average per round (95% CI ±0.42), and captures
about half of the theoretical best-possible swap upside — all confirmed
stable across three seasons and across a range of projection
hyperparameters.
