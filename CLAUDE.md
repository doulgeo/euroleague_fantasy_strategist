# EuroLeague Fantasy assistant — project context

Personal (non-commercial) project: a tool to track EuroLeague Fantasy stats,
rank players by fantasy potential, and recommend lineups/transfers for a
12-manager private **draft-mode** league the user plays in with friends.
Licensing/ToS concerns are explicitly out of scope per the user — this will
never be monetized or redistributed.

**Location**: `/mnt/c/Users/pheno/Documents/code/euroleague-fantasy` (Windows
filesystem, accessed via WSL at that `/mnt/c/...` path — not under the WSL
home directory, so a filesystem search rooted at `/home` won't find it).

**Repo**: https://github.com/doulgeo/euroleague_fantasy_strategist.git
(`main` branch). Push access is via SSH (keypair at `~/.ssh/id_ed25519` in
WSL, public key added to the user's GitHub account already — pushes should
just work, no credential prompt). Repo-local (not global) git identity is
configured: `user.name=doulgeo`, `user.email=doulgerisgeo@gmail.com`.

**Working agreement with the user**: commit every finalized/significant
change as its own commit, right away — don't batch multiple finished
changes into one commit or wait for a natural stopping point. Keep
`docs/testing_log.md` updated with validation activity (what was tested,
how, result) as a running log separate from git history — append, don't
rewrite past entries (only correct them if a past entry turns out to be
wrong, and say so explicitly rather than silently editing).

Full context for a fresh session, in order of what to read:

1. **`docs/game_rules.md`** — the actual fantasy game rules (roster, scoring,
   captain, the turn-based Thu/Fri substitution mechanic, this league's
   specifics). Read this first — nothing else here makes sense without it.
2. **`docs/technical_notes.md`** — the data source (which API endpoints,
   confirmed field mappings, quirks, rate limiting), the engine architecture,
   the heuristic design rationale, and validated backtest results.
3. This file — quick orientation + current status + what's next.

## Current status (as of this writing)

**Done and validated:**
- Data layer (`engine/data.py`) confirmed against the real API: PIR
  recomputation matches the official value exactly, position taxonomy is
  clean (Guard/Forward/Center), historical depth back to E2000 confirmed.
- Heuristic recommendation engine (`engine/projections.py`, `roster.py`,
  `lineup.py`, `transfers.py`) built and backtested against real season
  results, on the **corrected** roster/scoring model (13-player roster,
  5G/5F/3C; 10 active per round with 3 excluded; three-tier scoring of
  starters/sixth-man/half-point-bench — see `docs/game_rules.md`). The
  day-1/day-2 swap logic is confirmed to never exceed the theoretical
  best-possible ceiling. As of 2026-09-13, `engine/lineup.py` also has real
  logic (`choose_active_squad`) for the one per-round decision that used to
  be random even in the "recommendation": which 3 of the 13 roster players
  to exclude. Brute-forces all 286 exclusion combos and scores each by
  projected value after the same swap logic used for the real
  recommendation, rather than a greedy cut, so it accounts for a benched
  player's day-2 swap option value. **Current validated headline (regular
  season only, 91 rounds across E2023-E2025, 2730 trials, real exclusion
  logic): beats or ties a no-swap baseline in 99% of trials (90% outright
  beat), +24.06 PIR mean gain per round (95% CI ±0.68), captures ~73% of
  the theoretical best-possible swap upside.** This supersedes the prior
  headline (random exclusion: 96% beat-or-tie, +9.83 PIR, ~50% captured) —
  real exclusion logic alone turned out to be a bigger lever than the
  swap logic itself: it raised the no-swap *baseline* by +26.5 PIR and the
  best-possible ceiling by +40.0 PIR before the swap does anything, simply
  by not randomly benching good players. See `docs/testing_log.md` → "Full
  backtest with real exclusion logic" for the full per-season table. The
  ML-vs-heuristic comparison (below) was rerun under this same real
  exclusion logic (2026-09-13) and the "no demonstrable win" conclusion
  held against the much stronger new baseline too — see "ML-vs-heuristic
  comparison rerun" in the testing log. Playoff rounds
  are excluded by default from this evaluation — this POC's random
  league-wide roster *sampling* (the 13-man roster itself, not the
  exclusion within it — no real ownership data exists yet) doesn't account
  for team elimination, which made an earlier playoffs-included run look
  artificially worse late in the season (see `docs/testing_log.md`,
  "Elaborate backtest" entry, for that methodology writeup).
- **Data persistence**: full box-score history for E2023-E2025 backfilled
  and loaded into `euroleague.db` (SQLite, `engine/db.py`) — 25,286
  player-game rows. `sync_db.py` is the ongoing incremental refresh command
  (re-run after each gameweek; cache-aware, idempotent upserts, safe to
  re-run anytime). `backfill.py` remains for one-time/historical bulk pulls.
- `poc_run.py` is a working end-to-end CLI: fetch → project → sample a
  roster + active squad → recommend a lineup/sixth-man/captain → suggest
  transfers → backtest against actual results.
- **Note for whoever picks this up**: the roster size/shape (originally
  believed 10/4-4-2) and the active-squad/scoring model (originally believed
  simple starter/bench) were both wrong initially and had to be corrected
  after the user caught it — see "Correction #2" in `docs/technical_notes.md`
  for the full story. Treat any *other* unconfirmed rule as similarly
  provisional until the user has explicitly verified it, especially ones
  stated early and never revisited.

