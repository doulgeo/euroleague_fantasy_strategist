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
wrong, and say so explicitly rather than silently editing). Also keep
`README.md` (the public-facing repo overview — what the tool does, quick
start) updated whenever a change would make it stale — new
features/commands, changed setup steps — unlike `docs/testing_log.md` this
is a living doc, not an append-only log: edit it in place to stay accurate.

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
- **Real 12-manager draft/ownership/transaction tracking**, as of
  2026-09-13: three new tables in `engine/db.py` (`managers`, `ownership` —
  materialized current state, `transactions` — append-only log), a new
  `engine/ownership.py` (record_draft_pick/record_free_agent_add/
  record_drop/record_trade, plus read helpers), and a local Flask app
  (`app.py`, server-rendered, no JS framework) with routes for the draft
  board (annotated with real owners, pick-logging form), manager rosters,
  transaction history (add/drop/1-for-1-trade forms), and transfer
  suggestions. `engine.transfers.suggest_transfers` gained an optional
  `owned_ids` param so it can use real ownership instead of the old "pool
  minus my own roster" stand-in (backward-compatible: `poc_run.py`'s call
  site is unchanged and still gets the old behavior). End-to-end validated
  including the core payoff — a player drafted to any manager stops
  appearing as a transfer suggestion for anyone else — see
  `docs/testing_log.md` → "Draft/ownership tracking + Flask app" for the
  full write-up. Run it with `python app.py`, seed the league first with
  `python seed_league.py "Name1" "Name2" ...` (12 names).
- **Current-roster sync + "new to the league" marking**, as of 2026-09-14:
  `EuroleagueClient.list_people`/`normalize_people` (`engine/data.py`) pull
  club rosters from the v2 `/people` endpoint — live current-state data
  (who's on which club right now), not disk-cached like box scores. A new
  `rosters` table (full delete-and-reinsert per sync, `engine/db.py`) and
  `sync_rosters.py` (the refresh command — run before a draft or after
  transfer news, not tied to gameweeks) keep it current.
  `engine/rosters.py::merge_roster` folds this into a projections pool: it
  corrects a transferred player's team (previously only updated once they
  played a box-score game for the new club) and adds a zero-value
  placeholder `Projection` for anyone on a roster with **no box-score
  history anywhere in the local DB** (E2023+), flagging them as new so
  they're visible-but-honestly-unranked rather than silently absent. Wired
  into `app.py` (`get_pool` now returns `(pool, new_ids)`) and
  `draft_board.py`'s CLI (`--roster-season`, `[NEW]` tags); a `NEW` badge +
  highlighted row renders in `templates/draft.html` and
  `manager_roster.html`. Live-tested against the real E2026 season: found
  and handled two real upstream data quirks first (a transferred player
  appears twice in `/people`, old-club-inactive + new-club-active; 2 of 332
  players briefly showed simultaneous dual-active rows) — see
  `docs/testing_log.md` → "Roster sync + new-to-the-league marking" for the
  full write-up, including validation that an entire club (Baskonia, `BAS`)
  correctly came back 100% "new," consistent with being new to EuroLeague
  this season rather than a join bug. Deliberately NOT built: any actual
  projected value for new players (still 0.0/unranked, honestly reflecting
  no data rather than a guess) — user is thinking through how to source
  additional context for that.

