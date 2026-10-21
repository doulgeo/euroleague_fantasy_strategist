# Projection overhaul plan

**Status (reconciled 2026-10-21):** Phases 0, 1, 2 (except P8), and 3
are implemented. Phase 5a is partly implemented (B1/B4/B5 done; B2 open).
Phase 8's sequential swap execution is implemented, with planning and
tracker-validation follow-ups below. Phases 4, 5b-d, 6, 7 and 9 remain
open. All seven user questions are answered. The implementation notes and
current backlog below supersede older pending/blocker statements in this
document; the original design and historical findings are preserved.

## Current backlog (2026-10-21)

| Phase | Remaining work | Status / dependency |
|---|---|---|
| 2, P8 | Extra shrinkage for team-changers; measure remaining segment bias | Open; other Phase 2 items, including P7, are implemented |
| 5a, B2 | Multi-season backtest history, early rounds, parity with live availability/expected-value decisions | Open; complete before relying on realized-points acceptance checks |
| 4, W1/W2/W3/X4 | Win probability, home/away context, new-team bonus, optional win/PIR correlation | Next major modeling phase; use backtest parity work to validate |
| 5b-d, B3 | Paired realized-points comparisons, renewed ML evaluation, full headline rerun | Open; compare final behavior after Phase 4; existing headline remains stale |
| 6, U1 | Uncertainty-aware exclusion, lineup and captain decisions | Open; availability is implemented and the no-show question is answered |
| 7, T1 | Historical injury/roster snapshots; frozen availability/status in tracker snapshots | Open; Phase 3 dependency satisfied |
| 8 follow-ups | Evaluate initial formation/exclusions across every window; validate tracker intermediate windows; review multi-date hindsight ceiling | Sequential execution is done (R1); initial search simulates one window and tracker checks against day 1 only |
| 3 display follow-up | Show p_play in tracker tables | Open; snapshots retain expected value only; detailed snapshot work belongs to T1 |
| 9, X1/X2/X3 | Transfer horizon, fatigue investigation, conditional cache robustness work | Deferred until Phase 5 enables measurement; X3 remains unreproduced |
| 10 | Keep docs, issue registry and validation log current | Continuous |

Phase 3's realized-points non-regression check is deferred until backtest
parity/comparison work is complete. Injury multipliers remain user-approved
assumptions, not fitted historical estimates. The optional risk-aware
captain toggle remains deferred until an outcome model and backtest
evidence support it.

**Source:** a three-agent review of the PIR projection mechanism, run
2026-10-15. The agents were a master statistician, a backtest analyst, and a
code and leakage auditor. Their raw findings, with numbers, are preserved in
the Appendix. Their scratch scripts lived in a session-only scratchpad and
will not survive. The appendix is the durable record; Phase 0 ports the
useful parts into the repo.

**How to use this doc:**
- Work the phases in order. Each phase lists its dependencies.
- Each phase is one or more commits, per the working agreement in `CLAUDE.md`.
- Each phase gets its own `docs/testing_log.md` entry.
- Tick the checkbox in the Issue registry when an item lands. Note in the
  registry anything that turns out differently from this plan, rather than
  silently changing the plan text.

---

## Issue registry

Every finding from the review, with the phase that addresses it.
Severity and finding descriptions are the reviewers' historical assessment;
checkboxes track implementation, not every acceptance check. R1's completed
execution still has the follow-ups in the current backlog.

| ID | Sev | Finding (short) | Phase | Done |
|---|---|---|---|---|
| P1 | High | Projections assume the player plays. ~21.5% of projected player-rounds score 0, giving about +1.0 PIR per game of overshoot | 3 | [x] |
| P2 | High | Over-dispersed: calibration slope 0.76, top decile +2.55 PIR. No shrinkage | 2 | [x] |
| P3 | High | Estimator mid-pack: the season-to-date mean beats it. A 10-game linear ramp window is too short | 2 | [x] |
| P4 | High | The 3-game minimum zeroes players. 19% of E2026 R1–5 played-games were projected 0; those players actually averaged 7.05 | 2 | [x] |
| P5 | Med | Known gap: players with only E2023/E2024 history, or 1–2 games in the window, get 0.0 with no flag. 28 players at round 6 | 2 | [x] |
| P6 | Low | Manual overrides get no win bonus (`rosters.py:106`), so they run about 5% low vs everyone else | 2 | [x] |
| P7 | Low | A manual override can't lift a *known* player who has only 1–2 games | 2 | [x] |
| P8 | Low–Med | Team-changers: projection R² −0.04, slope 0.47 (noisier, though not biased on average) | 2 | [ ] |
| P9 | Low | The early-season blend window includes last season's playoff games | 2 | [x] |
| L1 | High | Postponed games leak future data. The cutoff is by round number, not date (E2025 game 134: round 14, played 2026-03-17). Affects the backtest and, latently, the live app | 1 | [x] |
| L2 | Low | Team win-rate window orders by `game_code`, not date (`projections.py:192`) | 1 | [x] |
| L3 | Low | Rescheduled games distort day-1/day-2 team assignment (`lineup.py:98-136`); E2025 R34 and R14 are examples | 1 | [x] |
| W1 | Med | Win-probability stand-in (team last-10 win rate) has Brier 0.265, worse than a flat 0.5 (0.250). Home/away alone gets 0.236; logistic on home + form gets 0.219 | 4 | [ ] |
| W2 | Low | Home/away PIR effect (+0.38 / −0.34) is ignored | 4 | [ ] |
| W3 | Low | A transferred player keeps the old team's win rate and bonus (`rosters.py:88-89`). 68 players at R1 | 4 | [ ] |
| B1 | Med | Backtest values day-1 no-shows at their projection; live values them at 0 (`backtest_eval.py:127-129` vs `app.py:1086-1091`) | 5 | [x] |
| B2 | Med | Backtest never runs the live path: single season, `min_round=6`, no blend, no early rounds | 5 | [ ] |
| B3 | Med | Backtest metric (swap gain over no-swap with the *same* projections) can't see projection quality. The ML rejection may be unsound (Ridge R² 0.238 vs heuristic 0.181) | 5 | [ ] |
| B4 | Low | Backtest `actual_pir_lookup` keeps only the last row per player per round; the tracker sums. Latent | 5 | [x] |
| B5 | Low | Backtest `classify_phase` treats rescheduled rounds as regular season | 1/5 | [x] |
| U1 | Med | Decisions ignore uncertainty, so the exclusion search's claimed swap "option value" is ~0 with fixed values | 6 | [ ] |
| T1 | Med | Tracker re-renders and snapshots past rounds with *today's* injury report and today's rosters | 7 | [ ] |
| R1 | ? | 3-date rounds (E2026 R4, 7, 10, 11, 21, 28, 35): only the earliest date is treated as known. Wrong if there is a second swap window | 8 | [x] |
| X1 | Low | `projected_gain` is a per-game delta with no rest-of-season or schedule horizon (`transfers.py:28-30`) | 9 | [ ] |
| X2 | Low | Double-round fatigue (392 team-games on 2 days' rest) is untested | 9 | [ ] |
| X3 | Low | The projection cache is keyed on DB-file mtime and could serve stale data under WAL or coarse `/mnt/c` mtime. Not reproduced | 9 | [ ] |
| X4 | Low | Win-bonus correlation: PIR runs +0.98 in wins and −0.94 in losses, so `win_rate × PIR` slightly understates EV (~0.1 PIR) | 4 | [ ] |

---

## Open questions for the user

These must be answered before the phase that depends on them.

1. **No-shows in the real game (blocks Phase 3 and Phase 6).** If a
   starter or 6th man doesn't play, does the real game auto-substitute from
   the bench, or does the slot just score 0? `docs/game_rules.md` doesn't
   say. Under auto-sub, a no-show costs much less, and the availability
   model has to feed the lineup logic differently.
2. **3-date rounds (blocks Phase 8).** When a round spans 3 dates, is there
   one swap window (after day 1) or one after each date? Can you change
   the lineup after the second date's games?
3. **Postponed games (blocks part of Phase 1).** If a game is rescheduled
   months later, how does the fantasy game count it? It could be scored in
   its nominal round (impossible in real time), in the round whose window
   it falls into, or not at all.
4. **Injury-status multipliers (Phase 3).** There's no historical injury
   data to fit them from. Proposed defaults: Out 0, Doubtful 0.4,
   Questionable/Uncertain/Game-time 0.75, Expected/Ready 0.95. OK, or
   different numbers?
5. **Manual override precedence (Phase 2, P7).** Proposal: an explicit
   manual override always wins over the model, for any player (still
   flagged EST), instead of only filling in for players with no
   projection. OK?
6. **Variance preference for captain (Phase 6, optional).** Head-to-head
   scoring makes variance strategic: an underdog should prefer
   higher-variance picks. Want an "aggressive/safe" toggle, or keep a pure
   expected-value maximizer?
7. **Display (Phase 3).** Should the draft board, transfers and lineup show
   the expected value (×p_play) as the main number, with "if he plays" PIR
   secondary, or the other way round?

---

### Answers (2026-10-15)

1. **No-shows:** NOT substituted - the slot scores 0. (Phase 3 and 6 use expected value.)
2. **3-date rounds:** a swap is allowed after each date. (Phase 8 is a real multi-window swap.)
3. **Postponed games:** don't count. (Matches the current exclusion.)
4. **Injury multipliers:** the plan's defaults, except Questionable = 0.6 (plan: Out 0, Doubtful 0.4,
   Questionable/Uncertain/Game-time 0.75, Expected/Ready 0.95).
