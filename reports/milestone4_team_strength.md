# Milestone 4 — team strength (adjusted ratings + preseason net-rating projection)

**Headline: the acceptance gate (spec §4.8) is not met, and the honest
fallback the spec itself prescribes ("ship shrunk-naive with
returning-minutes weighting") *also* loses to the naive "league average"
baseline.** The realized-season adjusted-ratings machinery (§4.1-4.2) is
solid, validated infrastructure. The forward-looking piece - projecting
*next* season's net rating from this season's roster/coach changes
(§4.3-4.4) - does not predict anything useful in this data, on either of
the two available season transitions, by any of the four methods tried.
This independently reconfirms, with a substantially more rigorous method,
the same conclusion `engine/team_strength.py` reached on 2026-09-14 with a
simpler heuristic (see `reports/data_audit.md` §1.3). Not wired in;
`config.yaml`'s `integration.use_new_features` stays `false`.

## What was built and validated

- **`proj/possessions.py`** (§4.1): `poss = FGA - OREB + TO + 0.44*FTA`,
  averaged across both sides of a game (spec's explicit instruction),
  points per 100 possessions, pace per 40 minutes normalized for
  overtime via each team-game's actual on-court minutes (not an assumed
  40). Sanity-checked: league-wide pace ~70-75 poss/40min,
  pts_per100 ~104-122 (25th-75th pctile) - both in a plausible EuroLeague
  range.
- **`proj/team_strength.py::fit_adjusted_ratings`** (§4.2): ridge
  regression of points-per-100 on offense-team dummy + defense
  (opponent)-team dummy + home indicator, one row per team-game-side, no
  dropped reference level - identifiable specifically *because* it's
  ridge (L2-regularized "adjusted plus-minus"), not OLS. Lambda chosen by
  leave-one-round-out CV over a 7-point grid; landed on an interior
  minimum (not a boundary artifact) on real E2024 data
  (best λ=10, CV MSE curve genuinely U-shaped: 155.4→153.4→159.2 across
  the grid). Bootstrap SEs (50-200 resamples, at the game level to
  preserve within-game correlation) run cleanly; team SEs ≈1.1-1.9
  points/100, a sane order of magnitude given ~30-42 games/team/season.
  E2024 (2024-25) team net ratings spanned roughly −7.5 to +6.7 - a tight
  spread consistent with EuroLeague's known competitive parity.
- **`proj/team_strength.py`** §4.3/§4.4 machinery: `returning_minutes_share`,
  `shrunk_value_above_position` (PIR/40 shrunk toward position average,
  k=600 minutes, "value" centered so an average player at his position
  scores ≈0 - verified in `tests/test_team_strength.py`),
  `team_projected_value`, and `fit_preseason_projection` (ridge,
  standardized inputs, leave-one-team-out CV, the spec's exact 4-feature
  set minus `net_last` for teams new to the league per §4.4).
- **`proj/eval_team_strength.py`** (§4.7): backtests both available
  transitions (E2023→E2024, E2024→E2025) against the spec's two named
  baselines (last-season net rating, league mean 0), plus RMSE and
  Spearman rank correlation.
- §4.5 (game-level margin model, `minutes_multiplier`) and §4.6 (matchup
  multipliers) are implemented as standalone functions
  (`expected_margin`, `fit_blowout_logistic`, `fit_minutes_multiplier`,
  `matchup_pir_allowed`) but **not evaluated end-to-end** - both are
  explicitly downstream consumers of `net_rating_proj`/`net_diff`, and
  building out a full evaluation on top of a core signal that itself
  doesn't clear its own acceptance bar would be exactly the kind of
  premature layering the project's established discipline avoids. Kept
  as reusable, tested-in-isolation building blocks (same posture as
  `engine/team_strength.py` after its own 2026-09-14 result), not wired
  into a pipeline.
- 2 new tests (`tests/test_team_strength.py`) - 15/15 total pass.

## A real bug found and fixed while building this

`returning_minutes_share`'s first draft was called on the *entire
league's* stint table, not the one team's - `sum()` over `prior_stints`
silently summed minutes across all ~300 league players instead of just
the ~13-15 on that team, so every team's "returning minutes share" came
back near **4%** instead of a realistic **~52-55%**. Caught by sanity-
checking the number directly (spot-checked Real Madrid's actual E2024→
E2025 roster overlap: 9 of 14 players carried over, ~64% by headcount -
nowhere near consistent with a 4% minutes share), not by the acceptance
metrics themselves, which would have quietly accepted the wrong number.
Fixed in `proj/eval_team_strength.py::build_transition_row` (filter
`stints[from_season]` to the team before calling
`returning_minutes_share`) and documented directly in the function's
docstring so a future caller can't repeat it silently. Added
`tests/test_team_strength.py::test_returning_minutes_share_uses_only_given_team_stints`
as a permanent regression guard. The acceptance-gate conclusion below is
**after** this fix, not before it.

## Results (net-rating projection, both transitions)

RMSE / Spearman rank correlation vs. actual next-season net rating:

| Transition | n teams | Predictor | RMSE | Spearman |
|---|---|---|---|---|
| E2023→E2024 | 17 | **ridge model** (no training data available - falls back to shrunk-naive) | 4.26 | −0.13 |
| E2023→E2024 | 17 | shrunk-naive (`net_last × returning_share`) | 4.37 | −0.11 |
| E2023→E2024 | 17 | raw last-season net | 5.21 | −0.07 |
| E2023→E2024 | 17 | **league mean (0)** | **3.81** | n/a |
| E2024→E2025 | 17-20 | **ridge model** (fitted on the one available transition) | 3.47 | **−0.35** |
| E2024→E2025 | 17 | shrunk-naive (`net_last × returning_share`) | 3.89 | +0.32 |
| E2024→E2025 | 17 | raw last-season net | 4.96 | +0.19 |
| E2024→E2025 | 17-20 | **league mean (0)** | **3.42** | n/a |

(`E2024→E2025`'s "ridge model" row includes 2-3 teams new to local data
in E2025, per spec §4.4's "every term except b1" handling - shrunk-naive/
raw-last-season rows above are computed only on the 17 teams present in
both seasons, for a clean apples-to-apples read on the season-to-season
signal itself.)

## Reading on this, honestly

- **Nothing beats the naive "predict league average" baseline on RMSE**,
  on either transition, by any of the four methods - not raw last-season
  carryover, not shrunk-naive with returning-minutes weighting (the
  spec's own §4.8 fallback), and not the fitted ridge model. This isn't
  a narrow miss: league-mean RMSE (3.42-3.81) beats every alternative
  (3.47-5.21) in both transitions.
- **The ridge model's rank correlation is actively negative on both
  transitions** (−0.13, −0.35) - worse than useless for ranking teams,
  while the simpler shrunk-naive approach at least gets a *positive*
  correlation on the more recent transition (+0.32). This mirrors
  milestone 3's finding almost exactly: a ridge-fitted correction, built
  on only one or two small transitions, can achieve a marginally lower
  RMSE while getting the *direction* of the relationship backwards - not
  a model worth trusting even where its raw error metric looks
  competitive.
- **This independently reconfirms the project's prior finding.**
  `engine/team_strength.py` (2026-09-14, a much simpler per-team
  PIR-allowed heuristic) found "no demonstrable predictive value" for
  team strength as a signal. This milestone built a substantially more
  rigorous system - real adjusted ratings via ridge-regularized
  plus-minus, roster-continuity-weighted projection, shrunk player
  values, coach-change tracking - and reached the same conclusion by a
  different, more careful route. Two independent methods failing the
  same way is stronger evidence than either alone that the underlying
  signal (season-to-season net-rating persistence) is genuinely weak in
  this league, not that either implementation was flawed.
- **A plausible real explanation, not just noise**: milestone 3 found
  real EuroLeague coach turnover of 44-50% year over year, and this
  milestone's own `returning_minutes_share` fix shows only ~52-55% roster
  continuity by minutes. A league with this much year-over-year
  personnel and coaching turnover *should* have weak net-rating
  persistence - the negative result is consistent with a real structural
  feature of this league, not obviously an artifact of small sample size
  alone (though the small sample - 17 team-season pairs per transition -
  certainly doesn't help either, and both plausibly matter).

## Recommendation

Do not wire this into `engine/`/`app.py`. If team strength is revisited
later, this milestone's evidence suggests the productive direction isn't
a better preseason projection model (§4.3/4.4 specifically looks like a
dead end here, on two independent attempts now) - it would more likely be
an **in-season** signal (the realized §4.2 adjusted ratings, updated as
the season progresses, are solid and could plausibly serve as an
opponent-strength input to `engine/lineup.py`'s existing captain/swap
tiebreaking - the exact open question `docs/technical_notes.md`'s "Team
strength index" entry already left on the table) rather than a
season-ahead forecast from roster/coach turnover, which this data
doesn't support.
