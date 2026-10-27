# EuroLeague Fantasy Strategist

A personal (non-commercial) tool for a 12-manager private **draft-mode**
EuroLeague Fantasy league. It tracks real box-score data, projects player
fantasy value, ranks players for the draft, and helps manage rosters/trades
for the season.

Not affiliated with, endorsed by, or licensed by Euroleague Basketball. Built
for personal use among friends — never monetized or redistributed.

## What it does

- **Projections** — a transparent PIR estimator using recency-weighted
  form, small-sample shrinkage and availability. A fixed logistic model
  estimates the +10% win bonus from team form and point differential.
  Draft/transfer values use a neutral matchup; lineup values use the actual
  scheduled opponent and home/away context. Transferred players use their
  current team's bonus. A **form ticker** next to every
  projection shows how it moved after the player's last game (▲/▼) and
  flags players scoring well above/below it over their last 3 (HOT/COLD).
- **Season points** — every player's real fantasy points so far this season
  (PIR + the 10% win bonus, before captain/bench multipliers): season total,
  per-game average, and last game, shown on the draft board (sortable),
  transfer suggestions/compare tool, and lineup builder.
- **Draft board** — every player ranked per position, with VORP
  (scarcity-adjusted value) and tiering (where the drop-off to the next
  player actually matters).
- **Draft & ownership tracking** — log real draft picks, trades, and
  free-agent adds/drops for all 12 managers; see who owns whom. A completed
  draft can also be bulk-imported from a draft-room app's CSV export
  (drag-and-drop or file picker on the draft board) instead of logging
  every pick by hand — matches each drafted player to this project's own
  data and auto-creates a league manager for each name in the file.
- **Transfer suggestions** — same-position upgrades from the actual
  unowned free-agent pool, plus a manual compare tool: pick a player to
  drop from your roster, then pick any same-position player to add from a
  dropdown (showing which manager already owns them, if any, or "Free
  agent") to see the projected PIR gain or loss.
- **Watchlist** — a personal shortlist of players to keep an eye on for
  future transfers, toggled from the draft board and reviewed on a
  dedicated Watchlist page; watched players are flagged with a ★ badge
  wherever they appear, including in the transfer compare tool's Add list.
- **Lineup builder** — for a chosen manager and round (including a round
  that hasn't been played yet), picks which 3 of 13 to exclude (always one Guard, one Forward and
  one Center, per the game's rules), the day-1
  starting lineup and captain, and the day-2 swap plan - auto-optimizing
  the starting formation (or force one manually) to maximize total
  projected score, shown in a totals box alongside the recommendation.
  Sync after day 1's games and the swap plan re-plans from their real
  scores; for your own team it starts from the day-1 lineup you saved in
  the Points Tracker. The swap table labels actual and projected scores separately
  and shows each player's contribution after the captain or bench multiplier.
  Each lineup also renders as a court "pitch view" (starters by position,
  a checkmarked bench with the 6th man tagged) above the detail table.
- **Points tracker** — record the lineup you actually played each round
  (day-1 lock and final post-swap lineup), scored automatically from real
  box scores, next to what the lineup builder suggested, the best lineup
  your roster could have played in hindsight, and optionally your official
  in-game total. Includes a season-to-date cumulative chart.
- **Roster sync** — current club rosters pulled directly from EuroLeague's
  data backend, so transfers and new signings are reflected without waiting
  for a player's first box score. Triggerable from the app itself (a
  **Sync** page) as well as from the terminal.
- **Real Fantasy draft-pool sync** — the real EuroLeague Fantasy game's
  actual draftable pool, synced from a user-maintained Google Sheet, is the
  primary source for the draft board's roster composition (who's on which
  team, at what position) — found to be more complete and current than
  EuroLeague's own official roster data, especially pre-season. Falls back
  to EuroLeague's own roster data if the sheet hasn't been synced yet. Also
  triggerable from the **Sync** page.
- **Dev tools** (on the Sync page) — a one-click "randomize a full draft"
  button for populating a realistic 12-manager league while developing,
  without hand-drafting 156 players; "clean the teams" to wipe all rosters
  back to free agency without a re-draft; and "clean the managers" for a
  full league reset (deletes managers, ownership, and transaction history —
  re-seed with `seed_league.py` afterward). Not for the real draft.
- **Manual projection overrides** — for a player with zero EuroLeague
  box-score history anywhere (a mid-season transfer from another league,
  say), the projection is otherwise a flat 0.0. `set_manual_projection.py`
  lets you set a researched estimate instead (marked with an `EST` badge on
  the draft board and roster pages) — the intended workflow is to ask a
  Claude Code session to look up the player first, then persist whatever
  number you settle on.