**Explicitly NOT done yet (all deferred, not forgotten):**
- The draft-tracking UI above is v1: no draft-credit/budget tracking
  (confirmed out of scope — credits don't affect in-season scoring), no
  undo button (fix mistakes via a compensating transaction or a direct
  `sqlite3` edit), and the trade form is strictly 1-for-1 (confirmed this
  league never does bigger trades). Revisit only if the user's actual usage
  says otherwise.
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
  revisiting with a richer feature set or more data.
- **Opponent-strength adjustment**: tried 2026-09-14, and it *was* the one
  feature the ML writeup above flagged as unattempted — same "no
  demonstrable win" verdict. `engine/team_strength.py` (per-team PIR-allowed,
  leakage-safe, same cutoff-round discipline as projections) sanity-checked
  soundly (E2025's stingiest-defense team was the actual champion), but
  `team_strength_backtest.py` found only a +0.050 correlation between
  opponent weakness and the existing projection's error, and the
  best-case MAE improvement from actually using it was ~0.13% — not worth
  the added complexity. Not wired into `build_projections`. Both files kept
  as standalone tools (useful on their own; reusable if a better-targeted
  opponent signal is tried later). See `docs/testing_log.md` → "Team
  strength index" for the full writeup, including one open question it
  doesn't resolve: whether the signal, though too weak for point-prediction
  accuracy, could still help as a narrow tiebreaker in the (not yet built)
  lineup builder's captain/swap choices — untested, since that UI doesn't
  exist yet.
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

- Actually run the real 12-manager draft through `app.py`/`seed_league.py`
  (replacing the current empty/test-cleared tables) and start logging real
  trades as they happen — the tool is ready for this now.
- A lineup-builder route/page (day-1/day-2 swap, captain choice) for a
  logged-in manager's roster — not built yet; `app.py` currently stops at
  draft/ownership/transfers, doesn't touch `engine.lineup`.
- The deferred UI/architecture questions in `docs/technical_notes.md`
  (hosting/deployment target — local-only was the working assumption for
  the draft-tracking app above, confirmed by the user for that feature,
  but full deployment target for the wider project is still open).
- `sync_db.py` and `sync_rosters.py` are both manual commands today ("run
  after each gameweek" / "run before a draft or after transfer news") -
  automating either trigger (cron/scheduled task) is a small later step, not
  urgent given trades/rounds are infrequent in this league.
- New-player valuation: once the user has an approach for sourcing
  additional context on players with no local box-score history (see
  "Current status" → roster sync entry), wire it into
  `engine/rosters.py::merge_roster`'s placeholder `Projection` instead of
  the current flat 0.0.

## Where things were left off (2026-09-14 session)

Nothing is mid-flight or running in the background (the local dev Flask
server from testing this session was left running in the background of
that session only — start fresh with `python app.py` for a new session) -
safe to pick up directly from any of the "Natural next steps" above, or
from scratch on something new. What happened this session, most recent
first:

1. UI polish on the Flask app, per the user's direction: added
   `README.md` (public-facing repo overview, now part of the working
   agreement above — keep it current) and a `/how-it-works` page (league
   rules, scoring, projection/VORP/tier/NEW-badge explanations, data-refresh
   commands — everything a user of the tool might need, linked from every
   page). Replaced the per-page column-explanation boxes from earlier this
   session with `title`-attribute tooltips on table headers instead (hover,
   no box taking up space). Added a team filter and sort-by-any-column
   control to the draft board (`app.py`'s `/draft` route), with tier breaks
   now only rendered in the default value-sorted view since they're not
   meaningful in other orderings. All routes re-verified 200 after the
   changes; sort/filter behavior spot-checked directly against the live
   E2026-seeded local DB (e.g. team=MAD filter, sort=team asc).
2. Built current-roster sync (`sync_rosters.py`, v2 `/people` endpoint) and
   "new to the league" marking (zero-value placeholder + `NEW` badge for
   any rostered player with no local box-score history), so transferred
   players show their real team and brand-new players are visible instead
   of silently absent from the draft board. Live-tested against the real
   E2026 season, including catching and handling two real upstream data
   quirks (duplicate transfer rows, transient dual-active rows) before they
   hit the DB. See "Current status" above and `docs/testing_log.md`'s most
   recent entry for the full write-up. New player *values* are explicitly
   NOT estimated yet — the user is thinking through how to source
   additional context for that; revisit when they have an approach.
3. Evaluated a user-supplied research document on EuroLeague data sourcing
   and PIR-prediction methodology against this project's own validated
   findings — mostly corroborated (same endpoints, same PIR formula, same
   field quirks), one correction (the doc conflated v2's confirmed deep
   historical coverage with the legacy Boxscore endpoint, which this
   project already proved is current-season-only), and the licensing/
   Sportradar section doesn't apply here (see "Licensing" above). The
   `/people` endpoint it surfaced is what led directly to item 2.
4. Earlier sessions (2026-09-10 through 2026-09-13): built the heuristic
   engine and corrected the roster/scoring model
   (`docs/technical_notes.md`), backfilled E2023-E2025 into `euroleague.db`,
   ran the elaborate multi-season backtest (current validated headline
   numbers are under "Current status" above), built `draft_board.py` +
   `engine.lineup.choose_active_squad` (real per-round exclusion logic,
   which raised the headline gain from +9.83 to +24.06 PIR), and built the
   12-manager draft/ownership/transaction tracking Flask app. Full detail
   in `docs/testing_log.md`.

If picking this up fresh: read this file top to bottom (it's short), then
skim the two most recent `docs/testing_log.md` entries for the current
state of validation - no need to re-read the whole log unless you want the
full history.