5. **Overrides:** win over the model for any player, and expire after a round.
6. **Variance/captain toggle:** not now; added to the roadmap (below).
7. **Display:** expected value is the main number, with p_play as a small % and the
   "if he plays" figure in the tooltip.

### Roadmap additions

- **Risk-aware captain (from Q6):** an aggressive/balanced/safe toggle (score = mean + lambda x SD, lambda = 0
  by default) once Phase 6's outcome model exists; ship only if the backtest shows variance-aware picks pay.

## Guiding principles

- **Measure before and after every change.** Phase 0 builds the harness.
  Every later phase states its acceptance criteria in the harness's
  metrics, and records them in the testing log.
- **No tuning on the evaluation season.** Tune on E2024 (history from
  E2023). Hold out E2025, using E2024 as the blend prior exactly like the
  live app. E2026 R1+ is an extra check, with wide CIs.
- **Paired comparisons.** Bootstrap over rounds, with the same targets,
  rosters and seeds for both arms. Report 95% CIs.
- **Keep the heuristic transparent.** Every new knob is a named module
  constant with a comment explaining it. Nothing opaque goes into the
  default path.
- **Backward compatibility in steps.** New behavior goes behind a config
  object first. It is flipped to default only once the harness confirms
  it. Old configs stay reproducible, for comparison.
- **Leakage discipline.** Everything that feeds a projection must be
  strictly before the target round's start date, not just its round
  number.

---

## Phase 0: Projection-accuracy harness

**Goal:** a permanent, repo-resident tool that measures projection quality
directly. The existing `backtest_eval.py` measures only swap gain (B3).
This is the yardstick for Phases 1–4.

**Depends on:** nothing.

**New files:**

`projection_eval.py` (CLI) and `engine/proj_eval.py` (library), with
these pieces:

- **`build_targets(conn, seasons, mode)`**
  - For each regular-season round r of each target season, compute the
    projections the live app would have produced at r's start. Mode
    `blended` uses the prior season plus current rounds before r. Mode
    `single` is the old behavior, kept for comparison.
  - Pair each projection with the actual outcome in round r.
  - Targets carry: `player_id`, season, round, position, team,
    `team_changed` flag, games-before count, projected value, actual PIR,
    actual with bonus, played flag, and an "absent" flag.
  - Absent means the player was on the same club both before and after
    the round but has no row in it. This gives the unconditional view.
  - The backtest analyst's `build_targets.py` already did this and matched
    the engine with 0 mismatches over 4,398 targets. Port that logic, and
    reproduce the 0-mismatch check as a test.
- **Pluggable estimators.** A callable `(history_rows, as_of) → {pid: value}`,
  so the live config, baselines (season-to-date mean, last-5, prior-season
  mean) and candidate configs all run through the same harness.
- **Metrics.**
  - MAE, RMSE and bias, in two views:
    - conditional on playing;
    - unconditional, with DNPs and absences counted as 0.
  - Calibration: the slope and intercept of actual regressed on
    projected, plus a decile table.
  - Ranking: per-round Spearman, top-10 and top-30 hit rate, and the mean
    actual PIR of the projected top 30. The hindsight ceiling is shown for
    reference.
  - Segments:
    - games before the round: 0, 1–2, 3–5, 6–10, 10+;
    - rounds 1–5 vs later;
    - position;
    - team-changer vs not;
    - projection decile.
  - Zeroed-player cost: the count of players projected 0, and their
    actual mean PIR.
- **Comparison.** A paired bootstrap over rounds for any two estimators.
  Report the ΔMAE, Δtop-30 hit rate and Δtop-30 actual PIR, each with a
  95% CI.
- **Win-probability metrics,** added later in Phase 4: Brier score and log
  loss, from a separate target builder (team-games).
- **Availability metrics,** added later in Phase 3: Brier score for
  p_play, and reliability buckets.
- **Output:** a printed summary plus an optional CSV under `reports/`. The
  folder is committed when a run is cited as a headline.

**Tests** (`tests/test_proj_eval.py`):
- The 0-mismatch check against `build_projections` on a small fixture.
- Target construction respects the cutoff: no target's history contains a
  row dated at or after the target round's start.
