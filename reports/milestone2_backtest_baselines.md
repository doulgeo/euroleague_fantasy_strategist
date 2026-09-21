# Milestone 2 checkpoint — minutes-projection backtest harness + baselines

`proj/data.py` (loading + per-season player-stint tables) and
`proj/backtest.py` (fold definitions, three baselines, minutes-weighted
metrics, segments) are built and run against real data. No modeling yet —
per the spec's own ordering ("before any tuning on results"), this is
baselines only. Run with `python -m proj.run`; full per-player output
cached at `proj/cache/backtest_minutes_baselines_details.parquet`, summary
at `reports/backtest_minutes_baselines_summary.csv`.

## Folds

Only 2 possible, per the sample-size note in `reports/data_audit.md` §3:

| Fold | Train | Test |
|---|---|---|
| 1 | E2023 | E2024 |
| 2 | E2023, E2024 | E2025 |

Evaluated against test-season players with ≥1 active game (a player who
never plays has no meaningful "minutes when active" target — predicting
*whether* a rostered player plays at all is the availability/p_active
model's job, milestone 3, not this baseline's).

**Opening-day rosters**: `proj/data.py::opening_day_roster` infers a
season's roster from each team's first 3 played games, since no historical
roster snapshot exists for E2023–E2025 (per spec §3.9's own documented
fallback — see `reports/data_audit.md` §2, the `rosters` table only ever
holds current-season live state). Not yet wired into the evaluation loop
above (which currently scores against *all* test-season active players,
not just the opening-day subset) — will matter once §3.5 allocation needs
a specific team roster to allocate minutes across; noted so it isn't
mistaken for an oversight.

## Baselines

- **B0** = last train season's `mpg_active` for that player, no adjustment.
- **B1** = spec §3.3's `mpg_prior` formula *without* the ridge correction
  or team allocation that come later: weighted across up to 3 seasons
  (weights 0.6/0.3/0.1, most-recent-first), each season shrunk by
  `r_s = games_active_s / (games_active_s + 8)`.
- **B2** = role-class mean. "Role class" here is an **mpg_active tertile
  within position group**, pooled over train seasons — not a roster-
  assigned depth-chart role, since no depth-chart data exists for
  E2023–E2025 (`reports/data_audit.md` §2). A player's tier is looked up
  from their own most-recent train-season rate; a true newcomer (no train
  history) falls back to the position group's overall mean across all
  tiers.

Hyperparameters used (all from `config.yaml`, none tuned on this result
yet): `season_weights=(0.6, 0.3, 0.1)`, `shrink_games=8` — both taken
directly from the spec's own §3.3 formula, not fit.

## Results (minutes-weighted MAE / bias, weight = actual games active)

| Fold test | Segment | n | B0 MAE | B0 bias | B0 cov | B1 MAE | B1 bias | B1 cov | B2 MAE | B2 bias | B2 cov |
|---|---|---|---|---|---|---|---|---|---|---|---|
| E2024 | all | 302 | 3.94 | +0.09 | 64% | 3.94 | +0.10 | 64% | 4.45 | −0.38 | 100% |
| E2024 | stayers | 137 | 3.71 | −0.14 | 100% | 3.71 | −0.14 | 99% | 4.08 | −0.50 | 100% |
| E2024 | team_changers | 51 | 4.57 | +0.78 | 100% | 4.57 | +0.78 | 100% | 4.55 | +0.12 | 100% |
| E2024 | newcomers | 109 | — | — | 0% | — | — | 0% | 5.12 | −0.49 | 100% |
| E2024 | starters (actual) | 101 | 3.37 | −1.66 | 80% | 3.37 | −1.66 | 80% | 4.42 | −3.42 | 100% |
| E2024 | bench (actual) | 101 | 4.69 | +3.32 | 45% | 4.69 | +3.34 | 44% | 5.41 | +4.82 | 100% |
| E2025 | all | 340 | 3.67 | +0.35 | 64% | 3.71 | +0.35 | 64% | 4.08 | −0.28 | 100% |
| E2025 | stayers | 140 | 3.13 | −0.02 | 100% | 3.26 | +0.05 | 99% | 3.33 | −0.29 | 100% |
| E2025 | team_changers | 71 | 4.93 | +1.24 | 100% | 4.78 | +1.14 | 100% | 4.80 | +0.44 | 100% |
| E2025 | newcomers | 123 | — | — | 0% | — | — | 0% | 4.83 | −0.74 | 100% |
| E2025 | starters (actual) | 114 | 3.25 | −1.46 | 74% | 3.29 | −1.69 | 74% | 4.19 | −3.25 | 100% |
| E2025 | bench (actual) | 113 | 4.43 | +2.60 | 49% | 4.65 | +3.05 | 48% | 5.47 | +4.45 | 100% |

("starters"/"bench" here = actual-outcome mpg tertile in the *test* season
itself, computed post-hoc for segmentation only — not a predictive input,
so no leakage.)

## Reading on this, honestly

- **B0 and B1 are nearly identical** — expected, and a useful sanity
  check on the formula: with only 1 train season (fold 1), the weighted
  formula's numerator/denominator reduce algebraically to exactly B0
  (single term, weight and shrinkage cancel). Fold 2 (2 train seasons)
  shows the first real daylight between them (3.67 vs 3.71 MAE overall) —
  small, as expected with only a second data point added.
- **B2 (role-class mean) is worse than B0/B1 on every segment except
  newcomers** (where it's the only baseline that even has a prediction).
  Not surprising — collapsing a player to a position+tier mean throws
  away individual signal a simple "last season's own rate" baseline
  keeps. This sets a real, not-strawman bar for the ridge-corrected model
  in milestone 3 to clear.
- **The starters/bench split is the most informative result here**: B0/B1
  systematically *under*-predict players who turn out to be real-season
  starters (bias ≈ −1.5 to −1.7 min) and *over*-predict players who turn
  out to be bench (bias ≈ +2.6 to +3.3 min) — a textbook regression-to-
  role-change pattern (breakout players earning more minutes than their
  prior rate suggested; aging/injured/demoted players earning less).
  This is exactly what spec §3.3's `team_changed`/`start_share`/age
  features in the ridge correction are meant to fix — milestone 3 should
  be judged against closing this specific bias, not just overall MAE.
- **Coverage gap**: B0/B1 have zero coverage on newcomers (109 and 123
  players per fold — a third of each test season) and well under 100% on
  the starters/bench segments (players who are newcomers *and* end up a
  starter or bench outcome). B2's 100% coverage is only because it has an
  unconditional fallback, not because it's actually informed for those
  players — flagging so a naive "B2 has better coverage" reading doesn't
  get mistaken for "B2 is a better model."
- **Team-total sanity** (spec §3.9): not meaningful at this stage and
  deliberately not computed — none of B0/B1/B2 model roster allocation at
  all (each predicts a player's own rate in isolation), so summing them
  per team has no reason to approach ~200 minutes. This check becomes
  real once §3.5's allocation exists (milestone 3).

## Acceptance gate reminder (spec §3.10)

Not evaluated yet — that's a comparison of the *real model* (milestone 3)
against these baselines, specifically B0. Recorded here for what "beat by
≥5% MAE" will be measured against: B0 overall MAE is 3.94 (E2024 fold) and
3.67 (E2025 fold).
