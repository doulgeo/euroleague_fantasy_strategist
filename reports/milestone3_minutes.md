# Milestone 3 — availability, minutes prior, ridge correction, team allocation

**Headline: the acceptance gate (spec §3.10) is not met.** Every layer
described in spec §3 is built and runs end-to-end on real data
(`proj/availability.py`, `proj/bio.py`, `proj/coaches.py`, `proj/minutes.py`,
`proj/allocation.py`, `proj/pipeline.py`), but the full pipeline's
per-player minutes predictions are **worse** than milestone 2's naive B0
baseline (last season's own rate), not better by the required ≥5%. Per the
spec's own ground rule ("If missed, report which segment failed and stop
tuning"), this report documents the honest result and a precise,
evidence-backed diagnosis of where it fails, and stops there rather than
continuing to iterate. `config.yaml`'s `integration.use_new_features`
stays `false` - nothing here is wired into `engine/` or `app.py`.

## What was built

- **`proj/bio.py`**: player birth dates (for age), sourced from the raw
  per-game stats cache (100% coverage for anyone with box-score history)
  plus one live `/people` call for true newcomers - resolves the "age"
  gap flagged in `reports/data_audit.md` §2. 823 players cached.
- **`proj/coaches.py`**: per-team-season head coach identity, sourced from
  the `coach` field already sitting in the raw stats cache (joined against
  `player_game_stats`' own team/home-away columns for club identity, since
  the stats payload's own `"team"` key turned out to be a stats-totals
  row, not club identity - a real early bug, fixed before this landed).
  Confirmed real coach turnover: 8/18 teams (44%) changed coach
  E2023→E2024, 10/20 (50%) E2024→E2025.
- **`proj/availability.py`**: injury-vs-rest run detection (runs of ≥3
  consecutive 0-minute team-games = injury-like, 1-2 = rest), return-game
  flagging, `p_active` via Bayesian shrinkage toward a position-group
  rate (k=15), and a fitted age slope (+0.012/year - see "surprising
  finding" below).
- **`proj/minutes.py`**: injury-down-weighted `mpg_active`, the
  multi-season `mpg_prior` formula, a 5-feature ridge correction
  (`mpg_prior`, `age_bucket`, `team_changed`, `start_share`,
  `coach_changed`) with leave-one-team-out CV over an 11-point alpha grid,
  newcomer fallback, and residual-band uncertainty.
- **`proj/allocation.py`**: the spec's `allocate_minutes` water-filling
  function (generalized to accept a per-position cap array, verified
  identical to the scalar version - see tests), the scenario allocator,
  2000-draw Monte Carlo expected minutes, position-group sanity bands,
  and coach-concentration tau-fitting (N_eff targeting).
- **`proj/pipeline.py` + `proj/eval_minutes_model.py`**: wires all of the
  above into one per-player prediction and evaluates it against the same
  2-fold / 6-segment framework as milestone 2, for a fair comparison.
- 8 new tests (`tests/test_allocation.py`, `tests/test_minutes_pipeline.py`)
  covering allocation invariants (sum-to-total, cap respect, infeasibility,
  monotonicity, position-group teammate effect), Monte Carlo determinism,
  and leakage on the new ridge-training-pair construction. 13/13 total
  pass.

## Results (minutes-weighted MAE / bias, weight = actual games active)

| Fold | Segment | n | B0 MAE | **pred MAE** | **pred bias** | exp_minutes MAE | exp_minutes bias |
|---|---|---|---|---|---|---|---|
| E2024 | all | 304 | 3.92 | **4.61** | −1.39 | 5.71 | −2.60 |
| E2024 | stayers | 138 | 3.69 | **3.71** | −0.07 | 4.71 | −1.31 |
| E2024 | team_changers | 52 | 4.57 | **4.42** | +0.76 | 5.07 | +0.30 |
| E2024 | newcomers | 109 | — | **6.92** | −6.33 | 8.68 | −8.21 |
| E2024 | starters | 102 | 3.37 | **5.13** | −3.74 | 6.07 | −3.97 |
| E2024 | bench | 101 | 4.69 | **3.35** | +2.50 | 3.25 | −0.25 |
| E2025 | all | 340 | 3.67 | **4.31** | −1.65 | 5.03 | −2.43 |
| E2025 | stayers | 140 | 3.13 | **3.10** | −0.17 | 3.51 | −0.74 |
| E2025 | team_changers | 71 | 4.93 | **3.84** | +0.29 | 4.28 | +0.71 |
| E2025 | newcomers | 123 | — | **7.03** | −6.02 | 8.61 | −8.19 |
| E2025 | starters | 114 | 3.25 | **4.93** | −4.70 | 5.71 | −4.97 |
| E2025 | bench | 113 | 4.43 | **4.25** | +3.88 | 3.42 | +0.96 |

("pred" = `mpg_active_pred`, the ridge-corrected prior before team
allocation; `exp_minutes` = after the full Monte Carlo allocation. Full
detail: `reports/minutes_model_eval_summary.csv`,
`proj/cache/minutes_model_eval_details.parquet`.)

## What actually works

- **`stayers`** (the largest, most representative segment): `pred` is
  essentially tied with B0 (3.71 vs 3.69, 3.10 vs 3.13) - the ridge
  correction doesn't hurt where there's nothing unusual to correct for,
  as expected.
- **`team_changers`**: `pred` genuinely **beats** every milestone-2
  baseline on both folds (4.42 vs 4.57 E2024; **3.84 vs 4.78-4.93** on
  E2025, a real ~20% MAE improvement where the ridge correction had a
  fitted transition to learn from). This is exactly the pattern spec
  §3.3's `team_changed` feature was meant to fix, and it's the one clear,
  reproducible win in this milestone.
- **Team-total sanity** (spec §3.9) actually holds at the aggregate
  level: summed `exp_minutes` per team clusters tightly around the
  ~200-206 target (median 201.3 across 20 E2025 teams) - the allocator
  is correctly constraining the *aggregate*. This is exactly why it can't
  fix per-player errors: forcing a team's total to the right number can't
  distinguish "this specific player is over/under-predicted" from
  "the team's total needs adjusting," so it just redistributes the
  existing per-player errors rather than reducing them - see the
  `no_mc_alloc` diagnostic column in the CSV output, nearly identical to
  `exp_minutes` (the Monte Carlo/p_active layer isn't the driver - the
  raw pre-allocation predictions are).

## Why the acceptance gate fails - two distinct, diagnosed causes

**1. The newcomer fallback is a genuinely weak predictor** (MAE 6.9-7.0,
worse than even milestone 2's B2 baseline's 4.8-5.1 on the same
population). Root cause, already flagged in `reports/data_audit.md` §2:
there is no depth-chart-role data anywhere in this project, so every
newcomer gets the same "position-group median × 0.7" fallback (spec
§3.4's own documented fallback path) regardless of whether they're
about to be a rotation cornerstone or the 13th man. This isn't a bug -
it's the honest consequence of a real, previously-flagged data gap, not
fixable without an external roster-depth input this project doesn't have.

**2. The 5-feature ridge correction overfits the single available
training transition, and it shows up concentrated in the `starters`
segment specifically** (bias −3.74 to −4.70, roughly 2-3x worse than
B0's own −1.46 to −1.66 starter bias - the correction is making the
*exact* pattern it was built to fix worse, not better). Concrete
evidence: the fitted `start_share` coefficient is **strongly negative**
(−2.99 to −3.60) - a player who already started a lot gets *penalized*
relative to `mpg_prior` alone, the opposite of the intuitive direction.
This is very likely a multicollinearity artifact (`start_share` and
`mpg_prior` are highly correlated - a heavy starter also has a high
`mpg_prior`), and with only ~192 training pairs from a *single*
season-to-season transition (E2023→E2024) to fit 5 correlated features
on, ridge's own regularization (even at the CV-selected alpha=100,
already toward the high end of an 11-point grid) isn't enough to prevent
this specific coefficient from picking up noise that then generalizes
poorly to E2025. This is exactly the risk the spec's own ground rules
flagged in advance ("only TWO season-to-season transitions... prefer
ridge/shrinkage and 2-4 parameters") - 5 features turned out to be one
transition's worth of data too many.

**3. (Secondary, smaller effect) Team allocation adds error on top of
both of the above**, rather than removing it - `exp_minutes` MAE is
consistently ~1.0-1.4 points worse than `mpg_active_pred` alone on every
segment. Diagnosed and ruled out as a Monte Carlo/p_active artifact
specifically (the deterministic `no_mc_alloc` variant - single
water-filling call, no availability sampling - lands within noise of
`exp_minutes`); the real driver is that `allocate_minutes` rescales an
entire team's roster by one shared factor, so it can't repair
segment-level bias, only redistribute it, and every team's rescale factor
is itself somewhat noisy (correlates −0.54 with `opening_day_roster`'s
inferred squad size, which is itself a 3-game, small-sample estimate per
spec §3.9's own documented inference method). A larger opening-day-games
window might reduce this specific noise source, but that would mean
deviating from the spec's explicitly-specified "first 3 games" - flagged
here rather than changed unilaterally.

## Surprising finding worth keeping in mind

The fitted **p_active age slope is slightly positive** (+0.012/year,
age_ref≈28.7): older players in this data have *marginally higher*
availability, not lower. Plausible explanation (not verified further,
per "stop tuning"): survivorship - only players good enough to still be
rostered at older ages in EuroLeague tend to be reliable veterans, while
younger players see more rest/rotation-driven absences. Kept as-fit since
it's a real, data-driven result, not overridden with a "should be
negative" assumption.

## Deliverable

`proj/cache/minutes_proj.parquet` (340 rows, E2025 fold): `player_id,
team, mpg_active_pred, p_active, exp_minutes, min_p10, min_p90,
role_class`, per spec §3's output header. **Caveat**: `role_class` in
this specific file is the *actual-outcome* tier (used for the evaluation
above), not the model's own predicted tier - `proj/pipeline.py`'s
`build_minutes_projection` computes a genuine predicted `role_class`
internally (from `mpg_active_pred`, not hindsight), but
`proj/eval_minutes_model.py`'s merged output for this report reused the
evaluation frame's actual-tier column for convenience. Worth fixing
before this file is used for anything beyond this report.

## Recommendation

Do not wire this into `engine/`/`app.py` (`config.yaml`'s
`integration.use_new_features` stays `false`) - consistent with this
project's established discipline (the ML regression pivot and the
original `team_strength.py` were both shipped-if-it-wins, and both
didn't, so neither is wired in either). If this is revisited:
- The `team_changers` win is real and worth keeping - it's isolated to
  the ridge correction's `team_changed` feature specifically, not
  entangled with the other problems.
- Dropping `start_share` from the ridge feature set (or fitting it with
  a monotonicity constraint) is the most targeted next experiment for
  the `starters`-segment regression, since it's the one coefficient with
  a sign that doesn't survive a sanity check.
- The newcomer gap has no data-driven fix available in this project
  today - it would need an external depth-chart input, not a modeling
  change.
- The allocation step should probably not run at all until the
  per-player predictions it's built on are actually accurate - it's a
  variance-preserving redistribution, not an error-correction mechanism,
  and current results show it faithfully doing exactly that (correct
  team totals, unchanged or worse individual accuracy).