**Explicitly NOT done yet (all deferred, not forgotten):**
- No real 12-manager ownership/draft tracking (who owns whom, transaction
  history). The POC's "free agents" = pool minus one sampled roster, a
  stand-in.
- No UI of any kind. The user wants one specifically for logging opponents'
  transfers/trades (manual entry, since trades are rare in this league and
  the commissioner enters draft results by hand) — not yet started.
- No ML model **in production** — this was tried (2026-09-12): Ridge
  regression and gradient-boosted trees (`engine/ml_projections.py`,
  `engine/ml_features.py`, `--projection-method {ridge,gbm,ensemble}` in
  `backtest_eval.py`), tuned via a hyperparameter grid search
  (`ml_grid_search.py`) and also tried as an equal-weight ensemble with the
  heuristic, evaluated with the same walk-forward backtest as the
  heuristic. None **demonstrably** beat the heuristic then, and this was
  reconfirmed 2026-09-13 after real exclusion logic (`choose_active_squad`,
  see below) raised the baseline substantially: tuned Ridge +24.60, GBM
  +24.39, ensemble +24.22, vs. heuristic +24.06 mean PIR gain — all within/
  near the ±0.67 95% CI, same conclusion as the original ±0.5-CI comparison
  (Ridge +13.51 vs heuristic +13.40) it superseded. See
  `docs/testing_log.md` → "Regression-model pivot", the grid-search/
  ensemble follow-up, and "ML-vs-heuristic comparison rerun" for the full
  writeup, including a cross-season-pooling hypothesis that was tested and
  disconfirmed, and a minimal-vs-full feature-set ablation showing the extra
  box-score detail (beyond what the heuristic itself uses) does measurably
  help. Heuristic remains the default; the ML code paths stay available for
  revisiting with a richer feature set (opponent strength wasn't attempted)
  or more data.
- Frontend approach, hosting/deployment target, and full historical backfill
  sequencing were raised as open questions earlier and explicitly deferred
  by the user ("let's see what data we get first") — still open, see
  `docs/technical_notes.md` → "Open questions / paused decisions".

## Quick start

```bash
cd euroleague-fantasy
# This WSL machine's system Python has no pip and is PEP-668
# externally-managed (no sudo) - use a project-local venv, bootstrapped
# without pip first so the system lock never applies to it:
python3 -m venv --without-pip .venv
.venv/bin/python3 <(curl -s https://bootstrap.pypa.io/get-pip.py)
.venv/bin/pip install -r requirements.txt
# then run everything via .venv/bin/python3 (e.g. .venv/bin/python3 poc_run.py ...)

# Explore raw data for a handful of recent games (fast, uses cache)
python explore_client.py --season E2025 --games 10

# Run the full engine POC (first run for a new round window fetches live data
# at ~6.5s/game - can take several minutes; reruns of the same window are
# instant, cached under raw/)
python poc_run.py --season E2025 --cutoff-round 20 --history-rounds 12 --validate-rounds 3
```

`raw/` is a gitignored on-disk cache of every API response ever fetched, keyed
by endpoint/season/game — safe to delete (just means the next run re-fetches
over the network), never needs to be committed.

## Natural next steps (not started, pick one)

- Real ownership/draft tracking: a place to record the 12-manager draft
  results and ongoing transactions, which is a prerequisite for real
  (non-stand-in) transfer suggestions.
- The deferred UI/architecture questions in `docs/technical_notes.md`.
- `sync_db.py` is a manual command today ("run this after each gameweek") -
  automating that trigger (cron/scheduled task) is a small later step, not
  urgent given trades/rounds are infrequent in this league.

## Where things were left off (2026-09-13 session)

Nothing is mid-flight or running in the background - safe to pick up
directly from any of the "Natural next steps" above, or from scratch on
something new. What happened this session, most recent first:

1. Added `draft_board.py` (per-position ranked draft cheat sheet, tiering,
   VORP) and `engine.lineup.choose_active_squad` (real logic for which 3
   of 13 to exclude each round, replacing a random stand-in), then reran
   the full validated backtest with the new exclusion logic - it turned
   out to matter far more than expected, raising the headline mean gain
   from +9.83 to +24.06 PIR. Reran the ridge/gbm/ensemble ML comparison
   under the same new logic per the user's request (expecting no change in
   verdict) - confirmed: still no demonstrable win over the heuristic, now
   against the stronger baseline. See `docs/testing_log.md`'s three most
   recent entries and "Current status" above for the details.
2. Set up the git repo (see "Repo" above), pushed everything to GitHub.
2. Ran an elaborate multi-season backtest with confidence intervals, a
   loss-case breakdown, and a rolling-window sensitivity check - in the
   process found and fixed a real methodology bug (playoff rounds were
   skewing results; now excluded by default). See
   `docs/testing_log.md` → "Elaborate backtest" for the full story and the
   current validated headline numbers (also summarized above under
   "Current status").
3. Backfilled E2023-E2025 and loaded it into `euroleague.db` (SQLite).
4. Original POC session (roster/scoring model corrections, base engine
   build) - see `docs/technical_notes.md` for that history.

If picking this up fresh: read this file top to bottom (it's short), then
skim the two most recent `docs/testing_log.md` entries for the current
state of validation - no need to re-read the whole log unless you want the
full history.