- **Injury report** — scraped daily from basketnews.com's EuroLeague injury
  report (`sync_injuries.py`, also triggerable from the Sync page) and shown
  as a status badge (Out/Doubtful/Questionable/Uncertain/Game-time/Expected/
  Ready) on the draft board and roster pages. An `Out` player's value is
  also treated as 0 in the lineup builder's recommendation, since they
  genuinely can't score that round.

See the in-app **How this works** page (linked in the nav once the app is
running) for the full mechanics — scoring model, projection method, what
every column/badge means, and how to keep the data fresh.

## PIR projection: features and weights

The live app uses `DEFAULT_CONFIG = CONTEXT_CONFIG` in
[`engine/projections.py`](engine/projections.py), with separate win-probability
and availability models. The following are the current defaults; experimental
Ridge/GBM models in `engine/ml_projections.py` are not used by the live app.

**1. Base PIR, conditional on playing.** The inputs are official historical
PIR, game order, season and position. The app blends all synced seasons;
each player's estimate uses their last **60 played games**, with a minimum
of **one**. DNPs are excluded here and handled by availability below.
For round-specific predictions, history ends strictly before the round's
start date, so late-played postponed games cannot leak into the estimate.
Prior-season playoff games are included by default.

| Input | Weight / effect |
|---|---|
| Historical PIR | Exponential recency weight: `0.5 ** (age_in_played_games / 10)`. Latest game has weight 1; 10 games back 0.5; 20 games back 0.25. These are relative weights, normalized when averaging. |
| Season age | Multiply each game's recency weight by `0.25 ** season_rank`: newest season in the eligible history 1, next older season 0.25, next 0.0625. Ranks use seasons present in that history. |
| Position | Shrink toward the Guard/Forward/Center mean with **one pseudo-game** (`shrink_k = 1`, target scale 1). The target uses all eligible played rows for that position, weighted by season age, without the player's 60-game cap or recency decay. |

```text
w_i = 0.5 ** (age_i / 10) * 0.25 ** season_rank_i
weighted_PIR = sum(w_i * PIR_i) / sum(w_i)
n_eff = sum(w_i) ** 2 / sum(w_i ** 2)
base_PIR = (n_eff * weighted_PIR + position_mean) / (n_eff + 1)
```

The player's history therefore gets `n_eff / (n_eff + 1)` of the estimate,
and the positional mean gets `1 / (n_eff + 1)`. With one played game that
is 50% / 50%; with effective sample size 10 it is about 91% / 9%.
These weights depend on the available history rather than fixed feature
percentages. Season discounting changes the relative mix of games; it does
not multiply the resulting PIR by 0.25.

**2. Matchup and expected win bonus.**
[`engine/win_prob.py`](engine/win_prob.py) uses each team's last **20 complete
games**, equally weighted across season boundaries, to calculate win share
and average point differential. The frozen logistic model is:

```text
z = 0.617960127
    + 0.072802830 * (home_win_share - away_win_share)
    + 0.952964634 * (home_mean_margin - away_mean_margin) / 10
P(home win) = 1 / (1 + exp(-z))
P(away win) = 1 - P(home win)
```

Win shares are fractions from 0 to 1; margins are points scored minus points
allowed. The intercept represents home advantage. Unknown teams default to
0.5 win share and zero margin. Coefficients were fitted on E2023/E2024.

Draft/transfer values use base PIR and a neutral win probability: average the
current club's home and away probabilities against an average club (0.5 win
share, zero margin). Lineup values use the scheduled opponent and add
**+0.340590633 PIR at home / -0.340590633 away** to base PIR, an adjustment
fitted on E2024 residuals. Opponent form affects the win probability; there
is no opponent-defense multiplier on base PIR. Transferred players use
their current club for bonus/context calculations.

```text
PIR_with_bonus = contextual_PIR * (1 + 0.10 * P(team win))
```

Thus a 70% win probability adds 7% to conditional PIR. Historical cutoffs
through **2025-05-25** retain the old last-10-team-games win-share bonus
and omit the home/away PIR adjustment, to avoid using coefficients fitted
on future outcomes.

**3. Availability and injury status.**
[`engine/availability.py`](engine/availability.py) estimates whether the
player will play at all, separately from how much PIR they produce if they
play. It uses the current club's last **5 games** within the player's
current stint, with exponential half-life **2 club games** and **one
pseudo-game at an 80% prior**:

```text
v_i = 0.5 ** (age_in_club_games_i / 2)
p_history = (sum(v_i * played_i) + 0.8) / (sum(v_i) + 1)
p_play = p_history * injury_multiplier
expected_value = PIR_with_bonus * p_play
```

