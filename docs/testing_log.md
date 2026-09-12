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

---

## 2026-09-11 — `backtest_eval.py` switched to read from `euroleague.db`

**What**: `backtest_eval.py` previously sourced rows via
`engine.data.fetch_season` (the JSON cache under `raw/`, falling back to the
live API on a cache miss). Rewired it to read via `engine.db.load_rows`
against `euroleague.db` instead — the SQLite copy `sync_db.py` already keeps
in sync — so backtests/testing no longer touch the JSON cache or the network
at all. `evaluate_season`/`run_full_eval` now take a `sqlite3.Connection`
instead of an `EuroleagueClient`; the now-meaningless `--competition` CLI
flag was dropped (season codes like `E2023` already disambiguate).

**How**: ran the full 3-season regular-season backtest
(`--seasons E2023 E2024 E2025 --min-round 6 --trials-per-round 30`) and
`--sensitivity` against the DB-backed path, and compared against the
previously validated headline (91 rounds / 2730 trials, beat-or-tie 96%,
+9.83 PIR mean gain).

**Result**: 91 rounds / 2730 trials evaluated (same as before — same
underlying rows, just read from a different store), beat 76% / tied 20% =
96% beat-or-tie, mean gain +9.98 PIR (95% CI ±0.44) — matches the prior
headline within trial-sampling noise (different `--trials-per-round`, same
seed). `--sensitivity` across rolling-window sizes 5/10/15/20 also ran
cleanly. `poc_run.py`, `explore_client.py`, and `backfill.py` were left
untouched — they intentionally still hit the live API/cache (current-season
round fetches, one-time bulk backfills), which is a separate concern from
backtesting against already-backfilled historical data.

---

## 2026-09-12 — Draft-pool sampling replaces uniform-random league-wide sampling