- Metric functions are checked on hand-computed toy data.

**Acceptance:**
- Reproduces the review's baseline numbers within rounding. Live config,
  E2024+E2025, played targets: MAE ≈ 5.835, slope ≈ 0.76.
- These baseline numbers are recorded in the testing log as "before".

**Commit:** "Projection-accuracy harness (projection_eval.py)".

---

## Phase 1: Date-correct cutoffs and reschedule handling (L1, L2, L3, B5)

**Goal:** remove future-data leakage from postponed games, everywhere.
This is done before tuning, so tuning isn't fit to leaked data.

**Depends on:** Phase 0, to re-measure. Open question 3 affects only the
"how to score the postponed game itself" sub-step.

**Design:**

1. **Round main window.** New helper,
   `engine.lineup.round_main_window(dates) → (start_date, end_date, outlier_dates)`.
   - Given all game dates of a round (from the schedule, or from box-score
     rows for history), the main window is the densest cluster: dates
     within 3 days of the round's median date.
   - Dates outside that are **rescheduled outliers**.
   - Unit-test it on the real cases: E2025 R14, where game 134 falls on
     2026-03-17, and E2025 R34, where the 03-10 early game is an outlier
     before the main window.
2. **Cutoff by date, not round.**
   - `build_projections` gains a `cutoff_date` parameter. History is rows
     with `game_date < cutoff_date`.
   - `as_of_round` remains for compatibility. When both are given, the
     date filter is applied in addition, so old callers stay correct.
   - The live app and the backtest always pass `cutoff_date` = the
     target round's main-window start date.
   - Rows with a missing date are excluded from history when a date cutoff
     is set, and logged. The auditor found none today, so this is a
     guard.
3. **Blending.** `blend_season_rows` already preserves real dates, so a
   date cutoff works across the season boundary unchanged. The
   round-renumbering stays only for `as_of_round` compatibility and the
   form ticker.
4. **`app._load_pool_rows(before_round)`**
   - Translate `before_round` into a cutoff date using the schedule table:
     the main-window start of that round.
   - Keep the existing round filter as well. This catches a game in an
     earlier round that hasn't been played yet; it would have no box score
     anyway.
5. **Team win-rate ordering (L2).** Sort a team's results by `game_date`
   first, then `game_code`, in `build_projections`. This is also replaced
   wholesale in Phase 4, but fix it here so Phase 2 tuning uses correct
   win rates.
6. **Day-1/day-2 assignment (L3).**
   - `team_dates_for_round` and `team_dates_from_schedule` take the main
     window into account.
   - Outlier-dated games are dropped from the round's `team_dates`. Those
     teams count as "not playing this round", pending open question 3.
   - Day 1 = the earliest date *within the main window*.
   - `day1_results` uses the same definition.
7. **Postponed game scoring (needs open question 3).**
   - Until answered, the backtest and tracker **exclude** outlier-dated
     games from the round's actuals, and the backtest excludes those
     teams' players from that round's trials.
   - The live app shows a note on `/lineup` when a team's game in this
     round is an outlier.
8. **`classify_phase` (B5).** Classify by main-window dates, so a
   regular-season game rescheduled into the playoff period doesn't make
   that round look like a playoff round, and vice versa.

**Files:**
- `engine/projections.py`
- `engine/lineup.py`
- `app.py` (`_load_pool_rows`, `compute_recommendation`, the tracker's
  calls)
- `backtest_eval.py`
- `engine/tracker.py` (actuals from the main window only)
- `docs/technical_notes.md`: a new subsection on round main windows and
  reschedules

**Tests:**
- `round_main_window` on the real E2025 R14 and R34 patterns.
- A synthetic history with a postponed game: verify it doesn't enter
  projections for any round whose start date precedes the game's real
  date.
- `team_dates_*` drops outliers.
- Existing tests still pass.
- E2025 R20: Fournier's projection now equals the date-correct 8.15, not
  13.49. (This is a live-DB check, recorded in the testing log rather than
  as a unit test.)

**Acceptance:**
- The leak count from the auditor's `leak.py` method drops to 0 for
  E2023–E2025.
- Phase 0 harness numbers are re-recorded as the new "after leakage fix"
  baseline. Expect slightly worse MAE in E2025, since the leak made the
  projections look better.

**Commits:**
- "Round main window + date-based projection cutoff"
- "Reschedule-aware day-1/day-2 assignment"

---

## Phase 2: Estimator rewrite (P2, P3, P4, P5, P6, P7, P8, P9)

**Goal:** better and calibrated conditional-on-playing PIR. The target is
the backtest analyst's "Config B", re-tuned on the leakage-free data.

**Depends on:** Phases 0 and 1. Open question 5 affects P7.

**Design.** Introduce a `ProjectionConfig` dataclass in
`engine/projections.py`:

```
weighting: "ramp" | "exp"      (default "exp")
half_life_games: float         (default 10)
window_games: int              (default 60)
season_age_weight: float       (default 0.25; per season back, multiplicative)
shrink_k: float                (default 2.0 pseudo-games)
shrink_target: "position" | "position_replacement"   (default "position")
min_games: int                 (default 1)
exclude_prior_playoffs: bool   (default: decided by the harness)
team_change_shrink_k: float    (default: decided by the harness; 0 means off)
```

`build_projections(rows, cutoff_date=..., config=DEFAULT_CONFIG)`. Today's
behavior is reproducible as `LEGACY_CONFIG`, and the harness compares it
against the new default.

**Pieces:**

1. **Weights.**
   - Each played game gets weight `0.5 ** (age_in_games / half_life)` ×
     `season_age_weight ** seasons_back`. "seasons_back" is 0 for the
     current season, 1 for the prior, 2 for two back, and so on.
   - Only the most recent `window_games` games are used.
   - "Age in games" counts back from the player's most recent game.
   - Weighting needs each row's season, so `blend_season_rows` must keep
     `season_code` on rows. Verify it does; the DB rows have it.
2. **Multi-season history (P5).**
   - `_load_pool_rows` loads **all** locally synced seasons (E2023+), not
     just prior plus current. The season-age weight makes old seasons
     count little but non-zero.
   - This closes the "known gap" for players with E2023/E2024-only
     history (ZIZIC, DEJULIUS) without a special case.
   - Generalize `blend_season_rows` to N seasons, or skip blending
     entirely once the date cutoff exists. Rounds are then only needed
     for the form ticker and backtests. The latter is cleaner, so prefer
     it.
   - **Harness check:** is `season_age_weight` for 2+ seasons back
     non-harmful? If it hurts, cap at prior season and handle the gap
     with an explicit "stale history" flag instead.