`played_i` is 1 if the player played and 0 otherwise. Club games before the
player's stint do not count against them. With no current-club history,
the app uses 0.8 for an established player or a manual estimate, and 0.5
for a genuinely new, unestimated player.

| Injury status | Multiplier on probability of playing |
|---|---:|
| Out | 0 |
| Doubtful | 0.40 |
| Questionable | 0.60 |
| Uncertain / Game-time | 0.75 |
| Expected / Ready | 0.95 |
| No recognized status | 1.00 |

These injury multipliers are judgment values, not fitted coefficients.
Draft ranking, transfers and lineup decisions use `expected_value`, so the
win bonus and availability each apply once. A team without an eligible
scheduled game has zero round value; postponed games outside the round's
normal window are excluded. Captain (1.5x) and bench (0.5x) multipliers are
applied later by lineup scoring.

**Other displayed fields and exceptions.** Minutes trend is a weighted
historical average, not an input to base PIR; there is no minutes-times-PIR-
per-minute model in the live path. Volatility, last-3-game HOT/COLD form,
and the projection-change arrow do not adjust the PIR point estimate.
Box-score components (points, rebounds, assists, etc.) enter through
official PIR rather than separately weighted predictive features. A player
with no usable history remains at zero unless given a manual PIR estimate;
that estimate receives the same bonus and availability treatment.

## Quick start

```bash
# One-time setup (WSL/Linux; system Python has no pip, so use a venv)
python3 -m venv --without-pip .venv
.venv/bin/python3 <(curl -s https://bootstrap.pypa.io/get-pip.py)
.venv/bin/pip install -r requirements.txt

# Seed the 12 managers, then run the app
.venv/bin/python3 seed_league.py "Name1" "Name2" ... # all 12 names
.venv/bin/python3 app.py
```

Then open `http://127.0.0.1:5000` and use the **Sync** page to pull box
scores and current rosters (or run `sync_db.py`/`sync_rosters.py` directly
from the terminal — same effect, useful for a first full historical load
since that's a longer-running fetch):

```bash
.venv/bin/python3 sync_db.py --seasons E2023 E2024 E2025 E2026
.venv/bin/python3 sync_rosters.py --season E2026
.venv/bin/python3 sync_fantasy_pool.py --season E2026
.venv/bin/python3 sync_injuries.py --season E2026
```

A standalone pre-draft cheat sheet (no server needed) is also available:

```bash
.venv/bin/python3 draft_board.py --season E2025 --top-n 20
```

To measure projection quality and retune the estimator:

```bash
.venv/bin/python3 projection_eval.py --seasons E2025 --estimators legacy tuned --segments
.venv/bin/python3 tune_projection.py --season E2024   # tune on one season, evaluate on another
.venv/bin/python3 win_prob_eval.py                    # reproduce Phase 4 win-model fitting/evaluation
.venv/bin/python3 phase4_eval.py --trials-per-round 10 # paired Phase 4 acceptance checks
.venv/bin/python3 backtest_eval.py --seasons E2025 --config context # game context; legacy remains default
```

The Phase 4 evaluator uses blended history and historical availability on
identical sampled rosters. The older backtest still uses single-season
history and lacks full live availability parity; its default remains
`legacy` for reproducibility. `tuned` retains the Phase 2 estimator and old
win bonus; `context` uses Phase 4. The frozen win model falls back to the old
bonus for historical cutoffs through its training end date, 2025-05-25.

## Project layout

```
engine/         Data fetching, projections, roster/lineup/transfer logic
app.py          Flask web app (draft board, rosters, transactions, transfers, lineup, tracker)
draft_board.py  Standalone CLI draft cheat sheet
sync_db.py           Refreshes box-score history (run after each gameweek)
sync_rosters.py      Refreshes current club rosters (run before a draft / after transfer news)
sync_fantasy_pool.py Refreshes the real Fantasy draft pool from a Google Sheet (run whenever the sheet changes)
sync_injuries.py     Refreshes the basketnews.com injury report (run whenever you want current availability)
seed_league.py  One-time setup of the 12 managers
projection_eval.py   Projection-accuracy harness (MAE, calibration, ranking, paired bootstrap)
tune_projection.py   Tunes engine.projections.ProjectionConfig on one season
set_manual_projection.py  Set/clear/list manual projection estimates for players with no box-score history
docs/           Fuller technical notes, game rules, and a validation log
```

## Development checks

Run Python lint and tests from the repository root:

```bash
.venv/bin/python3 -m pip install ruff==0.17.1
.venv/bin/python3 -m ruff check .
.venv/bin/python3 -m pytest -q
```

`ruff.toml` enables core Python error checks (`E4`, `E7`, `E9`, `F`).

## Status

Actively developed. See `docs/testing_log.md` for a running record of what's
been validated and how.