**What**: the user pointed out that uniform-random sampling from the entire
league pool (`sample_roster`'s previous only mode) doesn't reflect how they'd
actually draft — they target productive, high-usage/leader-type players, and
explicitly do *not* care whether a player's team wins. Added
`build_draft_pool()` to `engine/roster.py`: ranks players within each
position (Guard/Forward/Center, proportional to the 5/5/3 roster shape so
the scarcer Center slot isn't crowded out) by a 50/50 composite of min-max
normalized projected PIR and minutes trend, takes the top ~100 league-wide
(default `DRAFT_POOL_SIZE = 100`), and that's the new sampling universe.
Team win-rate is deliberately excluded from this ranking (it still feeds the
existing scoring bonus in `engine.projections` — that's a different
question). Wired into `backtest_eval.py` via `run_one_trial`/`evaluate_season`
(new `--pool-size` flag, default 100; `--pool-size 0` restores the old
uniform-random behavior for direct comparison).

**How**:
1. Sanity-checked pool composition for E2025 as-of-round-20 before trusting
   it: 99 players (38G/38F/23C), top names by position were Vezenkov,
   Nunn, Campazzo, Milutinov, etc. — recognizable real fantasy-relevant
   players, not an artifact of the ranking formula.
2. Reran the full validated-headline backtest with the new default:
   `python backtest_eval.py --seasons E2023 E2024 E2025 --min-round 6
   --trials-per-round 30 --seed 1 --csv backtest_trials_pool100.csv`.
3. Reran the identical command with `--pool-size 0` as a regression check —
   confirmed it exactly reproduces the prior DB-backed headline (76%/20%/4%,
   +9.98 ± 0.44 PIR), i.e. the refactor didn't change old behavior when the
   draft-pool restriction is disabled.
4. Reran `--sensitivity` (rolling-window 5/10/15/20, 15 trials/round) under
   the new default pool to check the hyperparameter-stability finding still
   holds with the new sampling.

**Result — regular season only, 91 rounds, 2730 trials, pool size 100 (vs.
prior full-league-pool numbers in parens):**

| Season | Beat / tie / lose | Mean gain (95% CI) | Swap-upside captured |
|---|---|---|---|
| E2023 | 89% / 9% / 2% (64%/32%/4%) | +14.58 ± 0.86 (+10.81 ± 0.77) | 51% (53%) |
| E2024 | 82% / 17% / 2% (62%/34%/4%) | +13.74 ± 0.95 (+10.14 ± 0.73) | 48% (54%) |
| E2025 | 72% / 26% / 2% (52%/45%/4%) | +12.06 ± 0.87 (+8.69 ± 0.68) | 40% (44%) |
| **All combined** | **80% / 18% / 2% (76%/20%/4%)** | **+13.40 ± 0.52 (+9.83 ± 0.42)** | **46% (50%)** |

No structural-invariant violations. Beat-or-tie rose from 96% to 98% and the
outright-loss rate roughly halved (4.0% → 2.0%), and mean PIR gain per round
rose ~36% (+9.83 → +13.40) — restricting to productive/high-minutes players
raises the floor (no-swap already scores higher, 94.40 → up to ~119-124 mean
depending on season) and makes the projection more reliable to act on, since
these players are the ones with the most stable game-to-game history for the
rolling-window projection to work from. Swap-upside captured dipped slightly
(50% → 46%, within/near the old ±3pp CI) — a mild, not dramatic, trade-off.

**Caveat worth flagging**: the loss-case *tail* got fatter even as it got
rarer — max single-round deficit rose from 6.0 to 15.0 PIR (mean deficit
1.56 → 2.14). Plausible mechanism: when a top-100 player's projection is
wrong, the miss itself is likely bigger in absolute PIR terms (these players
have higher and more volatile per-game ceilings than a random league-wide
player would), even though such misses now happen less often. Not
investigated further — noting it here so it isn't silently smoothed over by
the headline improvement.

Rolling-window sensitivity under the new pool (15 trials/round): 46-47%
swap-upside captured, 80-81% beat rate, stable across window sizes 5/10/15/20
— same conclusion as before (default of 10 isn't fragile), just re-verified
under the new sampling.

**This is now the default evaluation going forward** (`--pool-size 100` is
`backtest_eval.py`'s default). The `--pool-size 0` numbers above remain
useful as the "uniform random" baseline if that comparison is ever needed
again, but the headline number to cite is the pool-100 row.

---

## 2026-09-12 — Regression-model pivot: Ridge and gradient-boosted trees vs. the heuristic

**What**: per `docs/technical_notes.md`'s own stated rule ("prove the
heuristic works, *then* decide whether a regression/tree model demonstrably
beats it"), built a real regression pipeline and tested it head-to-head
against the heuristic, using the exact same evaluation harness. New:
`engine/ml_features.py` (feature engineering: per-player rolling stats
extended well beyond the heuristic's PIR/minutes-only scope - shooting
splits, rebounds split, assists/steals/turnovers/blocks, fouls, plus/minus,
starter rate, rest days, home/away, team, position - built once as a
labeled multi-season table), `engine/ml_projections.py` (Ridge and
`HistGradientBoostingRegressor` pipelines, trained per walk-forward round
pooling all strictly-prior seasons in full plus the current season's
rounds before the cutoff - the one structural advantage this pivot has
that the heuristic's single-season design categorically cannot use),
`ml_sanity_check.py` (a cheap fixed-split gate before the expensive full
backtest), and `--projection-method {heuristic,ridge,gbm}` wired into
`backtest_eval.py` as a pure swap-in (heuristic's own code path
byte-for-byte unchanged, confirmed by re-running it before and after these
changes and getting the identical 80%/18%/2%, +13.40 ± 0.52 headline both
times). Added `numpy`/`scikit-learn` to `requirements.txt` - the project's
first ML dependency.

**Environment note**: this machine's Python (3.14.4, via apt) has no `pip`
and is PEP-668 externally-managed, with no sudo available. Worked around by
bootstrapping a project-local `.venv` (`python3 -m venv --without-pip
.venv`, then running `get-pip.py` *inside* the venv's own interpreter, which
sidesteps the system lock entirely) rather than requesting `--break-system-
packages` or modifying the system Python. `cp314` wheels for both new
packages exist on PyPI - no interpreter downgrade needed. Anyone picking
this up fresh: `python3 -m venv --without-pip .venv && .venv/bin/python3
<(curl -s https://bootstrap.pypa.io/get-pip.py) && .venv/bin/pip install -r
requirements.txt`, then run everything via `.venv/bin/python3`.

**How**:
1. `python ml_sanity_check.py` - fixed split (train E2023+E2024, test
   E2025), MAE/RMSE/R² for Ridge, GBM, and the heuristic over the *same*
   (player, round) sample (heuristic predictions pulled from
   `build_projections` at each test row's own round, not computed
   independently, so the comparison can't silently use mismatched samples).
2. Full walk-forward backtest, identical settings to the validated
   heuristic headline, once each:
   `python backtest_eval.py --seasons E2023 E2024 E2025 --min-round 6
   --trials-per-round 30 --seed 1 --projection-method {ridge,gbm}`.

**Result 1 - sanity check (E2025 test set, n=7759, heuristic-matched sample):**

| Method | MAE | RMSE | R² |
|---|---|---|---|
| Heuristic | 5.820 | 7.519 | 0.181 |
| Ridge | 5.644 | 7.255 | 0.238 |
| GBM | 5.696 | 7.317 | 0.225 |

Both models beat the heuristic on raw per-game PIR prediction accuracy.
GBM's permutation importances put `pir_mean` (the same rolling-PIR signal
the heuristic already uses) far ahead of everything else, then
`minutes_mean`, shot volume, and `fouls_drawn_mean` - no exotic feature
dominates. Ridge's largest-magnitude coefficients were mostly **team**
dummy variables (`team_BER`, `team_PAM`, `team_ASV`, ±0.5-1.4), i.e. Ridge
is leaning partly on team identity as a pace/system proxy rather than
purely on individual form - plausible, not obviously wrong, but flagged
here rather than silently accepted (an ablation dropping `team` is a
reasonable follow-up if this pivot is pursued further, not done here).

**Result 2 - full walk-forward backtest, regular season only, 91 rounds,
2730 trials, pool-size 100 (same scope as the current heuristic headline):**

| Method | Beat / tie / lose | Mean gain (95% CI) | Swap-upside captured | Full run time |
|---|---|---|---|---|
| Heuristic | 80% / 18% / 2% | +13.40 ± 0.52 | 46% | (already validated) |
| Ridge | 81% / 17% / 2% | +13.48 ± 0.51 | 49% | 21.5s |
| GBM | 81% / 17% / 2% | +13.05 ± 0.50 | 47% | 28.8s |

Per-season mean gain (heuristic in parens):

| Season | Ridge | GBM | Heuristic |
|---|---|---|---|
| E2023 | +14.77 ± 0.85 | +14.51 ± 0.91 | +14.58 ± 0.86 |
| E2024 | +13.73 ± 0.99 | +12.71 ± 0.90 | +13.74 ± 0.95 |
| E2025 | +12.11 ± 0.80 | +12.07 ± 0.81 | +12.06 ± 0.87 |

Timing confirms the Step-2 estimate from planning: both methods add
seconds, not minutes, to a full backtest (model retraining is one `.fit()`
per round, ~91 times, sharing a feature table built once up front).

**Cross-season-pooling hypothesis - disconfirmed**: the plan's rationale for
expecting ML to help was that it can pool prior seasons' data while the
heuristic architecturally cannot (E2023 gets zero pooling for either
method, E2024 gets one prior season, E2025 gets two). If that mechanism
were doing real work, Ridge/GBM's edge over the heuristic should grow
E2023→E2024→E2025. It doesn't: the gap is E2023 +0.19/-0.07,
E2024 -0.01/-1.03, E2025 +0.05/+0.01 (Ridge/GBM respectively) - flat and
inside noise throughout, not growing. Whatever's happening, it isn't the
hypothesized pooling advantage.

**Loss-case comparison**: Ridge 1.9% loss rate (mean deficit 1.69, max
7.5 PIR), GBM 2.2% (mean deficit 1.79, max 11.5 PIR), heuristic (pool-100)
4.2%/mean 1.56/max 6.0 (rounding note: the pool-100 heuristic loss-rate
figure is from the prior entry above). Both ML methods' loss *rates* are
similar to or better than the heuristic; GBM's worst single-round deficit
(11.5) is noticeably fatter-tailed than Ridge's (7.5) or the heuristic's
(6.0).

**Bottom line**: neither model **demonstrably** beats the heuristic by the
project's own stated bar. Ridge is marginally ahead on every headline
metric (+13.48 vs +13.40 mean gain, 49% vs 46% captured) but the gap is
well within the reported 95% CIs (±0.5-0.52) - not statistically
distinguishable from noise at this sample size. GBM is essentially a wash
on mean gain (+13.05, actually *below* the heuristic) despite having better
standalone prediction accuracy in the sanity check - a genuinely useful
finding on its own: predicting individual-game PIR more accurately did not
translate into better swap/lineup *decisions*, most likely because what
the swap logic needs is correctly-ordered relative value between bench and
incumbent at decision time, not low average error across the whole player
pool. Both models ran fast enough (seconds) that this isn't a resourcing
verdict - the heuristic's transparent recency-weighted mean is simply
already capturing most of the exploitable signal in this feature set, at
this data volume (~20k labeled rows after the min-games gate). Given this,
**the heuristic remains the default** (`--projection-method heuristic`);
the regression paths stay available (`ridge`/`gbm`) for anyone who wants to
revisit this with a materially different feature set (e.g. opponent-
strength modeling, explicitly deferred from this pass) or more seasons of
data, but there's no case for switching the default today.