3. **Shrinkage (P2).**
   - `n_eff = (Σw)² / Σw²` (Kish effective sample size).
   - `projected = (n_eff × weighted_mean + k × target) / (n_eff + k)`.
   - Target: the position mean, computed leakage-safe from the same
     history window. The position mean is weighted per game played, so it
     reflects "a typical player who plays".
   - Alternative tried in the harness: 0.6 × position mean, as the
     statistician suggested for small samples. Pick by the harness on
     E2024.
4. **Team-change shrink (P8).**
   - If the player's most recent game is for a different club than his
     current roster club (known at merge time) or than his previous game,
     apply extra shrinkage `team_change_shrink_k`.
   - This has to run where the current team is known, which is after
     `merge_roster`. So add a `reshrink_for_team_change(projections,
     roster)` step called from `get_pool`.
   - Tune k on E2024 team-changers. The `proj/` work found the
     `team_changed` feature gives a ~20% MAE gain for these players; use
     it as a cross-check.
5. **Minimum games (P4).**
   - `min_games=1`. Shrinkage handles small samples.
   - A player with 0 history still gets the `merge_roster` placeholder,
     but see item 7.
6. **Prior-season playoffs (P9).** Harness test: drop prior-season playoff
   rows from history vs keep them. Pick by the harness.
7. **Unknown-player prior.**
   - The zero-history placeholder moves from 0.0 to a role-based prior,
     with a separate constant `UNKNOWN_PLAYER_PRIOR` (the statistician
     measured ~5.1 PIR for unknown players who did play). This is
     conditional-on-playing, so availability (Phase 3) multiplies it
     down.
   - The harness showed the position mean was *worse* than 0 for E2024 and
     E2025 unknowns (mostly fringe players) but better in E2026. Decide
     the number in Phase 3, where p_play for unknowns can be low. Until
     then, keep 0.0.
   - NEW/EST badges are unchanged.
8. **Manual overrides (P6, P7).**
   - Overrides go through the same bonus path as everyone else:
     `projected_pir_with_bonus` includes the win bonus from the player's
     current team.
   - If open question 5 is answered "yes", an override applies to any
     player, known or not. Store it as the conditional PIR; Phase 3
     availability still applies on top.
9. **Volatility.**
   - Weighted SD with the same weights, shrunk toward the position-level
     SD by the same `k`. It isn't used in decisions until Phase 6, but
     compute it properly now.
   - Also store `n_eff` on `Projection`, for display and for Phase 6.
10. **Form ticker.** `recent_pir` is unchanged (plain last-3 mean).
    `projection_change` = projection now minus projection before the last
    game, both computed with the new estimator. Re-check the HOT/COLD
    thresholds against the new, less volatile projection with a quick
    count of how many players flag per round before/after. Adjust the
    thresholds only if flag counts move a lot.
11. **ML paths.** `engine/ml_projections.py` builds on base `Projection`
    fields. Check it still runs, and leave its estimator logic alone (it's
    revisited in Phase 5).

**Tuning protocol:**
- Sweep, on E2024 only:
  - `half_life` ∈ {6, 8, 10, 12, 15, 20};
  - `window` ∈ {20, 30, 60, 100};
  - `season_age_weight` ∈ {0.15, 0.25, 0.4, 0.6};
  - `shrink_k` ∈ {0, 1, 2, 3, 4};
  - target variant, playoffs in/out, `team_change_k`.
- Pick by E2024 MAE with calibration slope within [0.9, 1.05] as a tie-break.
- Then evaluate *once* on E2025 and E2026.

**Acceptance** (E2025 holdout vs LEGACY_CONFIG, both on Phase-1 leak-free
data):
- ΔMAE ≤ −0.10, CI excludes 0. The review measured −0.149.
- Calibration slope in [0.9, 1.05]. The review measured 0.98.
- Δ actual PIR of projected top 30 ≥ 0, CI lower bound > −0.1. The review
  measured +0.44.
- No segment's bias exceeds ±0.5. Segments are games-before, position and
  team-changer.
- E2026 R1–5: ΔMAE < 0.

**Files:**
- `engine/projections.py`
- `engine/rosters.py`
- `app.py` (`_load_pool_rows`, `get_pool`)
- `engine/db.py`, if multi-season loading needs a helper
- `tests/test_projections.py`: new tests for weights, shrinkage, n_eff,
  `min_games=1`, and season-age weighting

**Commits:**
- "ProjectionConfig + exponential/shrinkage estimator (behind config)"
- "Switch default to tuned config; multi-season history"
- "Manual overrides get the win bonus" (plus "apply to any player" if open
  question 5 is yes)

---

## Phase 3: Availability, p_play (P1)

**Goal:** stop pricing part-timers and frequently absent players as if they
always play.

**Depends on:** Phase 2. Open questions 1, 4 and 7.

**Design:**

1. **Fields.** New `Projection` fields:
   - `p_play: float` (0–1);
   - `expected_value: float` = `projected_pir_with_bonus × p_play`.

   `projected_pir` keeps its meaning: conditional on playing.
2. **Estimating p_play** (fit on E2024, test on E2025):
   - Per player, take the team's last N games while he was on that team.
     Each game counts as played, dressed-DNP (`played=0` row) or absent
     (no row at all).
   - "While on the team" is the span from his first appearance for the
     current club, so games before he joined don't count against him.
   - The base rate is a smoothed play share, i.e. a Beta-binomial shrunk
     toward a prior that depends on his projection tier. The review found
     play rate tracks projection: 57% in the bottom quintile, 88–89% at the
     top.
   - Candidate models, all compared on Brier score:
     - (a) the statistician's `0.3 + 0.7 × share_last10`;
     - (b) Beta-binomial share with a tier prior;
     - (c) a small logistic on share_last10, share_last3, a
       consecutive-absences count and the projection tier.
   - Pick the simplest one within 0.002 Brier of the best.
   - **Long absences** (injury): 3+ consecutive absent team games
     dominate. A player returning from injury is handled once he plays
     again, since share_last3 recovers. A player still absent stays low,
     which is correct.
   - **Unknown/new players** with no history get a separate p_play prior,
     measured from E2024/E2025 debutants' play rate in their first rounds.
     This replaces the "0.0 or position mean" dilemma from Phase 2 item 7.
     The `UNKNOWN_PLAYER_PRIOR × p_play_unknown` product is checked in the
     harness against the E2024, E2025 and E2026 unknowns.
3. **Injury integration.**
   - Final `p_play = model_p_play × injury_multiplier(status)`, using the
     multipliers from open question 4.
   - "Out" = 0 keeps today's behavior, now as one case of the general rule.
   - **Refactor:** move the `out_ids` zeroing out of
     `compute_recommendation` (`app.py:1025, 1063-1065`) into a single
     place, applied in `get_pool` after injuries are resolved. Every
     consumer then gets the same expected value.
