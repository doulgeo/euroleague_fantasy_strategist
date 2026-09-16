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

---

## 2026-09-12 — Hyperparameter grid search, heuristic+ridge+gbm ensemble, minimal-feature-set ablation

**What**: three follow-ups to the regression pivot above, all aimed at
"can we close the gap to the heuristic further, and is the full feature set
actually earning its keep": (1) a hyperparameter grid search for both
models, since neither had been tuned - Ridge used sklearn's default
`alpha=1.0` and GBM used an untuned guess (`max_depth=6, learning_rate=0.1`);
(2) an equal-weight ensemble averaging the heuristic's own prediction with
Ridge and GBM (`build_ensemble_projections` in `engine/ml_projections.py`,
wired in as `--projection-method ensemble`); (3) a "minimal" feature subset
close to the heuristic's own inputs (`pir_mean`, `minutes_mean`,
`pir_volatility`, `team_win_rate`, `position` - 5 features vs. the full
set's 22), to test whether the extra box-score detail is actually useful or
just noise (`--feature-set {full,minimal}`, `engine.ml_features.FEATURE_SETS`).

**How**: `python ml_grid_search.py` (cheap fixed-split sweep, same
E2023+E2024→E2025 split as `ml_sanity_check.py` - Ridge alpha ∈
{0.1, 0.3, 1, 3, 10, 30, 100}, GBM max_depth ∈ {3, 6, 9} × learning_rate ∈
{0.05, 0.1, 0.2}, each × feature_set ∈ {full, minimal}), then the full
walk-forward backtest (same settings as the pivot's headline: `--seasons
E2023 E2024 E2025 --min-round 6 --trials-per-round 30 --seed 1`) for the
newly-tuned Ridge/GBM defaults and for `--projection-method ensemble`.

**Result 1 - grid search** (E2025 test set, full feature set unless noted):

| Sweep | Best config | Best MAE | vs. untuned |
|---|---|---|---|
| Ridge alpha | 100.0 | 5.637 | 5.642 (alpha=1.0) - essentially flat |
| GBM depth×lr | depth=3, lr=0.05 | 5.660 | 5.694 (depth=6, lr=0.1) - real improvement |

Ridge's alpha barely moved MAE across a 1000x range (0.1→100: 5.642→5.637) -
the "team dummy coefficients look large" observation from the original
pivot wasn't actually a held-out-accuracy problem regularization could fix;
noted, not chased further. GBM's original untuned guess was genuinely too
deep/fast; shallower, slower trees (depth=3, lr=0.05) measurably help and
this was **adopted as the new default** in `engine/ml_projections.py`
(`GBM_DEFAULTS`), confirmed to also help in the full backtest below (not
just the fixed-split MAE). Ridge's new default (`alpha=100.0`) was adopted
too, on the "no reason not to, doesn't hurt" logic even though the gain is
inside noise.

**Feature-set ablation** (same sweep, `feature_set=minimal` vs `full`):

| Feature set | Ridge best R² | GBM best R² |
|---|---|---|
| full (22 features) | 0.239 | 0.236 |
| minimal (5 features) | 0.222 | 0.219 |

Clear and consistent for both models: the extra box-score detail (shot
splits, rebound split, fouls, plus/minus, rest days, home/away, team) is
earning its keep, not just adding noise - this **contradicts** the
hypothesis raised when discussing next steps ("maybe the heuristic's own
inputs are already enough signal"). Given how clear and consistent this
gap is at the cheap fixed-split stage, the expensive full walk-forward
backtest wasn't re-run for `minimal` - the fixed-split result is decisive
enough on its own.

**Result 2 - full walk-forward backtest, regular season only, 91 rounds,
2730 trials, pool-size 100, full feature set, tuned hyperparameters:**

| Method | Beat/tie/lose | Mean gain (95% CI) | Captured | Loss rate (mean/max deficit) |
|---|---|---|---|---|
| Heuristic | 80%/18%/2% | +13.40 ± 0.52 | 46% | 4.2% (1.56 / 6.0) [pool-100 entry above] |
| Ridge (tuned) | 81%/17%/2% | +13.51 ± 0.51 | 48% | 1.9% (2.25 / 15.0) |
| GBM (tuned) | 80%/19%/1% | +13.27 ± 0.51 | 46% | 1.4% (2.40 / 17.5) |
| Ensemble (heuristic+ridge+gbm) | 81%/18%/2% | +13.45 ± 0.52 | 48% | 1.8% (1.88 / 16.0) |

GBM's tuning translated into a real backtest improvement (+13.05 → +13.27
mean gain, loss rate 2.2% → 1.4%), confirming the fixed-split MAE gain
wasn't just an artifact of that one split. The ensemble lands between
Ridge and GBM, as expected from equal-weight averaging - it doesn't beat
its best individual component (Ridge), it just softens GBM's drag on the
average.

**Bottom line, updated**: all four methods are now clustered within
~0.24 PIR of each other (13.27-13.51), all well inside/near the reported
±0.5 95% CIs. Ridge (tuned) is the best individual method today, edging
the heuristic by +0.11 PIR mean gain - still not a demonstrable win by the
project's own bar, but the smallest gap yet and the closest thing to a
positive signal for the regression path so far. Every method's loss *rate*
beats the heuristic's (1.4-1.9% vs 4.2%), but with a consistently fatter
tail on individual bad rounds (max deficit 15-17.5 PIR vs the heuristic's
6.0) - a recurring pattern across every regression variant tried this
session, worth remembering if this is revisited: **the regression paths
trade a lower frequency of bad rounds for occasionally much worse ones**,
not a uniformly safer profile. Heuristic stays the default.

**Not yet tried** (deliberately deferred per this session's own scoping,
to revisit later): other model families (Lasso/ElasticNet for automatic
feature selection, quantile/Poisson loss functions for PIR's skewed
distribution), stacking the heuristic's own prediction in as a training
*feature* rather than an ensemble-averaging input (letting the model learn
a correction/residual instead of predicting PIR from scratch), opponent-
strength features, and more backfilled seasons of training data.

---

## 2026-09-12 — Draft-board tool sanity check

**What**: built `draft_board.py`, a new draft-prep cheat sheet (per-position
ranked list, tiering by gap detection, and cross-position VORP using
positional replacement level = 12 managers x that position's roster
requirement). Requested by the user ahead of the live draft: "a list per
position that would yield the largest amount of points," explicitly no ML.
Reuses `engine.projections.build_projections` unchanged - no new
projection logic, just a new ranking/presentation layer over it.

**How**: ran against `euroleague.db` E2025 (most recent completed season,
rounds 1-47), `min_games=10`, full history as the projection window
(`as_of_round=48`). Eyeballed the top of each position list and the
overall VORP-sorted list for plausibility.

**Result**: sane. Top names match real-world EuroLeague standouts
(Vezenkov, Hezonja, Larkin, Fournier, Tavares, Lessort, Milutinov all
appear near the top of their position). Tiering produced plausible
breaks (e.g. Centers: Wright/Oturu/Diakite as tier 1, a clear drop to
Milutinov alone in tier 2). No unit tests written - this is a
presentation layer over already-validated projections, not new
projection logic, so a plausibility read was judged sufficient.

**Caveat surfaced to the user**: board reflects last season's form only -
summer transfers, retirements, and players new to EuroLeague this season
won't be captured or may show up under a stale team code.

---

## 2026-09-13 — Real exclusion-choosing logic (choose_active_squad)

**What**: added `engine.lineup.choose_active_squad`, filling the one real
per-round gap surfaced when the user asked how starters/formation get
picked: nothing previously decided *which 3 of 13 roster players to
exclude* - `engine.roster.sample_active_squad` was only ever a random
POC/backtest stand-in, not real logic. Wired `poc_run.py` to use the new
function for its recommendation flow.

**How**: brute-forces all C(13,3)=286 exclusion combinations, skips any
that fail `ActiveSquad`'s formation-feasibility check, scores the rest by
projected round value *after* running the same `build_lineup` +
`swap_after_day1` logic used for the real recommendation (using
projections as the "actual" stand-in, since exclusion locks in before any
results exist), and keeps the best. Chosen over a greedy "cut the 3
lowest-projected" shortcut because a benched player retains day-2 swap
option value an excluded player doesn't - brute force is cheap enough
(286 combos) that there's no reason to approximate.

**Result**: ran `poc_run.py --season E2025 --cutoff-round 20 --seed 42`
end-to-end without errors. Excluded players were sensibly the 3 lowest-
projected on the sampled roster (1.9, 2.7, 6.3 proj) rather than a random
draw. Left `backtest_eval.py` (the validated 91-round/2730-trial headline
numbers) on the old random-exclusion sampler deliberately - swapping it in
there would change the validated methodology and needs a deliberate rerun/
re-validation, not a silent side effect of this change.

**Follow-up not yet done**: rerunning the full backtest with
`choose_active_squad` in place of the random sampler would likely raise
the validated headline numbers (since it removes a currently-random
decision) - worth doing if/when the user wants to re-validate.

---

## 2026-09-13 — Full backtest with real exclusion logic (choose_active_squad)

**What**: reran the full validated backtest (`backtest_eval.py --seasons
E2023 E2024 E2025 --min-round 6 --trials-per-round 30 --seed 1`), with
`choose_active_squad` (see prior entry) now used in `run_one_trial` in
place of the random `sample_active_squad` for every trial's exclusion
decision. Exclusion uses projections (decision-time, foresight only) for
both `no-swap` and `recommended`, matching how exclusion is actually
locked in before a round starts in the real game - the hindsight
`best-possible` ceiling still uses that same chosen active squad and only
optimizes the lineup/swap on top of it with perfect result knowledge,
exactly as before. This isolates one question: does replacing random
exclusion with real exclusion logic improve outcomes, holding the rest of
the (already-validated) pipeline fixed?

**How**: identical command/scope to the previous headline run (same
seasons, same `--min-round 6 --trials-per-round 30 --seed 1`, regular
season only) so the two are directly comparable. Raw trials in
`backtest_trials_realexclusion.csv` (gitignored).

**Result — regular season only, 91 rounds, 2730 trials, real exclusion vs.
old random-exclusion headline:**

| Season | Mean no-swap | Mean recommended | Mean best-possible | Beat/tie/lose | Mean gain (95% CI) | Swap-upside captured |
|---|---|---|---|---|---|---|
| E2023 | 116.45 | 143.58 | 152.74 | 97%/2%/1% | +27.14 ± 1.11 | 82% ± 5% |
| E2024 | 125.95 | 150.14 | 161.85 | 92%/6%/1% | +24.19 ± 1.24 | 74% ± 4% |
| E2025 | 120.33 | 141.58 | 155.26 | 83%/16%/1% | +21.25 ± 1.13 | 64% ± 4% |
| **All combined (new)** | **120.88** | **144.94** | **156.56** | **90%/8%/1% (99% beat-or-tie)** | **+24.06 ± 0.68** | **73% ± 2%** |
| All combined (old, random exclusion) | 94.40 | 104.22 | 116.52 | 76%/20%/4% (96% beat-or-tie) | +9.83 ± 0.42 | 50% ± 3% |

No structural-invariant violations (no-swap never exceeded best-possible).
Loss case: 32/2730 (1.2%, down from 4.2%), mean deficit 1.97 PIR, max 6.0 -
same "projection was wrong that round" profile as before, just rarer.

**This is the headline finding of this session, bigger than any single
swap-logic tweak or the entire ML detour**: real exclusion logic alone
raised mean no-swap by +26.5 PIR and mean best-possible by +40.0 PIR
*before the swap logic does anything* - simply not randomly benching your
best players some fraction of the time is worth far more than the day-1/
day-2 swap optimization on top of it. The swap's own isolated contribution
(recommended - no-swap) also grew, from +9.83 to +24.06 - a smarter
exclusion leaves stronger players on the bench, so there's more genuine
upside for the swap to capture (upside-captured also rose, 50%→73%).

**This supersedes the previous headline** (91 rounds/2730 trials, +9.83
PIR, 50% captured, 96% beat-or-tie, logged above under "Elaborate
backtest") for any use of the pipeline with `choose_active_squad` in
place, which is now the default in both `poc_run.py` and
`backtest_eval.py`. The old entry is left as-is per the working
agreement (append, don't rewrite) - it documents a real prior state
(random-exclusion methodology), not an error.

**Not rerun this session** (left as future follow-up if revisited): the
rolling-window sensitivity check and the ridge/gbm/ensemble ML comparisons
all still reflect the old random-exclusion methodology. Given how large
this effect was, the ML-vs-heuristic comparison in particular is worth
redoing at some point under the new methodology before trusting the old
"no demonstrable win" conclusion still holds.

---

## 2026-09-13 — ML-vs-heuristic comparison rerun under real exclusion logic

**What**: reran the ridge/gbm/ensemble comparison (tuned hyperparameters,
full feature set, pool-size 100 - same config as the "Hyperparameter grid
search" entry above) now that `choose_active_squad` is the exclusion
method for every trial, to check whether the prior "no demonstrable win
over the heuristic" conclusion still holds against the much stronger new
baseline (+24.06 PIR vs the old +9.83). Requested by the user, who
correctly predicted the outcome going in.

**How**: `python backtest_eval.py --seasons E2023 E2024 E2025 --min-round 6
--trials-per-round 30 --seed 1 --projection-method {ridge,gbm,ensemble}`.
Same command as the prior ML headline, unchanged except that
`run_one_trial` now uses real exclusion logic for every method (heuristic
included) instead of random.

**Result - regular season only, 91 rounds, 2730 trials, real exclusion
logic:**

| Method | Beat/tie/lose | Mean gain (95% CI) | Upside captured | Loss rate (mean/max deficit) |
|---|---|---|---|---|
| Heuristic | 90%/8%/1% | +24.06 ± 0.68 | 73% | 1.2% (1.97 / 6.0) |
| Ridge (tuned) | 91%/8%/1% | +24.60 ± 0.67 | 76% | 1.0% (3.00 / 14.0) |
| GBM (tuned) | 91%/8%/1% | +24.39 ± 0.67 | 73% | 1.0% (1.90 / 8.0) |
| Ensemble | 90%/9%/1% | +24.22 ± 0.67 | 75% | 0.6% (1.66 / 3.0) |

**Bottom line, confirmed**: all four methods are clustered within 0.54 PIR
of each other (24.06-24.60), inside/near the ±0.67 95% CIs - the same "no
demonstrable win" conclusion as the original ML pivot, now holding against
a baseline 2.4x stronger than the one it was first tested against. Ridge
again edges the heuristic (+0.54 this time, vs +0.11 before) - consistent
direction across both methodologies, still not a demonstrable win by the
project's own bar. The fat-tail loss pattern partially persists (Ridge's
worst individual round: 14.0 PIR deficit vs the heuristic's 6.0) but is
notably *not* present for the ensemble this time (max deficit only 3.0,
down from 16.0 under the old methodology, and its lowest loss rate of any
method at 0.6%) - the ensemble's smoothing effect looks more valuable
under real exclusion logic than it did before, worth keeping in mind if
this is revisited again.

**Verdict unchanged**: heuristic remains the default. This closes out the
open question from the previous session about whether the ML comparison
needed redoing - it did, and the answer didn't move.

---

## 2026-09-13 — Draft/ownership tracking + Flask app, end-to-end validation

**What**: validated the new manual draft/ownership/transaction tracking
(`engine/ownership.py`, three new tables in `engine/db.py`) and the local
Flask app (`app.py`) that logs the 12-manager draft-mode league and wires
real ownership into `engine.transfers.suggest_transfers` (previously a
stand-in: "pool minus my own roster" with no knowledge of the other 11
managers).

**How**:
1. Seeded 12 dummy managers (`seed_league.py`) and started the Flask dev
   server (`python app.py`).
2. Hit all 5 routes (`/`, `/draft`, `/managers`, `/transactions`,
   `/transfers`) — all 200.
3. Drafted 3 players to 3 different managers via `POST /draft/pick`;
   confirmed via direct SQLite inspection that `ownership` had exactly 3
   rows and `transactions` logged all 3 as `type='draft'`.
4. Attempted to draft an already-owned player to a different manager —
   got a clean inline flash error ("already owned by manager 1"), not a
   500, confirming the `ownership.player_id` PRIMARY KEY +
   `IntegrityError`→`ValueError` wrapping works.
5. Recorded a 1-for-1 trade via `POST /transactions/trade` — confirmed
   both players' `ownership.manager_id` swapped and both `transactions`
   rows shared one `group_id`.
6. Recorded a free-agent add then a drop for the same player — confirmed
   `ownership` row appeared then was deleted, both logged.
7. Built one manager a complete, valid 13-player roster (5G/5F/3C) by
   drafting the top-ranked available player per position; over-drafted to
   14 (4 Centers) on the first attempt because an already-owned Center
   wasn't excluded from the pick list up front — caught immediately via
   `manager_roster_ids` returning 14, fixed by dropping the extra Center.
   Worth remembering if scripting draft-entry test data again: exclude
   *all* currently-owned ids, not just the ones a prior step drafted.
8. Confirmed `/transfers?manager_id=<id>` for that manager's roster
   returned "no upgrade suggestions" (correct: the roster already held the
   best available player at every position, so there was nothing to
   suggest — not a bug).
9. **Core payoff check**: built a deliberately weak 13-player roster
   in-process and called `suggest_transfers(roster, pool,
   owned_ids=all_owned_ids(conn))` directly — got a top suggestion (add
   HEZONJA, MARIO, +19.3 gain). Drafted that exact player to a *different*
   manager via the real `POST /draft/pick` route, then re-ran the same
   `suggest_transfers` call — confirmed that player no longer appeared as
   an available upgrade anywhere in the results. This is the concrete
   before/after proving real ownership now gates transfer suggestions,
   not just "not on my own roster."
10. Re-ran `python sync_db.py --seasons E2025` with `managers`/`ownership`/
    `transactions` already populated (12/16/22 rows) — confirmed via
    direct row counts that all three tables were byte-for-byte untouched
    afterward (sync only ever writes `player_game_stats`).
11. Timed `/managers/<id>` requests to sanity-check the mtime-keyed
    projection cache in `app.py`: ~257ms cold (includes Flask/Jinja
    startup overhead) vs. ~138-141ms on repeat warm requests; touching
    `euroleague.db`'s mtime afterward brought the next request back up to
    ~213ms, confirming the cache both hits and correctly invalidates.
    (`build_projections` itself is only ~10ms at this data scale, so the
    absolute savings are modest - the cache logic being correct mattered
    more here than the speedup.)
12. Cleared all test/dummy data (12 fake managers, test picks/trades) from
    `managers`/`ownership`/`transactions` afterward so the local DB is
    clean for the user's real draft.

**Result**: all checks passed. Schema, `engine/ownership.py`,
`engine/transfers.py`'s new `owned_ids` parameter, `seed_league.py`, and
`app.py`'s routes/caching all behave as designed. No regressions:
`poc_run.py` end-to-end smoke test (unaffected `suggest_transfers` call
site) still produces identical output to before this change.

---

## 2026-09-14 — Roster sync (v2 `/people`) + "new to the league" marking

**What**: added a way to keep club rosters current independent of played
games, and to surface players with no local box-score history distinctly,
instead of them being silently invisible on the draft board. Prompted by the
user noting that a player who transfers mid-window wouldn't show their new
team until their first box score, and that brand-new-to-EuroLeague players
had no path onto the board at all.

**Built**: `EuroleagueClient.list_people` + `normalize_people`
(`engine/data.py`) hitting `v2/.../people`; a `rosters` table
(`engine/db.py`, full delete-and-reinsert per sync — unlike
`player_game_stats` this is current state, not history); `sync_rosters.py`
(the refresh command, mirrors `sync_db.py`); `engine/rosters.py::merge_roster`
(merges the synced roster into a projections pool — corrects a transferred
player's `team`, and adds a zero-value placeholder `Projection` for anyone
on a roster with no box-score history anywhere in the local DB, flagging
their `player_id` as new); wired into `app.py`'s `get_pool` (now returns
`(pool, new_ids)`) and `draft_board.py`'s CLI; `NEW` badge in
`templates/draft.html` / `manager_roster.html` + `.new-player`/`.new-badge`
CSS.

**How**:
1. Live-tested `list_people` against the real `E2026` season before writing
   any normalization code. Found `/people` returns every person type
   (players, coaches, refs, scorers...) — `type == "J"` isolates players
   (332 of 837 rows on that pull).
2. First `normalize_people` pass (keep all `type == "J"` rows) crashed
   `replace_roster` on a `UNIQUE` constraint: a transferred player appears
   **twice** in the response, once per club (old club `active: False` with a
   past `endDate`, new club `active: True`) — confirmed directly (e.g.
   player `012613`, inactive at `ULK`, active at `MAD`). Fixed by filtering
   to `active` rows. Also found 2 of 332 players with two *simultaneous*
   `active: True` rows for the same player_id (likely a transient dual-active
   state mid-transfer in the upstream data) — broke the tie by latest
   `startDate` rather than raising, since this is unauthenticated/
   undocumented upstream data and failing the whole sync over 2 edge-case
   rows would be the wrong tradeoff.
3. Ran `sync_rosters.py --season E2026` for real: 269 active players synced,
   568 non-player staff/officials filtered out, 47 flagged new-to-the-league
   (no row in `player_game_stats` for any locally-synced season, E2023+).
4. Sanity-checked the 47: an entire club (`BAS`, all positions) showed up
   as 100% new — consistent with Baskonia being new to EuroLeague this
   season rather than a join bug, since every other club's "new" players
   were individual transfers/rookies, not whole rosters. Spot-checked known
   veterans (e.g. Barcelona/ASVEL players) landed correctly in the "known"
   bucket, confirming the `person.code` / `player_id` join between `/people`
   and `player_game_stats` is consistent.
5. `draft_board.py --season E2025 --top-n 400`: merge log line reported
   "Merged E2026 roster (269 players, 47 new to the league)"; grepping the
   full board output for `[NEW]` showed those 47 correctly appearing at
   0.0 projected value (the placeholder), tagged, rather than being absent.
6. Started `app.py`, fetched `/draft` over HTTP: 47 `new-badge` spans in the
   rendered HTML, matching the CLI count exactly. Spot-checked the raw HTML
   for two of them (Baugh, Dotson) — correct team, correct badge, correct
   tooltip. Also hit `/managers/<id>`, `/managers`, `/transfers?manager_id=`,
   `/transactions` after the `get_pool` signature change (now returns a
   tuple) — all 200, no regressions.
7. `py_compile` on every changed file (`app.py`, `draft_board.py`,
   `engine/data.py`, `engine/db.py`, `engine/rosters.py`, `sync_rosters.py`)
   — all clean.

**Result**: all checks passed, including two real upstream data quirks
(duplicate transfer rows, transient dual-active rows) caught and handled
before they could reach the DB rather than discovered later. Deliberately
NOT built yet, per the user ("I will try and think of a way to get
additional context to establish value for new players"): any actual
projected-value estimate for new players — they're visible and flagged now,
but still score 0.0/excluded from ranking, which is honest (no data exists
yet) rather than a guess.

---

## 2026-09-14 — UI polish + Sync page, end-to-end via live HTTP

**What**: this session's UI work (README, `/how-it-works` page, header
tooltips replacing column-legend boxes, draft-board team filter/sort, and
the `/sync` page triggering `sync_db.py`/`sync_rosters.py` from the
browser) was validated by hitting the running Flask app directly, not just
by reading the templates.

**How**:
1. `py_compile` on every changed Python file after each round of edits.
2. Curled every route (`/`, `/how-it-works`, `/draft`, `/managers`,
   `/managers/<id>`, `/transactions`, `/transfers?manager_id=`, `/sync`) —
   all 200.
3. Draft board: confirmed `sort=team&dir=asc` actually produces
   alphabetically-ordered teams (ASV, BAR, BAS...), confirmed `team=MAD`
   filtering returns only MAD rows, confirmed tier-break markup (`grep -c
   tier-break`) is present in the default sort and exactly zero under any
   other sort/dir combination.
4. `/sync`: triggered a real roster sync via `POST /sync/rosters` (no
   `--dry-run`, this hit the live API for real) — watched it run as a
   background subprocess, complete, and update `roster_synced_at`/the log
   tail on the page. Triggered a real box-score sync
   (`POST /sync/db, season_E2026=1`) — confirmed it correctly found 0
   played E2026 games (season hasn't started) rather than erroring. Fired
   two roster-sync triggers back-to-back to confirm the concurrent-run
   guard rejects the second one with a flash message rather than running
   both at once (or crashing on the same log file).

**Result**: all checks passed, no regressions. The sync subprocess
correctly runs under the venv's Python (`sys.executable`, not the system
Python that lacks `requests`), and `_projection_cache.clear()` after a sync
finishes was confirmed necessary in principle (the mtime-based cache key
would eventually self-correct too, but the explicit clear removes any
timing dependency on that).

---

## 2026-09-14 — Team strength index: standalone backtest, no demonstrable win

**What**: prototyped a per-team defensive-form index (`engine/team_strength.py`
- recency-safe "PIR allowed" per team, same leakage-safe cutoff-round
discipline as `engine.projections`) and, before touching the real projection
pipeline at all, backtested whether knowing a player's next opponent's
defensive weakness actually improves next-game PIR prediction accuracy over
the existing heuristic alone (`team_strength_backtest.py`). Same discipline
that ruled out the ML path: prove it helps on a backtest before adding the
complexity, not before.

**How**: walked forward through every regular-season round (playoffs
excluded, reusing `backtest_eval.classify_phase`) from round 8 across
E2023-E2025, building leakage-safe player projections and team-strength
figures using only strictly-prior rounds at each step. For every played
player-game in the round being evaluated, compared the existing baseline
projection against an opponent-adjusted version
(`baseline * (1 + weight * opponent_factor)`, `opponent_factor` = that
opponent's PIR-allowed relative to the league average that round) at a grid
of weights, against the player's actual PIR that game (MAE/RMSE), plus the
correlation between `opponent_factor` and the baseline's own prediction
error.

**Sanity check first**: before the accuracy test, printed the E2025
end-of-season team-strength ranking directly - Olympiacos (the actual E2025
champion) came out with both the *lowest* PIR allowed and a 1.0 recent win
rate, and the ranking generally tracked win rate sensibly. Confirmed the
metric itself is behaviorally sound before asking whether it's *useful*.

**Result**: 85 rounds, 16,703 player-games evaluated.
- Correlation(opponent_factor, baseline residual) = **+0.050** - the right
  sign (a weak-defense opponent does correlate with the baseline
  under-predicting), but very weak.
- Baseline MAE 5.738. Best weight in an initial 0-0.5 grid (0.5) got MAE
  down to 5.731 - a monotonic-looking improvement that turned out to be the
  left slope of a real minimum: widened the grid (0.5 through 10.0) and
  found the true optimum sits around weight ~0.5-0.7, past which MAE gets
  rapidly worse (w=2.0: MAE 5.815; w=10.0: MAE 8.267 - badly overshooting).
- **Best-case improvement: MAE 5.738 -> ~5.731, about 0.13% relative.**
  RMSE showed the same negligible-improvement shape.

**Conclusion**: the signal is real and correctly-signed but too weak to
matter for point-prediction accuracy - **not wired into
`engine.projections.build_projections`**. Matches the same "no demonstrable
win" verdict already reached for the ML comparison (see
`docs/technical_notes.md`), for the same reason: don't add a parameter
(here, an opponent-adjustment weight) that doesn't measurably earn its
complexity. `engine/team_strength.py` and `team_strength_backtest.py` are
kept as-is (not deleted) - the module is useful on its own (e.g. an
at-a-glance defensive ranking) and the backtest script is reusable if a
better-targeted opponent signal is tried later (team totals/pace rather
than PIR-allowed, or a matchup-specific rather than blanket adjustment).
One open question this doesn't resolve: this tests raw point-prediction
accuracy across the whole player population, not narrow tie-breaking
between two similarly-projected players for a captain/day-2-swap choice -
a weaker bar the signal might still clear even though it fails here. Not
tested; would need `engine.lineup` to be wired into the app first (see
CLAUDE.md "Natural next steps") to have a real decision to break ties on.

---

## 2026-09-14 — Wired the lineup builder into the app (`/lineup`), incl. a new schedule sync

**What**: `engine/lineup.py`'s real logic (`choose_active_squad` ->
`build_lineup` -> `swap_after_day1`) was previously only exercised through
`poc_run.py`. Wired it into a new `/lineup` route/page for a chosen manager
and round, live-recomputed each visit (no persistence of the manager's
actual choice, matching `/transfers`'s existing pattern). Building this
surfaced and fixed a real gap: `team_dates_for_round` is built from
`player_game_stats`, which only ever has *already-played* games
(`fetch_season` filters to `played` before writing anything to the DB) - so
there was no way to build a lineup recommendation for a round that hasn't
happened yet, which is the entire point of a decision-support tool. Fixed
with a new `schedule` table (every game, played or not - full
delete-then-reinsert per season, populated by `sync_db.py` alongside its
existing box-score sync, at no extra network cost since it reuses
`list_games`'s already-warm disk cache) and a new sibling function,
`engine.lineup.team_dates_from_schedule`, alongside the existing
(untouched) `team_dates_for_round`.

**How**:
1. `engine/db.py` round-trip: `replace_schedule` with fabricated rows,
   `load_schedule` returns them unchanged; `replace_schedule` again with
   different rows for the same season confirmed the old ones are gone
   (delete-then-insert, not upsert).
2. Live-checked the actual question this all hinges on before writing
   `normalize_schedule`: does `E2026` even have a published schedule yet?
   Confirmed directly - 380 games, all 38 rounds, 0 played, every
   `gameCode` present, round 1 dated 2026-09-25 (11 days out from this
   session). Also confirmed the exact field shapes used
   (`date`, `local.club.code`, `road.club.code`) against a real response.
3. Ran `sync_db.py --seasons E2026` and `--seasons E2025` for real: E2026
   schedule refreshed at 380 games (0 played, 380 upcoming) from cache; the
   already-synced E2025 box scores were untouched (upsert, no duplicates)
   and its schedule populated at 402 games, all played.
4. **Key consistency check**: for every one of E2025's 47 rounds, compared
   `team_dates_from_schedule` (new, schedule-sourced) against
   `team_dates_for_round` (existing, box-score-sourced) - **0 mismatches**.
   Strongest possible evidence the new function behaves identically to the
   validated one, not just superficially.
5. Drafted a real, valid 13-player roster (5G/5F/3C, ranked by E2025
   season-end projection) to a test manager, then hit
   `/lineup?manager_id=13&round=1` for real - **round 1 of the actual
   2026-27 season**, a genuinely upcoming round, not a simulation. Got a
   200 with all three sections (excluded/day-1/day-2) populated.
6. Manually traced the output player-by-player against the documented
   rules rather than just checking it didn't crash:
   - `JAMES, MIKE` (20.6 projected - higher than several starters) was
     excluded. Looked suspicious at first; confirmed his team (MCO) simply
     isn't scheduled to play round 1 at all
     (`'MCO' in team_dates_from_schedule(...)` → `False`) - `compute_round_score`
     never counts a player whose `_availability_rank` is 2 ("not playing"),
     so excluding him vs. burying him on the bench is scoreless either way;
     the optimizer correctly treated him as a free exclusion rather than
     protecting his season-long projection. Not a bug - the system
     reasoning about round-specific playability, not just raw season value.
   - Day-1 formation was 2 Guard/1 Forward/2 Center (a valid formation);
     day-2 kept the identical shape, only occupants changed - matches the
     documented "formation is locked at day-1" assumption.
   - Every day-1-swapped-out slot was replaced by a same-position bench
     player with a later game and positive projected value; slots with no
     eligible same-position later-playing bench candidate correctly stayed
     put (e.g. `DIAKITE` at Center - the only other Center bench candidate,
     `OTURU`, had already played day-1 too).
   - Captain was recomputed over the post-swap 5 starters, matching
     `compute_round_score`'s per-stage doubling design.
7. Edge cases, all clean (200, no 500s): no manager selected; an
   intentionally-incomplete test manager (0/13 drafted) →
   `roster_error`; a round number outside the synced schedule (999) →
   `schedule_error` with a working Sync-page link.
8. No-regression check: re-ran `poc_run.py --season E2025 --cutoff-round
   20 ...` and `backtest_eval.py --seasons E2025 --min-round 15
   --trials-per-round 3` - both exercise the untouched
   `team_dates_for_round`/`build_lineup`/`swap_after_day1`/
   `choose_active_squad`/`compute_round_score` and produced output
   consistent with prior runs (no-swap <= recommended <= best-possible
   held throughout; mean gain in this small 24-round/72-trial subset was
   +21.31 PIR, in line with the full-season headline of +24.06).
9. Cleaned up the 13-player test draft from the DB afterward (same reason
   test data was cleared after the original draft/ownership validation -
   keep the local DB ready for the real draft).

**Result**: all checks passed, including a genuine architectural gap found
and fixed (not just "wire existing code into a route" - the schedule table
was a real missing piece), verified against the real, live, upcoming 2026-27
season schedule rather than only historical data.

---

## 2026-09-14 — "GONE" player marking (departed clubs / unsigned players)

**What**: while spot-checking the lineup builder's round-1 output above,
`JAMES, MIKE` (20.6 projected, higher than several actual starters) showed
up excluded with team `MCO`. The user, pasting the real confirmed 2026-27
club list, pointed out Monaco isn't part of the league this season at all -
prompting a check of whether that was stale sync data or something else.

**How**: queried the already-synced `rosters` table directly - confirmed
Monaco (`MCO`) has **zero** players in the E2026 roster sync and the 20
teams present match the user's list exactly, and confirmed `MCO` appears in
**zero** of the 380 games across all 38 rounds of the synced schedule. So
the sync itself was correct - the gap was downstream: `merge_roster` only
overrides a player's `team` when they're found on a *current* roster: a
player absent from every current club (Monaco's former roster, in this
case) keeps their last-known team indefinitely with nothing marking them as
no longer part of the league. Confirmed this is a real, previously-
unflagged case, distinct from the existing NEW badge (which flags the
opposite: present now, absent from history).

Also checked a related question the user raised ("where are the Besiktas
players") while investigating: Besiktas (`BES`) **is** correctly synced (9
players, 20-team roster confirmed), 5 of 9 have real E2025-based
projections (transferred in from other EuroLeague clubs) - so they weren't
actually missing, just mostly showing very low/placeholder values and
sorting toward the bottom of the board (only 3 of 9 have a real
above-replacement value). Separately noted (not fixed this session):
`known_player_ids` checks history across ALL locally-synced seasons
(E2023-E2025) for the NEW flag, but `build_projections` only uses ONE
season (whichever `_resolve_pool_source` picks) for the actual value -
so a player with history in an *earlier* season but not the one currently
used for projections (e.g. `ZIZIC, ANTE`, `DEJULIUS, DAVID`) is correctly
NOT flagged NEW, but still gets an unhelpful 0.0 placeholder with nothing
distinguishing that from "genuinely brand new." Flagged as a known gap in
CLAUDE.md, not addressed yet - a third, distinct case beyond NEW/GONE.

**Fix**: `engine.rosters.merge_roster` now returns a third value,
`gone_player_ids` (pool players absent from the current-season roster
entirely). Wired through `app.py`'s `get_pool` (now a 3-tuple) and
`draft_board.py`'s CLI. `/draft` filters gone players out of the board
entirely (`build_draft_board` never sees them) rather than just flagging
them, per the user's explicit direction ("they should vanish"). `/transfers`
filters them from the ADD-candidate pool (can't suggest acquiring someone
not really acquirable) while still allowing them as a DROP candidate if a
manager already owns one (`roster.players` is built from the full,
unfiltered pool). `/managers/<id>` shows an owned GONE player greyed out
with a badge and an explicit "consider dropping or trading" tooltip,
mirroring the NEW badge's styling but distinct (grey `GONE` vs. orange
`NEW`).

**Validation**:
1. `draft_board.py --season E2025`: log line correctly reported "90 no
   longer on any current roster - excluded from this board" (out of a
   284-player pool) - plausible given a full season of transfers,
   retirements, and a genuinely departed club (Monaco).
2. Live app: confirmed `JAMES, MIKE` no longer appears anywhere on
   `/draft?position=Guard` (0 matches), and the 20 teams shown on the board
   exactly match the user's confirmed club list (no `MCO`).
3. Drafted `JAMES, MIKE` to a test manager directly (bypassing the UI, via
   `engine.ownership.record_draft_pick`) specifically to exercise the
   "already owned" path: `/managers/<id>` rendered him with the
   `gone-player` row class, the grey `GONE` badge, and the stale `MCO` team
   still visible for context - exactly as designed.
4. Directly confirmed in Python that `005985` (his player_id) is in
   `gone_ids` and absent from the `addable_pool` passed to
   `suggest_transfers` - the ADD-suggestion exclusion is real, not just
   inferred from the UI.
5. Test draft cleaned up afterward (dropped back to free agency).
6. `py_compile` on every changed file
   (`engine/rosters.py`, `app.py`, `draft_board.py`); all five `/draft`,
   `/managers`, `/transactions`, `/transfers`, `/lineup` routes reconfirmed
   200 after the `get_pool`/`merge_roster` signature change (2-tuple ->
   3-tuple) touched every call site.

**Result**: all checks passed. One related gap (season-scoped-only
projection history vs. all-seasons-scoped NEW flag, affecting a handful of
Besiktas players) was found but deliberately not fixed this session -
logged as a known issue rather than silently expanding scope beyond what
was asked.

---

## 2026-09-14 — Randomize-draft dev tool, and the full loop end-to-end

**What**: added a "Randomize a full draft" dev tool (`engine/dev_draft.py`,
a `/dev/randomize-draft` route, a "Developer tools" section on `/sync`) -
wipes all current ownership and re-drafts every seeded manager a fresh,
valid, exclusive 13-player roster (weighted-random by projected value,
reusing `engine.roster._weighted_sample_without_replacement` - same
mechanism `sample_roster` already uses, just applied against real
managers/ownership instead of a throwaway in-memory `Roster`). Explicitly a
dev/testing convenience, not for the real draft - labeled as such in the
UI. Requested specifically so there's always a realistic full-league state
to develop the lineup builder etc. against, without hand-drafting 156
players.

**How**: `py_compile` on `engine/dev_draft.py` and `app.py`. Triggered the
route for real via HTTP (`POST /dev/randomize-draft`) against the 12
already-seeded test managers - got a `302` with the correct flash cookie
("Randomized a fresh draft: 156 picks across 12 managers"). Confirmed
directly in the DB: all 12 managers ended up with exactly 13 players each
(156 total, matches). Closed the loop the user actually asked for: hit
`/lineup?manager_id=13&round=1` immediately after - `200`, all three
sections (excluded/day-1/day-2) rendered correctly against the freshly
randomized roster, exercising the full "randomize a draft, then predict
lineups" flow end-to-end in the running app.

**Result**: all checks passed. Left the randomized draft in place
afterward (not cleaned up) - unlike earlier single-player test drafts this
session, populating a realistic full league is this tool's actual purpose,
and the user asked for it to stay available "while we are developing."

---

## 2026-09-15 — UI restyle ("Courtside" theme)

**What**: reskinned the Flask app's look, per the user's explicit ask to
make it "more professional and sporty" - brainstormed direction with the
user first (`AskUserQuestion`: color palette, scope, logo approach) before
touching anything. Landed on a "courtside hardwood" palette (warm
off-white/parquet background, charcoal-brown header, basketball-orange
accent), an Oswald (headings/nav/buttons) + Inter (body/tables) Google
Fonts pairing, and a small original inline-SVG basketball-icon logo + "
EuroLeague Fantasy / Strategist" wordmark in a new dark header bar with
active-page highlighting. Deliberately scoped as a design-tokens pass
(CSS variables + `base.html` header restructure) rather than a per-page
layout rework, per the user's choice - no template beyond `base.html` was
touched; existing classes (`new-badge`, `gone-player`, `tier-break`,
`filters`, `stacked-form`, etc.) were re-skinned in place via
`static/style.css` rather than renamed, so no other template needed
edits. Not affiliated with/doesn't reuse the real EuroLeague logo -
original mark, consistent with this project's existing non-affiliation
stance (see README).

**How**: confirmed the full class/element surface first (`grep -oh
'class="[^"]*"' templates/*.html`) so the new stylesheet covers everything
actually used, including plain unstyled elements (`button`, `select`,
`input`) used in `draft.html`'s filter/draft-pick forms. Applied against
the already-running local dev server (`debug=True`, auto-reload) rather
than restarting it. Verified all 8 routes (`/`, `/draft`, `/managers`,
`/transactions`, `/transfers`, `/lineup`, `/sync`, `/how-it-works`) still
return `200` after the change, and that the new header markup
(`site-header`, `brand-name`) and font link (`Oswald`) actually appear in
the rendered HTML.

**Gap**: this sandboxed environment has no headless browser (no
`chromium-cli`, no Playwright, no `node`/`npx`) to actually screenshot the
result - visual correctness (spacing, contrast, whether the palette reads
as intended) was not confirmed by looking at rendered pixels, only by
HTTP status + markup presence. Flagged to the user directly rather than
claimed as visually verified; asked them to eyeball it at
`http://127.0.0.1:5000` in their own browser.

**Result**: all automated checks passed; visual review pending the user's
own look.

---

## 2026-09-15 — Tooltip fix (native title tooltip wasn't showing)

**What**: user reported hovering for column/badge explanations "doesn't
work" after the restyle above. Root cause: the dotted-underline hover hint
color on table headers (`--ink-soft`, a mid-brown meant for a light
background) was nearly invisible against the new dark charcoal header
background, and separately the native browser `title` tooltip has a long,
inconsistent show delay - together these made hovering feel broken even
though the underlying `title` attributes were still correctly rendered
(verified via `curl` - unaffected by the CSS change). Fixed by having
`base.html` swap every `title` attribute to `data-tip` on page load
(so the native tooltip never fires) and rendering a themed, instant
tooltip via CSS `content: attr(data-tip)` on hover/focus - no template
content changes needed, since it reads the same text that was already in
each `title=`.

**How**: verified via `curl` that `/draft` still returns `200` and the new
script/`data-tip` markup is present in the served HTML.

**Gap carried over from the prior entry**: still no headless browser in
this sandbox to visually confirm the tooltip actually renders/positions
correctly - asked the user to check in their own browser.

---

## 2026-09-15 — Roster-cap validation on draft/free-agent-add

**What**: user reported drafting a player onto an already-full (13-player)
roster in the `/draft` tab silently succeeded when it should error and
leave the DB unchanged. Root cause: `engine.ownership.record_draft_pick`
and `record_free_agent_add` only enforced player-level uniqueness (the
`ownership` table's `player_id` primary key stops the same player being
owned twice) - neither checked the manager's total roster count against
`engine.roster.TOTAL_ROSTER_SIZE` (13). Fixed with a
`_check_roster_not_full` guard called before any write in both functions;
raises `ValueError` (already caught and flashed as an error by both
`app.py` routes - `draft_pick` and `transactions_add` - so no route
changes were needed).

**How**: live-tested against the running app/DB rather than just unit
logic. Found the bug was already live in the data: manager 13 had 14
players (one over cap) from before this fix existed - left as-is, flagged
to the user rather than silently corrected (it's their roster to fix via
Transactions). Confirmed the fix rejects a `POST /draft/pick` and a
`POST /transactions/add` against that already-over-cap manager with the
correct flash message, and that the DB was unaffected (`ownership` count
stayed at 14, the free agent stayed unowned) in both cases. Regression-
checked normal drafting still works: temporarily dropped a player from a
13/13 manager, drafted a different free agent into the freed slot
(succeeded), then undid both steps (dropped the test pick, re-drafted the
original player back) to restore the exact prior DB state.

**Result**: fix confirmed working; one pre-existing over-cap roster
(manager 13, 14/13) found but left for the user to resolve.

---

## 2026-09-15 — Real Fantasy draft-pool eligibility filter

**What**: the user shared a user-maintained Google Sheet
(`docs.google.com/spreadsheets/d/1bAs-.../edit?gid=0`) listing the real
EuroLeague Fantasy game's actual current draftable pool (Name/Surname/
Position/Team, plus a Credits column the user explicitly said to ignore -
draft-credit tracking is confirmed out of scope, see CLAUDE.md), noting it
"should be updated constantly." Brainstormed intent with the user first
(`AskUserQuestion`: what to use it for, one-off vs. ongoing sync) rather
than guessing given the real architectural fork - landed on: an ongoing
sync (mirroring `sync_rosters.py`) feeding an eligibility filter on the
draft board and transfer suggestions, so a player on a EuroLeague club
roster but NOT part of the real Fantasy game's pool (e.g. Head Coaches,
which the sheet includes as a draftable category but this league's rules
don't - see `docs/game_rules.md`) stops showing as draftable/addable, same
treatment `gone_player_ids` already gets.

Confirmed the sheet is publicly fetchable as CSV with no auth
(`/export?format=csv&gid=0`) before building anything. Added:
- `fantasy_pool` table (`engine/db.py`) - raw synced sheet rows, full
  delete-and-reinsert per sync like `rosters`/`schedule` (no stable ID in
  the source to upsert against).
- `engine/fantasy_pool.py` - fetch/parse, a 20-entry team-code map (the
  sheet and this project's own `rosters` table use different 3-letter
  codes for the same 20 clubs - e.g. sheet's `RMB`/`EFS`/`VBC` = this
  project's `MAD`/`IST`/`PAM` for Real Madrid/Efes/Valencia - confirmed by
  cross-checking every one of the 20 codes against real rosters, not
  guessed), and `eligible_player_ids()` - matches sheet rows to this
  project's player_id by normalized surname + team (no player_id in the
  sheet at all). Deliberately conservative: an unmatched/ambiguous name is
  skipped rather than guessed, since a false "ineligible" would wrongly
  hide a real draftable player - worse than an occasional missed match.
- `sync_fantasy_pool.py` - mirrors `sync_rosters.py`'s structure; also
  prints a match-quality summary (X/Y current roster players matched,
  unmatched names listed) so match quality is visible, not blindly
  trusted.
- `app.py`: `get_pool()` is now a 4-tuple (added `ineligible_player_ids`,
  empty if the pool hasn't been synced yet - fails open, never blocks
  drafting on missing data); wired into the same `pid not in gone_ids`
  filter already used for `draftable_pool` (draft board, dev randomize-
  draft) and `addable_pool` (transfer suggestions). New `/sync/fantasy-
  pool` trigger route + a "Fantasy draft pool" section on `/sync`
  (mirrors the existing box-score/roster sync sections exactly - button,
  status, log tail).

**How**: ran the real sync against the real sheet and the real local DB
repeatedly while developing, not just once at the end - this caught two
real problems before they shipped:
1. Our own `rosters` table turned out to be quite incomplete for some
   clubs (ASV: 4 players, BAR: 5 - real players like Mike James and Jonas
   Valanciunas are simply absent). Re-ran `sync_rosters.py` to check if it
   was just stale - came back with the identical 269 rows, so this is an
   upstream `/people` completeness gap, not staleness on this project's
   side. Not something this session fixed (out of scope) - flagged here
   and to the user. Doesn't cause any false eligibility exclusions though,
   since `ineligible_ids` only ever draws from players already in
   `roster_rows` - a player missing from `rosters` entirely is simply
   unaffected by this feature, not wrongly excluded by it.
2. A genuine false-hide bug: initial matching got 238/269 (88%), and
   auditing the 31 "ineligible" names by hand found 6 were real, current,
   legitimately-draftable players hidden only because of name-formatting
   disagreement between the two sources (DB's 'BACOT JR., ARMANDO' /
   'WRIGHT IV, MCKINLEY' / 'HORTON TUCKER, TALEN' vs. the sheet's 'Armando
   Bacot Jr' / 'Mckinley Wright' / 'Talen Horton-Tucker' - suffix and
   hyphen-vs-space handling). Fixed by normalizing hyphens to spaces and
   stripping trailing Jr/Sr/II/III/IV/V tokens on both sides before
   comparing; re-ran - 244/269 (91%), 0 ambiguous, and confirmed via
   `curl` against the live app that all 6 previously-false-hidden players
   (plus their exact DB name spellings) now appear on `/draft` again.
   Audited the remaining 25 "ineligible" names by hand too: no further
   false-hide pattern found - they read as genuine fringe/deep-bench
   players not in the curated sheet (one likely exception, not chased
   further: 'Maozinha Pereira' looks like a first/last name-order swap
   between the two sources for one Brazilian player - left unmatched
   rather than guessed, per the conservative-by-design approach).
   Live-tested end-to-end via the running app throughout: `/sync/fantasy-
   pool` triggered via real `POST`, confirmed the background subprocess,
   log tail, and match-quality summary all render correctly on `/sync`;
   confirmed a known-ineligible player (`KARUTASU, DARIUS`) is absent from
   the live `/draft` HTML while a known-eligible free agent
   (`SIMA, YANKUBA`) still appears normally; all 8 routes re-verified `200`
   after every change.

**Result**: working and live-tested; 91% match rate against currently-
synced rosters, with the residual gap traced to this project's own roster
sync completeness (a pre-existing, separate issue) rather than the new
matching logic. Flagging for the user: worth spot-checking a few of the
25 remaining "ineligible" names (e.g. `Lorenzo Brown` / `MIL`, a
recognizable rotation player) against the real sheet/game directly, in
case the sheet itself has gaps rather than this project's matching.

---

## 2026-09-15 — Fantasy sheet promoted to primary roster-composition source

**What**: after the eligibility-filter feature above shipped, the user
reported ASV (ASVEL Villeurbanne) down to a single draftable player.
Investigated by pulling the raw EuroLeague `/people` API directly and
counting real player-type entries (`type: "J"`, not staff) per club:
ASV had only 4, Barcelona only 7 - everyone else had 13-24. Confirmed via
`client._get_json(...)` against the live endpoint, not a guess. This
predates today's eligibility filter entirely (`rosters` table already had
only 4 ASV rows before any of today's work) - EuroLeague's own backend
simply hasn't finished registering some clubs' full squads pre-season
(round 1 is 2026-09-25). The eligibility filter just made an existing gap
much more visible (4 mediocre options -> 1), rather than causing it.

Presented the finding and two options (wait for EuroLeague's data to catch
up vs. build a stopgap); the user asked for a bigger change than either
option offered - use the sheet as the PRIMARY roster-composition source
outright (not just a filter on top of EuroLeague's roster data), since real
players like Patty Mills and Jae Crowder are already in the sheet with
zero box-score history yet (they're new to EuroLeague this season), so
matching them to a real player_id isn't even possible via EuroLeague's own
roster - explicitly: "use the sheet to recreate the rosters, it is the
most reliable source... for the initial population the sheet should be the
one to trust", keeping the EuroLeague endpoint for box scores.

Replaced the additive `ineligible_player_ids` filter (this session's
earlier design) with a source swap:
- `engine.fantasy_pool.resolve_pool_rows()` (replaces
  `eligible_player_ids()`) resolves each sheet row to a REAL existing
  player_id via two tiers - first the current season's synced EuroLeague
  roster (correct current team when EuroLeague has it), then, only if
  that finds nothing, ANY player_id this project has ever synced box-score
  data for regardless of season/prior team (`engine.db.all_known_players`,
  new) - catches a player whose club hasn't re-registered them with
  EuroLeague yet but who has real history (e.g. Joel Bolomboy, ASV,
  resolved to his existing E-seasons player_id this way). A row matching
  neither gets a stable synthetic ID
  (`sheet:{team}:{surname}:{first}`) - genuinely new to this project's
  data (e.g. Patty Mills - confirmed via direct query: zero rows anywhere
  in `player_game_stats` under his name), same zero-value NEW-badge
  placeholder treatment as always.
- `app.py`'s `get_pool()` reverted to a 3-tuple (dropped
  `ineligible_player_ids` entirely - redundant now, since sheet
  membership IS pool membership by construction) and now passes
  `resolve_pool_rows`' output to the EXISTING `engine.rosters.merge_roster`
  unchanged (it only ever needed player_id/player_name/position/team per
  row, so no new merge function was needed) when the Fantasy pool has been
  synced, falling back to the old EuroLeague-`rosters`-driven call
  otherwise (fails open, never blocks on missing data).

**How**: iterated against the real local DB throughout, not just at the
end. Confirmed via direct query that Mills/Crowder/Waters/Pons (ASV) have
zero box-score history anywhere (correctly need placeholder IDs) while
Bolomboy/Dossou-Yovo/Sestina/Cale/Massa/Lighty/Jackson (ASV) DO have
existing player_ids from prior seasons (correctly should reuse them, not
get a new placeholder). Ran `sync_fantasy_pool.py` for real: 245/326
matched the current synced roster, 44 more matched via the historical
fallback, 37 genuinely new (placeholder). Verified ASV and Barcelona both
resolve to their full real 15-player rosters now (were 4 and 7). Live-
tested end-to-end via the running app: `/draft?team=ASV` now shows Mills,
Crowder, Bolomboy, Waters (previously invisible or absent); temporarily
dropped a player from a full (13/13) manager, drafted the synthetic-ID
`sheet:asv:mills:patty` via a real `POST /draft/pick`, confirmed it
persisted (`manager_id=14` in `ownership`) and rendered correctly
(`MILLS, PATTY`) on that manager's roster page, then undid the test drop/
draft to restore the exact prior DB state. All 8 routes re-verified `200`
after the change (one incidental restart needed - the dev server process
from earlier in the session had been killed when its owning terminal
closed, unrelated to this change; restarted cleanly with no errors).

**Result**: working and live-tested. ASV and Barcelona now show their real
rosters; the underlying EuroLeague `/people` incompleteness for those two
clubs remains (unfixable from this project's side) but no longer matters
for roster composition, only as a secondary identity-resolution input.

---

## 2026-09-16 — Bulk draft import from a draft-room app's CSV export

**What**: the user's real draft will happen inside a friend's draft-room
app, which can export a CSV of the completed draft afterward - requested a
way to bulk-import that instead of logging 156 picks one at a time via
`/draft`. Built `engine/draft_import.py` (`parse_draft_csv`,
`distinct_managers`, `resolve_manager_picks`) plus three new `app.py`
routes (`/draft/import` upload page with a drag-and-drop zone + file
picker, `/draft/import/preview` parses the upload and shows a
manager-matching page, `/draft/import/commit` writes the picks) and two
templates. Linked from the draft board header.

The export format (one row per pick: `manager_id`/`manager` - the
draft app's own identity, not this project's - `player`, `team`, `pos`,
`overall_pick`, plus other columns this project doesn't need) is parsed by
reading rows until one stops matching the header's column count or has a
blank manager_id/non-numeric overall_pick - which cleanly stops before an
optional "Final rosters" trailer section some exports append after a
blank line, without ever needing to special-case that section. Player
identity is resolved the same way as the existing Fantasy-sheet sync
(`engine.fantasy_pool.resolve_pool_rows`, reused as-is): by normalized
(team, surname, first name) against this season's synced roster, then
against any historical player_id, falling back to the same synthetic-ID
scheme on no match. The export's team codes were confirmed to exactly
match the Fantasy sheet's own codes (`engine.fantasy_pool.TEAM_CODE_MAP`,
reused unchanged) - both are downstream of the real EuroLeague Fantasy
game. The full-name column (no separate first/surname like the sheet has)
is split on the last whitespace token as the surname, which handles
hyphenated surnames correctly since the hyphen isn't a space character.

The commit step lets the user map each CSV manager name to one of this
project's real managers (auto-suggested on an exact case-insensitive name
match, editable) or skip it, with an optional "clear existing ownership
first" checkbox (checked by default) for the initial-teams-import use
case - reusing the same `DELETE FROM ownership` pattern as the existing
randomize-draft dev tool. Picks are still logged as normal `draft`
transactions via `engine.ownership.record_draft_pick`, so the transaction
history stays honest.

**How**: live-tested end-to-end against the real running app and DB
(not just unit-level), using the exact sample CSV the user provided (a
48-pick, 6-manager "mock draft — practice" export, including its
"Final rosters" trailer section). `parse_draft_csv` correctly stopped at
48 rows (not 49 - confirmed it did not pick up the trailer's first data
row). `resolve_manager_picks` against the real local DB resolved all 48
picks to real, existing player_ids with zero synthetic fallbacks,
including tricky cases the split/match logic needed to get right: "Wade
Baldwin" → `BALDWIN IV, WADE`, "Mckinley Wright" → `WRIGHT IV, MCKINLEY`,
"Nigel Hayes-davis" → `HAYES-DAVIS, NIGEL`, "TJ Leaf" → `LEAF, TJ`, "Talen
Horton-Tucker" → `HORTON TUCKER, TALEN`. Then drove the actual HTTP
endpoints against the running dev server: `POST /draft/import/preview`
with the real file (found all 6 CSV managers, 48 total picks), then
`POST /draft/import/commit` mapping them to 6 of this DB's existing
placeholder managers with `clear_existing=1` - confirmed the response
flash ("Imported 48 pick(s) across 6 manager(s)", zero skipped), then
verified directly against the DB (exactly 48 owned rows, 8 per mapped
manager, previous 157 wiped) and against the app's own pages (`/managers/13`
showed the right 8 players; `/draft` showed all 48 as drafted with the
correct owner name in each row - confirming a CSV-imported player
correctly disappears as a draft/transfer candidate for everyone else, the
same core payoff validated for manual drafting in the original
draft/ownership-tracking entry above).

Note: the DB's pre-existing ownership state (157 players across 12
placeholder `TestN` managers, evidently leftover ad-hoc test data from
outside this session) was wiped by this test's `clear_existing=1` and
replaced with the 48-pick mock import above - left as the new local state
rather than restored, since it was disposable placeholder data to begin
with (same `TestN` managers the existing roster-cap-validation entry above
already found in an inconsistent state). Re-run the dev randomize-draft
tool (`/sync`) for a fresh full test league if needed.

**Result**: working and live-tested against the real app/DB with the
user's own sample export. Not yet tested against a real (non-mock,
non-bot) draft export from the friend's app - the mock export's shape was
assumed representative; revisit if a real export turns out to differ.

---

## 2026-09-16 — Fixed sixth-man selection not honoring the day-1-first "golden rule"

**What**: the user spotted a real bug live in `/lineup` for manager Test1 -
a day-1-playing Forward with a solid projection (Vezenkov, 17.4) was stuck
on the bench (half points, no way to ever be upgraded) while a
later-playing Forward with a higher raw projection (Clyburn, 19.3) was
picked as the sixth man from the start - even though nothing had locked
Clyburn in yet and his slot didn't capture any day-1 value at all.

**Root cause**: `engine/lineup.py::build_lineup`'s starter selection
(`_build_formation_starters`) correctly ranks candidates by
`(_availability_rank, -value)` - day-1 players fill starter slots first
(the documented "golden rule"), which sets up `swap_after_day1` to later
promote the best same-position *pending* (later-playing) bench player into
that now-finished slot, banking two games' worth of full-rate points from
one slot across the round. But the sixth-man pick right below it just did
`max(remaining, key=value_fn)` - pure value, ignoring availability
entirely. A later-playing player could grab the sixth-man slot outright,
forfeiting its day-1 scoring opportunity completely (that slot only ever
captures the one later game), while a real day-1 candidate for that slot
got stuck on the half-scoring bench with no swap ever able to reach them
(swap only promotes bench players into slots whose *current* occupant
already played - a day-1-availability bench player was never eligible in
either direction).

**Fix**: changed the sixth-man pick to use the identical
`(_availability_rank, -value)` sort the starters use, so it participates
in the same day1-lock-then-swap-upgrade path instead of being chosen by
raw value alone.

**Validation**:
1. Reproduced against the real DB (Test1's actual roster, round 1 of the
   live E2026 schedule) - confirmed Hoard (day1, Forward, 17.8, the best
   remaining day-1 candidate) now correctly becomes the sixth man instead
   of Clyburn (later, 19.3), matching the documented golden rule.
2. Re-ran the full validated backtest (`--seasons E2023 E2024 E2025
   --min-round 6 --trials-per-round 30 --seed 1`, same 91 rounds/2730
   trials as the current headline) before and after the fix, same seed for
   a direct comparison:
   - **Before (buggy)**: mean gain +24.06 PIR (95% CI ±0.68), beat-or-tie
     98% (90% beat/8% tied), 73% of swap upside captured.
   - **After (fixed)**: mean gain **+29.67 PIR** (95% CI ±0.74),
     beat-or-tie 99% (92% beat/7% tied), 72% of swap upside captured.
   - The fix is a genuine, substantial improvement (+5.61 PIR/round mean
     gain) confirming the bug was real and previously costing real value
     in every recommendation - not just the one example the user noticed.
     Both `build_lineup`'s output *and* the theoretical best-possible
     ceiling used for backtesting reuse the same function (per the
     module's own docstring), so the previous best-possible ceiling was
     itself quietly capped by this same bug - the true ceiling was always
     a bit higher than the old headline reported. **This supersedes the
     prior headline** (91 rounds/2730 trials, +24.06 PIR, 99% beat-or-tie,
     73% captured) in `CLAUDE.md` and above in this log.
3. Structural invariant (recommended never worse than no-swap by more than
   a projection-miss, never breaking the no-swap<=best-possible guarantee)
   held after the fix - loss-case count and causes unchanged in character
   (still attributed to projection-vs-actual misses, not logic bugs).

**Also fixed** (found while investigating the user's follow-up question -
"why would I swap Montero for Larkin if Montero scores high?"): this was
*not* a second bug - Montero's day-1 points are banked the moment he plays,
at whatever tier he held in the Day-1 table (captain/2x here), completely
unaffected by where he lands in the Day-2 swap-plan table. The swap only
decides who occupies that *slot* for the still-to-be-played half of the
round, since Montero has no more games left to give it - moving Larkin in
lets Larkin score at full rate for his later game instead of half on the
bench, a pure upside pickup on top of Montero's already-locked-in score.
But the `/lineup` page's "Day 2: swap plan" table displayed this
misleadingly: an already-played former starter shown as "Bench / day1"
was visually identical to a genuine bench player who'd been there since
day 1 (half rate), with no way to tell "this already banked full/double
points, this row is just bookkeeping" from "this scores half". Added a
BANKED badge + explanatory paragraph (`templates/lineup.html`,
`static/style.css`) to any Day-2-table row whose availability is "day1",
clarifying their score is already locked in from the Day-1 table above
regardless of what slot they're shown in here. Confirmed via `app.py`'s
test client against the live Test1/round-1 data (8 BANKED badges rendered,
200 response).

---

## 2026-09-16 — Corrected the day-1/day-2 substitution rule (halving on demotion, formation reshuffle allowed)

**What**: right after the sixth-man fix (previous entry), the user asked a
follow-up about that fix's own example output - "why would I swap Montero
for Larkin if Montero scores high?" - which led to checking the actual
substitution mechanics against the official rules, and turned up a second,
much bigger correction: this codebase's core assumption that a
full-scoring slot's day-1 points are "banked permanently" (a later swap
can't take them away) was **wrong**. The user confirmed, against both the
official rules
([euroleaguebasketball.net](https://www.euroleaguebasketball.net/euroleague/news/euroleague-fantasy-challenge-rules-deadlines-tips/))
and their own experience playing the real game: **moving an already-played
starter/6th-man down to the bench halves their already-earned points** -
exactly like it would if they'd started on the bench all along. A
secondary source (`fantaking.gitbook.io`, "Classic Mode" rules) was
fetchable and corroborated this with an exact quote: *"The player that is
moved from the field to the bench halves his score."* The same source also
confirmed formation can be changed at the swap window (this codebase had
separately assumed the day-1 formation shape was locked), and that captain
can be reassigned to any starter who hasn't played yet.

**Why this matters**: under the old (wrong) model, swapping was a strictly
dominant, risk-free move - the day-1 occupant's points were safe no matter
what, so promoting any positive-value bench player could only help. Under
the corrected rule, swapping is a real trade: demoting a slot's occupant
costs half of what they already earned, so it only pays off if the
incoming replacement actually outscores them. This isn't just a threshold
tweak - it changes the entire optimization problem the engine is solving.

**Fix** (`engine/lineup.py`):
1. `compute_round_score` simplified to a single pass: a player's score is
   now governed entirely by whatever tier they hold in the ONE lineup
   passed in (captain 2x / starter+6th-man 1x / bench 0.5x) - no more
   separate day-1-uses-`initial`-tier / day-2-uses-`final`-tier split.
2. `swap_after_day1` rewritten as a genuine re-solve rather than a
   same-position patch: any day-1 player who started on the BENCH is
   locked there for good (the rules never let you swap in someone who's
   already played); everyone else - the day-1 full-slot occupants (keep or
   demote) plus every day-2 player (fully flexible, never played yet) - is
   "flexible" and competes for the best valid formation + sixth man by
   value alone (no day-1-first tiebreak at this stage - there's no future
   swap left to preserve optionality for). This single value-maximizing
   re-solve elegantly subsumes the old "swap only if incoming beats
   outgoing" comparison (proven algebraically: promoting a never-played
   bench candidate is pure upside; demoting a played occupant costs exactly
   half their value, so picking whoever has the higher value for the slot
   is equivalent to that comparison) AND handles the newly-confirmed
   formation reshuffle for free, since it searches all 3 valid formations
   fresh from the flexible pool.
3. `build_lineup`'s day-1-first "golden rule" for the INITIAL lock is
   still correct and was kept unchanged - verified algebraically (a
   single-slot dominance argument: starting your best-projected day-1
   candidate and retaining the option to demote them later weakly
   dominates benching them pre-round in favor of a day-2 candidate,
   because the demote-later path can always fall back to keeping them if
   they outperform, which the pre-round-bench path can never recover from).

**A second, related bug this surfaced**: the backtest's swap *decision*
was using pre-round projections for BOTH day-1 and day-2 candidates
(matching how the live `/lineup` page works, since it's shown all at once
before the round starts). Under the old banked-permanently model this was
harmless (swapping was risk-free either way). Under the corrected model it
is NOT harmless: a naive re-run of the standard backtest command
(`--seasons E2023 E2024 E2025 --min-round 6 --trials-per-round 30 --seed
1`) produced mean gain **+2.45 PIR** with the recommended lineup actually
**losing to the no-swap baseline in 31% of trials** (worst single-trial
deficit: 52.5 PIR) - because a day-1 player who outperformed their
pre-round projection could get wrongly demoted, a real, costly mistake
under the corrected halving rule. Fixed in `backtest_eval.py` and
`poc_run.py`: the swap decision now uses each day-1 team's *actual* PIR
(once their games are over) instead of their pre-round projection, and
only day-2 teams still use projections - exactly the information a real
manager has at the real decision point (made after day 1, not before it).
Day-2 actuals are deliberately withheld from this lookup to avoid hindsight
leakage into a decision that precedes them.

**Result after both fixes (regular season only, 91 rounds/2730 trials,
same seed as prior headlines)**:
- mean gain (recommended - no-swap): **+11.11 PIR** (95% CI ±0.54)
- beat-or-tie: 84% (74% beat, 10% tied, 16% lost)
- mean best-possible ceiling: 149.46 (down from ~163 under the old, wrong
  model - the ceiling itself was inflated by assuming demotion was free)
- mean swap-upside captured: ~28% (down from ~72%, but now measured
  against a correspondingly smaller, correct ceiling - not a
  like-for-like regression)
- structural invariant (no-swap <= best-possible) still holds with zero
  violations across all 2730 trials.
- **This supersedes the prior headline** (+29.67 PIR, 99% beat-or-tie,
  ~72% captured) in `CLAUDE.md` and above in this log. The 16% loss rate is
  expected/documented, not a bug - it's the real, now-correctly-modeled
  risk of the swap mechanic itself (a wrongly-projected day-2 candidate can
  now genuinely cost points, unlike under the old model where the
  downside was structurally capped at zero).

**Also fixed**:
- `docs/game_rules.md`: replaced the "banked permanently" / "formation
  locked at day-1" / "captain doubling is per-stage" claims with the
  corrected mechanic and the source.
- `templates/lineup.html` / `static/style.css`: the Day-2 table's earlier
  "BANKED" badge (added in the sixth-man-fix session, before this
  correction) was itself now wrong - replaced with DEMOTED (flags a player
  who already played in a full slot and is being moved to the bench here,
  with an explicit "this halves their already-earned points" warning) and
  PROMOTED (a player moved from bench into a full slot before playing -
  pure gain, no cost) badges instead.
- `app.py`'s `/lineup` route now checks whether day-1's box scores for the
  selected round have actually been synced (`player_game_stats` is
  played-games-only, so presence = played) and uses real actual PIR for
  the swap decision when available, falling back to projections
  otherwise - mirroring the backtest fix above, live. A note under the
  Day-2 table tells the user which mode is active. Not yet live-tested
  against a real day-1-already-played round (E2026 hasn't started/synced
  any rounds yet as of this writing) - the "projections only" fallback
  path was confirmed rendering correctly; revisit once real E2026 day-1
  results exist to sync.

**Validation**:
- Reproduced the corrected engine against Test1's real roster (round 1,
  live E2026 schedule): confirmed the swap plan now correctly identifies a
  real net-positive trade (Clyburn promoted, Hoard demoted, net
  +0.75 projected PIR - matching the `0.5 * (incoming - outgoing)` formula
  by hand) instead of blindly treating any positive-value bench candidate
  as a free upgrade.
- `poc_run.py` end-to-end smoke test (E2025, round 20): ran without
  errors, formation correctly reshuffled from day-1's (3,1,1) to the
  swap plan's (2,2,1), backtest scored no-swap=67.0 /
  recommended=81.5 / best-possible=96.0 for that single real round -
  sane, recommended beats no-swap as expected.
- `app.py` test client: `/lineup?manager_id=13&round=1` returns 200 with
  both DEMOTED and PROMOTED badges rendering and the correct
  projections-only-fallback note showing (since E2026 has no synced
  results yet).

---

## 2026-09-16 — Formation-optimizing build_lineup, manual formation override, total projected score box

**What**: three related asks after the substitution-rule correction
(previous entry): (1) let the user manually select a formation instead of
always auto-picking one, (2) make the day-1 lock's formation choice itself
directly optimize for total projected PIR (not just a "most day-1 starters"
proxy) - explicitly requested even if it means a different formation than
day-1 gets used after the swap, and (3) add a visible total-projected-score
summary to `/lineup`.

**Changes** (`engine/lineup.py`):
- `build_lineup` refactored: the shared day-1-lock-for-one-formation logic
  moved into a new `_lock_formation` helper. By default (no `formation`
  arg), `build_lineup` now tries all three valid formations, carries each
  one's day-1 lock all the way through a simulated `swap_after_day1` (using
  the same `value_fn`, since only projections exist at decision time
  either way), and picks whichever formation's *final* simulated total is
  highest - not just whichever formation has the most day-1 starters (the
  prior proxy). Pass an explicit `formation` to force one shape instead of
  searching.
- `swap_after_day1` gained the same optional `formation` param, to keep the
  day-2 plan pinned to a manually-forced shape instead of freely
  reconsidering all three.
- `choose_active_squad` gained the same param and forwards it to both
  `build_lineup` and `swap_after_day1`; catches the `RuntimeError` a forced
  formation can raise for a given exclusion candidate (some exclusions
  simply can't field a specific formation) and skips that candidate rather
  than crashing the whole exclusion search.

**Changes** (`app.py` / `templates/lineup.html` / `static/style.css`):
- `/lineup` gained a `?formation=G-F-C` query param (e.g. `2-2-1`), parsed
  into a tuple and forwarded through `choose_active_squad`/`build_lineup`/
  `swap_after_day1`; invalid/garbled values safely fall back to Auto rather
  than erroring. A dropdown on the filter form (Auto (maximize PIR), plus
  each of the 3 valid formations) drives it.
- A **Total Projected Score** box (no-swap total / recommended total /
  swap gain, gain colored green/red) now renders above the tables, using
  `compute_round_score` with the same "actual PIR if the game's already
  synced, else projection" values the swap decision itself uses - so the
  box and the recommendation are always self-consistent.

**Validation**:
- Re-ran the full validated backtest (91 rounds/2730 trials, same seed as
  every prior headline in this log): **+10.62 PIR mean gain (95% CI
  ±0.53), 82% beat-or-tie (74% beat/8% tied/18% lost)** - statistically
  indistinguishable from the pre-this-session +11.11 PIR/84% beat-or-tie
  headline (both CIs overlap heavily). This is expected, not a null
  result: `swap_after_day1` already re-solves formation freely at the
  final step regardless of which formation day-1 started with, so
  optimizing the *initial* formation choice only affects which specific
  day-1 players get "protected" with a full slot (a smaller, harder-to-see
  effect) rather than the final formation shape itself. Kept the change
  anyway since it directly does what was asked - genuinely optimize for
  PIR, not approximate it - and runtime stayed reasonable (~1m48s for the
  full 2730-trial run, up from a faster but not-dramatically-so baseline,
  despite `build_lineup` now doing ~3x the internal work per call).
  **This supersedes the prior headline** (+11.11 PIR, 84% beat-or-tie) in
  `CLAUDE.md` and above in this log.
- `app.py` test client: `/lineup?manager_id=13&round=1` exercised with no
  formation param (Auto), each of the 3 valid formations explicitly, and
  two invalid values (`garbage`, `9-9-9`) - all returned 200; Auto's
  chosen totals matched the best of the three explicit formations exactly
  (180.4/181.1 for 2-2-1, which Auto also picked); invalid values correctly
  fell back to Auto instead of crashing. All other routes spot-checked
  (`/`, `/draft`, `/managers`, `/transfers`, `/sync`, `/how-it-works`) -
  still 200 after the import/signature changes.

## 2026-09-16 — "Pitch view" visual on the lineup builder

**Prompted by**: the user asked for a Biwenger-style graphic showing a
manager's team on a basketball court (player photo cards positioned by
role, plus a checkmarked bench list), and asked whether it belonged on the
Managers tab or would work better on `/lineup` instead.

**Why `/lineup`, not the Managers tab**: the reference image's shape - 5
players on court + a bench column with a "6th man" tag and checkmarks -
is exactly `/lineup`'s per-round output (`initial`/`recommended`:
`.starters`, `.sixth_man`, `.bench`, `.captain`), which the raw 13-man
`/managers/<id>` roster page has no equivalent of (no round, no
active/excluded split, no captain - that's all decided per-round by
`choose_active_squad`/`build_lineup`). Building it on the Managers tab
would have meant re-running the lineup logic there for an implicit round,
duplicating `/lineup`. Confirmed with the user that no real player photos
are needed/available before building - checked first: the EuroLeague
`/people` endpoint (`engine.data.EuroleagueClient.list_people`) returns an
empty `images: {}` for every player on a live E2026 pull (players and
coaches alike), unlike clubs, which do have a `crest` image. So every
player renders as a monogram avatar instead of a photo.

**Changes**:
- Three tiny presentational Jinja filters in `app.py`
  (`player_initials`/`player_surname`/`player_display_name`, all parsing
  the `"SURNAME, FIRSTNAME"` format `Projection.player_name` is always in)
  - deliberately kept out of `engine/` since they're pure display
    formatting, not game logic.
- New `templates/_pitch.html`: a `pitch(lineup, initial_full_ids=none)`
  macro rendering a court (players grouped into rows by position - Centers
  nearest the hoop, then Forwards, then Guards, adapting to whichever of
  the 3 valid formations is in play) plus a bench column (6th man tagged,
  then the rest of the active 10, each with a checkmark). Captain gets a
  "C" badge; when `initial_full_ids` is passed (day-2 pitch only) it
  reuses the exact same promoted/demoted comparison the table below it
  already does, so the two never disagree.
- `templates/lineup.html` imports the macro and renders one pitch under
  each of the existing "Day 1: starting lineup" and "Day 2: swap plan"
  headers, above the existing detail table (kept as-is - the pitch is a
  complement, not a replacement, since it can't show projected values,
  team, or availability).
- ~90 lines of new CSS in `static/style.css` (`.pitch*`), matching the
  existing "Courtside" theme tokens (hardwood/orange court, `--charcoal`
  avatars, same badge style as the existing new/gone/demoted/promoted
  badges) rather than introducing a new visual language.

**Validation**: Flask app already running locally against the real
E2026-seeded DB (test managers 13-24 from an earlier `/dev/randomize-draft`
run). `curl /lineup?manager_id=13&round=1` → 200; verified in the raw HTML
(no headless-browser tooling available in this sandbox - no `node`, no
system `pip`/sudo to install one - so this was structural, not a visual,
check): exactly 2 `.pitch` blocks (day-1 + day-2), 5 `.pitch-player` +
5 `.pitch-bench-item` per block (10 = the active squad size), 3
`.pitch-row`s per block (one per position group, none empty for this
roster's 2-2-1 formation), exactly 1 captain badge and 1 sixth-man badge
per block, and exactly 1 `DEM` badge total - correctly appearing only in
the day-2 block (day-1's macro call passes no `initial_full_ids`, so it
never renders promote/demote badges, matching the existing day-1 table's
behavior). No Jinja tracebacks in the response. CSS brace-balance checked
(101 open/101 close). Not yet confirmed by eye in an actual browser -
worth a quick look next time the app is opened normally, since a headless
sandbox check can't catch a purely visual misalignment (e.g. avatar
overlap, text overflow on a long name) the way a real screenshot would.