4. **Consumers.** Each switches to `expected_value`:
   - **Draft board.** Value and VORP (`app.py:343-344, 547`); the
     replacement level is recomputed on expected values.
   - **Transfers.** `projected_gain` and `suggest_transfers`
     (`engine/transfers.py:18-46`). `MIN_UPGRADE_GAP` may need re-tuning;
     check how many suggestions appear before/after.
   - **Lineup decisions.** `projected_value` in `compute_recommendation`
     and the backtest. The exact semantics depend on open question 1:
     - **If no auto-sub:** a no-show in a full slot scores 0, so
       `expected_value` is the right value for exclusion, formation and
       captain decisions.
     - **If auto-sub:** a no-show's slot gets filled from the bench. The
       deterministic lineup should then use conditional values for slot
       ordering, with availability affecting only the *exclusion* choice.
       Re-plan this sub-step once answered; Phase 6's Monte Carlo handles
       both cases cleanly.
   - **Swap decision, day-2 side.** Day-2 players use `expected_value`.
     Day-1 players use their real results, as already done.
   - **Roster sampling** in the backtest (`engine/roster.py:133-228`)
     switches to `expected_value`, so random rosters look like real
     drafts.
   - **Tracker snapshots.** `tracker.py:122` stores `projected_value`;
     store `expected_value`, and also `p_play`, for display.
5. **Display (open question 7).**
   - Show `p_play` as a small % next to the projection where space allows
     (draft board, roster, lineup).
   - Update the `/how-it-works` page.

**Acceptance:**
- The harness's unconditional bias moves from about +1.0 to within ±0.2
  on E2025.
- p_play's Brier score beats the "always plays" baseline and the flat-rate
  baseline on E2025.
- Phase 5's backtest (once it exists) shows no regression in realized
  points. If Phase 5 isn't done yet, this check is deferred and noted in
  the log.

**Files:**
- `engine/projections.py`, or a new `engine/availability.py` (preferred,
  for separation)
- `app.py`
- `engine/transfers.py`
- `engine/roster.py`
- `engine/tracker.py`
- templates for display
- `tests/test_availability.py`

**Commits:**
- "Availability model (p_play) + harness metrics"
- "Use expected value (p_play) in draft board, transfers and lineup"
- "Show p_play in UI + how-it-works"

---

## Phase 4: Win probability, home/away and the team-change bonus (W1, W2, W3, X4)

**Goal:** a real per-game win probability, and per-round, opponent-aware
expected value for lineup decisions.

**Depends on:** Phases 1 and 2. Independent of Phase 3, but it's simpler
after it.

**Design:**

1. **`engine/win_prob.py`**
   - `fit_win_model(team_game_rows)` → coefficients for
     `logit(p_home_win) = a + b × (form_home − form_away)`, where form is
     the team's last-20 win rate, date-ordered and leakage-safe. Then try
     adding season-to-date point/PIR differential (net rating proxy) as a
     second feature.
   - Fit on E2023–E2024; evaluate Brier on E2025 and E2026. The review
     measured 0.219 for this kind of model vs 0.265 for the current stand-in.
   - Coefficients live as constants in the module, with a comment linking
     the fit script and date.
   - Early season: blend in the prior season's form, same date-based
     history.
2. **Per-round context.**
   - New function `round_values(pool, schedule_rows, round_no)` →
     player_id → round-specific expected value, combining:
     - the projection (conditional);
     - plus the home/away PIR adjustment: ±0.35, or as fitted on E2024;
     - × p_play;
     - × (1 + 0.10 × p_win) for that specific game.
   - Optional X4 refinement: if fitting shows it, the conditional-on-win
     PIR shift. Expected impact is ~0.1 PIR; skip unless it falls out for
     free.
   - Used by `compute_recommendation` and the backtest.
   - The draft board and transfers keep a round-independent value: the
     season-average win probability from the team's form vs an average
     opponent, home/away neutral.
3. **Team change (W3).** `merge_roster` recomputes `team_win_rate` and the
   bonus for any player whose team changed. Implementation: expose a
   per-team form/strength map from `build_projections`, or compute it in
   `win_prob`, and pass it into `merge_roster`.
4. **Remove `team_win_rate × PIR`** from `projected_pir_with_bonus` for
   the default path. Keep the field for compatibility, now defined as
   PIR × (1 + 0.1 × p_win_vs_average).

**Acceptance:**
- Win-model Brier on E2025 below 0.236 (the home-only baseline); target
  ≈ 0.22.
- Harness MAE on the bonus-inclusive target improves slightly or stays
  neutral.
- The backtest shows no regression.

**Files:**
- `engine/win_prob.py` (new)
- `engine/projections.py`
- `engine/rosters.py`
- `app.py`
- `backtest_eval.py`
- `tests/test_win_prob.py`

**Commits:**
- "Logistic win-probability model"
- "Per-round expected value with home/away and opponent"
- "Team changers use new team's win probability"

---

## Phase 5: Backtest overhaul (B1, B2, B3, B4) and the ML question

**Goal:** a backtest that runs the live decision path and measures what we
actually care about: realized fantasy points. This yields a new, trusted
headline.

**Depends on:** Phase 1. It runs better after Phases 2–4, but its
structural fixes can land any time after Phase 1. **Recommended timing:**
land 5a right after Phase 1, so Phases 2–4 can each be checked for
realized-points impact as well as accuracy.

### 5a: Make the backtest match the live path

1. **Date-based cutoff** (from Phase 1) and main-window `team_dates`.
2. **Blended mode** (B2).
   - `--history multi`, the default, uses the same multi-season loader as
     the live app.
   - `min_round` defaults to 1.
   - The old single-season mode stays as `--history single`, for
     comparison with historical log entries.
3. **Day-1 no-shows = 0** (B1).
   - Build schedule-like rows for the target round from all of that
     round's games: box-score `game_code` plus date plus teams.
   - Call `engine.lineup.day1_results`, exactly as live does.
   - Players on a finished day-1 team with no row get 0.
4. **Team assignment.** Use the team from the player's most recent
   pre-cutoff game, as now. There's no historical roster data. This is
   documented as a known difference from live, where the roster merge
   uses the current club.
5. **Actuals aggregation** (B4). `actual_pir_lookup` sums per player per
   round, the same as `tracker.actual_scores`.
6. **Same scoring functions as live** throughout: `compute_round_score` and
   `actual_fantasy_score`.

### 5b: Metrics that see projection quality (B3)

- For each trial, record **absolute realized round points** for:
  - the full recommended plan (exclusion + lock + swap);
  - the no-swap plan;
  - the hindsight best.
- **Method comparison mode:** `--compare legacy,default[,ridge]` runs the
  *same* rosters and seeds through each projection method and reports
  paired differences in realized points per round, with a bootstrap CI.
  This is the metric the ML question should have been judged on.
- Keep the existing swap-gain metric as a secondary column, for
  continuity with old headlines.
- **Draft-value proxy** (optional): for each method, build a greedy
  13-man roster from its projections at R1 cutoff, under the
  position/exclusion rules. Measure the season-total realized points of
  that roster's best-lineup-per-round. That captures draft/transfer value,
  which random rosters can't.

### 5c: Re-ask the ML question

- Re-run Ridge, GBM and ensemble through `--compare`, on both:
  - the harness, for accuracy, calibration and ranking;
  - the backtest, for paired realized points.
- If Ridge wins with CI excluding 0 on the E2025 holdout, propose (don't
  auto-adopt) making it an optional `ProjectionConfig.method="ridge"`, and
  ask the user. Ridge then also needs the p_play and shrinkage layers on
  top, or it has to be re-fit with them in mind.

### 5d: New headline

- Run the full backtest on the final config: E2023 R1+ through E2025,
  regular season, with no-shows, date cutoffs and blending.
- Record the result in the testing log, and replace the stale headline in
  `CLAUDE.md`. Explicitly mark the old numbers as superseded and say why.

**Files:**
- `backtest_eval.py`
- possibly `engine/backtest.py`, to extract the trial logic so it shares
  code with `compute_recommendation`
- `tests/test_backtest.py`, new: a trial on a tiny fixture where a day-1
  no-show must be valued at 0

**Commits:**
- "Backtest: live-path parity (date cutoff, blend, no-shows = 0)"
- "Backtest: paired realized-points method comparison"
- "Backtest headline rerun" (testing log + `CLAUDE.md`)

---

## Phase 6: Uncertainty-aware decisions (U1)

**Goal:** choose the exclusion, formation, day-1 lock and captain by
*expected* realized points under uncertainty, so the swap option value is
real.

**Depends on:** Phases 2 (n_eff, volatility), 3 (p_play) and 5 (to
measure). Open questions 1 and 6.

**Design:**

1. **Outcome model per player-round.**
   - With probability `1 − p_play` the score is 0. If open question 1 says
     auto-sub, the slot is refilled from the bench instead.
   - Otherwise the score is projection + a residual.
   - The residual is drawn from an empirical distribution: the harness's
     out-of-sample residuals bucketed by projection decile, scaled by the
     player's shrunk volatility relative to the bucket's typical
     volatility. Clip at a realistic floor, since PIR can be negative.
   - Fit and validate on E2024 residuals. Check calibration: the 10% and
     90% quantiles should cover ~10% and ~90% on E2025.
2. **Monte Carlo evaluation.**
   - For each candidate plan, draw S scenarios of day-1 outcomes (common
     random numbers across candidates), and run the existing
     `swap_after_day1` on each with the realized day-1 values plus day-2
     expected values.
   - Score is the mean final score over S.
   - The deterministic search stays as the fallback and as the S=0 mode.
3. **Search cost.**
   - 75 exclusions × 3 formations × captain choices, × S=200, × one swap
     solve each, may be too slow for `/lineup`.
   - Mitigation: a deterministic pre-pass ranks all candidates, then Monte
     Carlo re-scores the top K (e.g. 15).
   - Measure the wall time. The target is under 2 s per `/lineup` render.
     Cache per (manager, round, DB mtime).
4. **Captain.** Pick the captain by expected value under the same
   scenarios. If the user wants open question 6's toggle, add a risk
   parameter λ (score = mean + λ × SD), λ=0 by default.
5. **Day-2 swap decision.** It stays deterministic, since day-1 results are
   real by then and day-2 players' expected values are the right
   point estimate. The exception is a captaincy move, where variance can
   matter (open question 6).

**Acceptance (Phase 5 backtest, paired):**
- Realized round points on E2025 improve vs the deterministic planner,
  with the CI lower bound ≥ 0.
- If the improvement is within noise, keep it behind a flag (default off)
  and record the null result, same posture as the team-strength attempt.

**Files:**
- `engine/lineup.py`
- `engine/outcomes.py` (new; residual model)
- `app.py`
- `backtest_eval.py`
- `tests/test_lineup.py` (deterministic equivalence when S=0;
  seeded-reproducibility test)

**Commits:**
- "Outcome model (residual distribution + p_play)"
- "Monte Carlo plan scoring for exclusion/formation/captain"

---

## Phase 7: Tracker point-in-time correctness (T1)

**Goal:** a past round's suggestion uses only what was knowable before that
round, including injuries and rosters.

**Depends on:** Phase 3, since injuries now feed p_play everywhere.

**Design:**

1. **Injury snapshots.**
   - `sync_injuries.py` currently does a full replace. Add an
     `injury_snapshots` table (`synced_at`, plus the same columns),
     append-only, written on every sync alongside the current-state table.
   - `get_pool(before_round=r)` with r in the past uses the latest
     snapshot with `synced_at` before round r's main-window start.
   - If there's none, it uses **no** injury info (neutral multipliers
     only), never today's.
2. **Roster snapshots.** The same pattern for `rosters` / `fantasy_pool`:
   a `roster_snapshots` table written on each sync, and point-in-time
   lookup for past rounds. If there's no snapshot, fall back to each
   player's last pre-cutoff team (the backtest's rule), not today's
   roster.
3. **Tracker save.** `tool_day1` / `tool_final` snapshots already freeze
   the suggestion at save time. Also store the per-player `p_play` and
   injury status used, so re-rendering a saved round never recomputes
   them.
4. **Cache key.** `get_pool`'s cache key already includes `before_round`.
   Verify that point-in-time variants don't collide.

**Acceptance:**
- A test: an injury synced *after* round r's start must not affect
  `get_pool(before_round=r)`.
- Re-rendering `/tracker/1` with a later injury sync gives an unchanged
  tool suggestion. (Do this on a DB copy; patch `app.get_connection`, per
  memory.)

**Files:**
- `engine/db.py`
- `engine/injuries.py`
- `sync_injuries.py`
- `sync_rosters.py`
- `sync_fantasy_pool.py`
- `app.py`
- `engine/tracker.py`
- tests

**Commits:**
- "Injury/roster snapshots for point-in-time pools"

---

## Phase 8: Multi-date rounds (R1)

**Depends on:** open question 2. Do nothing until it's answered.

- **If there is one swap window after day 1:** current behavior is
  correct. Document it in `docs/game_rules.md` and close R1.
- **If there's a window after each date:**
  - Generalize `swap_after_day1` to a sequence of windows,
    `swap_after_dates(lineup, team_dates, value_fn, known_through=date)`.
    Each window applies the same golden rule ("can only promote someone
    who hasn't started") with the halving rule.
  - `day1_results` becomes `results_through(date)`.
  - The backtest and tracker validate against a 3-step final.
  - Phase 6's Monte Carlo extends naturally: sample sequentially.
  - Tests: the R4/R7 E2026 patterns.

---

## Phase 9: Smaller items (X1, X2, X3)

Each is independent. Do them after Phase 5, so they can be measured.

1. **Transfer horizon (X1).**
   - `projected_gain` gains an optional horizon: the sum over the next N
     rounds (default 5) of each player's round expected value.
   - Uses the schedule, so it sees double weeks, and later Phase 4
     opponents and home/away, with p_play.
   - The UI shows both "per game" and "next N rounds".
   - Measure: does horizon-based ranking pick transfers with higher
     realized N-round points in backtest? Use a simple simulation over
     E2025.
2. **Double-round fatigue (X2).** Harness-only investigation: the residual
   for games on ≤2 days' rest vs normal.
   - If |effect| > 0.5 PIR with a CI excluding 0, add a `rest_days`
     adjustment in Phase 4's `round_values`.
   - Otherwise log the null result.
3. **Projection cache robustness (X3).** Add a cheap DB "version"
   (`PRAGMA data_version`, or a row-count/max-rowid fingerprint of the
   tables `get_pool` reads) to the cache key alongside mtime.
   - Low priority; do it only if Phase 7's snapshot tables make staleness
     more likely.

---

## Phase 10: Documentation (continuous, per working agreement)

With each phase, not batched at the end:

- **`docs/testing_log.md`:** one entry per phase. What was measured, the
  before/after harness numbers, backtest numbers, anything surprising.
- **`docs/technical_notes.md`:**
  - rewrite the "heuristic design rationale" section once Phase 2 lands;
  - add availability (Phase 3), win probability (Phase 4), the outcome
    model (Phase 6) and point-in-time snapshots (Phase 7).
- **`docs/game_rules.md`:** record the answers to open questions 1, 2 and
  3 once the user confirms them.
- **`/how-it-works` page and `README.md`:** new value definitions (expected
  value, p_play, EST semantics) and the new CLI (`projection_eval.py`).
- **`CLAUDE.md`:**
  - replace the stale headline after Phase 5d;
  - mark the "known gap" closed after Phase 2;
  - keep this plan linked under "Natural next steps" until it's done.

---

## Dependency summary

```
Phase 0 ─┬─> Phase 1 ─┬─> Phase 5a ─> (measures 2,3,4,6)
         │            ├─> Phase 2 ─┬─> Phase 3 ─┬─> Phase 6
         │            │            │            └─> Phase 7
         │            │            └─> Phase 4
         │            └─> Phase 8 (blocked on open question 2)
         └─> Phase 9 (after 5)
```

Suggested order: 0 → 1 → 5a → 2 → 3 → 4 → 5b/5c/5d → 6 → 7 → 8 → 9.

## Risks

- **Overfitting the tuning.** Only ~34 rounds of tuning data per season.
  Mitigations: a coarse grid, a single holdout evaluation, and preferring
  simpler configs within the CI.
- **Behavior change surprises the user.** Every projection number in the
  app will move, and part-time players drop noticeably on the draft board
  and transfers. Flag this in the commit message and testing log, and
  mention it to the user before switching the default (end of Phase 2,
  and again Phase 3).
- **The tracker's historical snapshots** were saved with old projections.
  They stay as saved. Re-scoring uses actuals, not projections, so nothing
  changes for already-saved rounds.
- **Monte Carlo cost** on `/lineup`. Mitigated by top-K re-scoring and
  caching. Fall back to deterministic if it's too slow.
- **Postponed-game scoring rule unknown** (open question 3). The interim
  choice (exclude) may slightly misstate a few rounds.

---

## Appendix: review findings, 2026-10-15 (verbatim numbers, preserved)

### Backtest analyst

**Setup:**
- Walk-forward, live blended path.
- Reimplementation checked against `build_projections`: 0 mismatches over
  4,398 targets.
- Target: raw `pir_official`.
- Main metrics on 14,859 played player-games, E2024+E2025.

**Accuracy** (8,921 common targets):

| Estimator | MAE | RMSE | Bias |
|---|---|---|---|
| Live (ramp, last 10, min 3) | 5.835 | 7.496 | −0.07 |
| Season-to-date mean | 5.758 | 7.410 | −0.20 |
| Last-5 mean | 5.986 | 7.710 | −0.06 |
| Prior-season mean | 6.228 | 7.962 | −0.23 |
| Shrinkage (flat 20, k=4 → position mean) | 5.797 | 7.390 | −0.24 |
| Per-minute × last-3 minutes | 5.851 | 7.519 | −0.08 |

As deployed (zeroed = 0): MAE 5.735. E2026 R1–5: MAE 6.11, bias −0.98.

**Calibration:**
- Slope 0.76 (E2024 0.785, E2025 0.744).
- Decile 1: projection 0.96, actual 2.85.
- Decile 10: projection 18.88, actual 16.33 (+2.55).

**Segments:**
- Bias within ±0.33 across games-before buckets.
- Rounds 1–5 MAE 5.94 vs 5.68 later.
- MAE by position: G 6.01, C 5.72, F 5.41.
- Team-changers: MAE 6.20 vs 5.65, bias −0.03.

**3-game minimum:**
- E2024: 302 played-games zeroed (4.6%), mean actual 4.86.
- E2025: 409 (5.0%), 5.63.
- E2026 R1–5: 218 (19.1%), 7.05.
- `min_games=1` E2026 ΔMAE −0.31 [−0.55, −0.07]; E2025 neutral.
- Shrunk 1–2-game estimate: MAE 5.53 vs 7.78 for projecting 0 (E2026).

**Ranking:**

| | Spearman | Top-30 hit | Top-10 hit | Projected top 30 actual mean |
|---|---|---|---|---|
| Live | 0.467 | 40.1% | 22.4% | 15.46 |
| Hindsight ceiling | | | | 23.11 |

**Tuning** (4,480 configs; tuned on E2024, held out on E2025/E2026):

| Config | Season | ΔMAE | Top-30 actual PIR | Slope |
|---|---|---|---|---|
| **B** (exp hl10, W60, prior-season ×0.25, k=2, min 1) | E2025 | −0.149 [−0.206, −0.099] | +0.44 [+0.14, +0.73] | 0.98 (live 0.74) |
| B | E2026 | −0.453 [−0.77, −0.17] | | 0.87 |
| A (B without shrinkage) | E2025 | −0.147 | | 0.83 |
| C (ramp W20, min 1) | E2025 | −0.109 | | |
| Linear recalibration | E2025 | ~0, n.s. | | |

**Availability:**
- Absent-between-appearances: 2,425 cases, mean projection 6.5.
- DNPs: 1,141, mean projection 2.8.
- Unconditional bias: −0.05 → +1.03.
- Play rate: 57% in the bottom quintile, 88–89% in the top three.

### Statistician

**Availability:**
- 21.5% of projected player-rounds scored 0 (15.4% didn't dress).
- Unconditional bias +1.11.
- `× (0.3 + 0.7 × share_last10)`:
  - bias −0.02;
  - RMSE 7.36 → 7.09;
  - MAE 5.64 → 5.29.
- Play rate by availability bucket: 0.53 / 0.70 / 0.83 / 0.95.

**Estimator:**
- Exp decay hl10: R² 0.236 vs 0.206, slope 0.87.
- Exp decay hl15 with prior-season ×0.25: R² 0.242.
- Plus EB shrink k≈2: slope 0.96.

**Small samples:**
- 1–2-game players: 603 cases, mean actual 6.05.
- Shrunk estimate: MAE 6.79 → 5.44.
- Zero-history players who played: mean 5.1.

**Win probability:**
- Brier: last-10 win rate 0.265; flat 0.5 0.250; home-only 0.236;
  logistic (home + last-20 diff) 0.219.
- Home teams won 62%.
- Home/away PIR vs projection: +0.38 / −0.34.

**Volatility:** forecast-error SD by volatility bucket 5.5 / 6.7 / 7.6 /
8.5 (r=0.19). Volatility is unused in decisions.

**ML:** earlier fixed-split R²: Ridge 0.238 vs heuristic 0.181. The
backtest swap-gain metric cancels projection quality; CIs weren't paired.

**Team-changers** (n=193): R² −0.04, slope 0.47.

**Endorsed as sound:**
- blending (vs the season-start cliff);
- exact real scoring;
- Out-zeroing over hard exclusion;
- the exact brute-force search;
- the team-strength and `proj/` minutes rejections.

### Code and leakage auditor

78/78 tests pass. Confirmed clean:
- `played` filter;
- recency sort across the blend;
- no tz mixing or date ties;
- the win bonus is applied exactly once per path;
- no duplicate (season, game, player) rows.

**Confirmed issues:**
1. **Postponed-game leak.**
   - E2025 game 134 (R14, played 2026-03-17) is visible at cutoffs R15–37.
   - Fournier R20: 13.49 with the leak vs 8.15 date-correct.
   - Leaked player-game rows summed over cutoffs from R6: E2023 96,
     E2024 210, E2025 1,465 (R34 alone: 740; partly an early game).
   - The live app has the same round-only filter.
2. **Backtest no-shows** use the projection (586 day-1 player-rounds,
   mean projection 13.3); live uses 0.
3. **Backtest is single-season and starts at R6;** it never runs the live
   blend, roster merge, overrides or injuries.
4. **Tracker** uses current injuries (89 rows) and current rosters for past
   rounds.
5. **Team-change win bonus** stays with the old team: 68 players at R1, 9
   at R2. Example: Pradilla PAM→MAD keeps 0.7.
6. **Unflagged 0.0 projections:** 28 known players at R6, e.g. Coffey,
   Baldwin, Toppin.
7. **Reschedule dates distort day 1/day 2** (E2025 R34: PRS/OLY alone on
   03-10; R14: OLY/ULK on 03-17).
8. **Team win-rate window** is ordered by `game_code`.

**Suspected:**
- 3-date rounds;
- backtest last-row vs tracker sum for two rows per round;
- mtime cache staleness;
- an override can't lift a known 1–2-game player.


---

## Implementation notes (2026-10-15)

Deviations and findings while implementing, so the plan text above stays as
originally written.
These notes are chronological: later entries supersede earlier statements
that questions or phases are blocked. See the current backlog above for
the reconciled status.

- **Phase 0:** `engine/proj_eval.py` + `projection_eval.py`; `classify_phase`
  moved to `engine/rounds.py`. Baseline on E2024+E2025 after the leak fix:
  MAE 5.724 (conditional), slope 0.76, unconditional bias +1.17. The review's
  5.835 was on a different target subset; everything else reproduces.
- **Phase 1:** `round_main_window` lives in `engine/rounds.py` (re-exporting
  it from `engine.lineup` was not needed). Postponed games are excluded from
  a round's actuals everywhere until open question 3 is answered. B4 (summed
  actuals) and B5 landed here too; the rest of Phase 5a is not done.
- **Phase 2:** `shrink_target` ("position" | "position_replacement") was
  replaced by `shrink_target_scale` (1.0 or 0.6 x the position mean); only
  the position mean is implemented. The tuned config is `TUNED_CONFIG` (exp,
  half-life 10, window 60, season-age weight 0.25, k=1, min_games=1); k=1
  rather than the plan's 2 was the E2024 pick, scale 0.6 and a longer
  window gained <0.02 MAE and were not adopted. All seasons in the DB feed
  the live pool (E2025 check: using two seasons back vs one made no
  difference, MAE 5.6678 vs 5.6675, but covers 19 more players).
  `exclude_prior_playoffs` is implemented; the E2024 sweep kept it off.
  **P7** (override applies to any player) waits for open question 5.
  **P8** (extra shrink for team-changers) is NOT done: the tuned config
  leaves a +1.08 (E2025, n=77) / +1.25 (E2026, n=81) team-changer bias, which
  breaks the plan's +-0.5 segment criterion, as does the 3-5-games-before
  bucket in E2025 (+0.71, but -0.35 in E2024, so season noise). Every other
  acceptance criterion passed (see the testing log).
- **Not started:** Phase 3 (blocked: questions 1, 4, 7), Phase 4, Phase 5a
  parity items B1/B2 and 5b-d, Phases 6-9.
- **Phase 3 (done 2026-10-15):** `engine/availability.py` (p_play: recency-weighted share of
  the club's last 5 games over the current stint, shrunk toward 0.8; tuned on E2024 by Brier, confirmed on E2025;
  a 5-game window with half-life 2 beat the plan's longer windows because absences persist),
  `Projection.p_play`/`expected_value`, the injury multipliers, and every consumer switched to
  `expected_value` (draft board, VORP, transfers, lineup, roster sampling, tracker snapshots).
  The `out_ids` zeroing in `compute_recommendation` is gone: an Out player has p_play 0 in `get_pool`.
  Not done: p_play columns in the tracker tables (snapshots store the expected value only), and the
  "if he plays" PIR is a tooltip rather than a column. Unknown players: 0.5 (a judgment value; not
  measurable from this DB), or 0.8 when the user supplied an override. P7 landed with it.
- **Phase 8 (done 2026-10-15, R1):** `engine.lineup.swap_after_date` (one window, generalizing
  `swap_after_day1`, which is now a wrapper), `plan_round` (replays every window in order; `actual_through`
  says which results are real), `known_through_date`/`results_through` (what `day1_results` is built on).
  The live `/lineup` and the backtest use `plan_round`. Not changed: `build_lineup`/`choose_active_squad`
  still evaluate candidates with a single day-1 window (a planning approximation), the tracker's
  `final_rule_violations` still checks the final lineup against the day-1 lock only (a necessary, not
  sufficient, check for 3-date rounds), and the hindsight ceiling is unchanged.
