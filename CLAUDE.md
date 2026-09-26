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
  logic, corrected substitution rule, formation-optimizing build_lineup):
  beats or ties a no-swap baseline in 82% of trials (74% outright beat, 18%
  lose), +10.62 PIR mean gain per round (95% CI ±0.53), captures ~23% of
  the theoretical best-possible swap upside.** **Stale as of 2026-09-22**:
  two corrections landed that day, after this number was computed, and
  neither has been folded back into a rerun yet - the captain multiplier
  was fixed from 2x to the real 1.5x, and `compute_round_score`'s actual
  scoring was found to be missing the real +10% team-win bonus entirely
  (it was only ever applied to *projected* values, never real ones - see
  `docs/testing_log.md`, "Actual scoring was missing the real +10%
  team-win bonus"). A third change followed on 2026-09-26:
  `swap_after_day1` now enforces the captain rule and does an exact final
  re-solve (see "Captain rule enforced" in the testing log). All three
  change the real point totals this headline is built from; treat this specific number as provisional until
  `backtest_eval.py` is rerun. This is also a much lower-looking
  number than the pre-2026-09-16 headline,
  and that's expected, not a regression — see the **2026-09-16 substitution
  rule correction** below, which fundamentally changed what "swapping" even
  means. Two corrections landed the same day, in order:
  1. **Sixth-man day-1-first fix**: the sixth-man slot was picked by raw
     projected value alone, letting a later-playing player grab it outright
     and forfeit its day-1 scoring opportunity entirely, while a real day-1
     candidate sat wasted on the bench. Fixed to follow the same
     day-1-first "golden rule" the 5 starters already did. In isolation
     this raised the then-headline from +24.06 to +29.67 PIR.
  2. **Substitution-rule correction (bigger, and the current headline's
     real driver)**: the user caught, live in the app, that the engine's
     core assumption — a full-scoring slot's day-1 points are "banked
     permanently," untouchable by a later swap — was simply wrong, and
     confirmed against the official rules and their own experience playing
     the real game: demoting an already-played starter/6th-man to the
     bench **halves** their already-earned points. Formation can also
     change at the swap window (not locked at day-1, another wrong prior
     assumption). This isn't a bug-severity fix like #1 — it's a genuine
     rule the engine was scoring against incorrectly the whole time, so the
     backtest ceiling itself dropped a lot (mean best-possible went from
     ~163 to ~149): a "swap" is a real bet with real downside now, not a
     free option, so a much smaller fraction of the (also now much smaller)
     theoretical upside gets captured. See `docs/testing_log.md` →
     "Corrected the day-1/day-2 substitution rule" for the full writeup,
     including why the swap *decision* also had to start using real day-1
     actual results (once synced) instead of pre-round projections for
     both sides — using projections throughout, as a naive port of the old
     logic did, produced a 31%-loss-rate, actually-worse-than-no-swap
     result once the halving cost was correctly modeled, because a
     wrongly-projected day-1 player could get wrongly demoted at real cost.
  3. **Formation-optimizing `build_lineup`** (same day, user-requested):
     the day-1 lock's formation choice previously used a "most day-1
     starters" proxy heuristic; now it simulates all three valid formations
     all the way through the eventual `swap_after_day1` outcome and picks
     whichever actually maximizes total projected PIR - directly optimizing
     rather than approximating. `build_lineup`/`swap_after_day1`/
     `choose_active_squad` also gained an optional `formation` param to
     force one shape throughout instead of auto-searching, wired into the
     `/lineup` page as a manual formation dropdown (default: Auto). This
     landed within noise of the #2 headline (+10.62 vs +11.11, well inside
     each other's ~±0.5 CI) - expected, since `swap_after_day1` already
     re-solves formation freely at the final step regardless of which one
     day-1 started with, so the initial choice only matters for *which*
     day-1 players get protected with a full slot, a smaller effect. Kept
     anyway since it's the more rigorous, directly-PIR-optimizing approach
     the user asked for, not just a heuristic proxy, and it's provably no
     worse. The `/lineup` page also gained a **Total Projected Score** box
     (no-swap total / recommended total / swap gain), using actual PIR for
     anyone whose game is already synced and projections otherwise - same
     values the swap decision itself uses.
  Before any of the three 2026-09-16 fixes, the prior headline (random exclusion: 96%
  beat-or-tie, +9.83 PIR, ~50% captured) — real exclusion logic alone
  turned out to be a bigger lever than the swap logic itself: it raised the
  no-swap *baseline* by +26.5 PIR and the best-possible ceiling by +40.0
  PIR before the swap does anything, simply by not randomly benching good
  players. See `docs/testing_log.md` → "Full backtest with real exclusion
  logic" for the full per-season table. The ML-vs-heuristic comparison
  (below) was rerun under real exclusion logic (2026-09-13) and the "no
  demonstrable win" conclusion held against that stronger baseline too —
  see "ML-vs-heuristic comparison rerun" in the testing log; **note this
  predates all three 2026-09-16 fixes and hasn't been rerun against them** —
  revisit if a precise ML-vs-heuristic number is needed again. Playoff
  rounds
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
  this season rather than a join bug. Deliberately NOT built then: any
  actual projected value for new players (still 0.0/unranked, honestly
  reflecting no data rather than a guess) — user was thinking through how
  to source additional context for that. Addressed 2026-09-20 via an
  on-demand manual-override workflow rather than an automated heuristic —
  see the "Manual projection overrides for brand-new players" entry below.
- **Lineup builder** (`/lineup`), as of 2026-09-14: wires `engine.lineup`'s
  real logic (`choose_active_squad` → `build_lineup` → `swap_after_day1`)
  into the app for a chosen manager and round — the 3-of-13 exclusion,
  day-1 starting lineup + captain, and the day-2 swap plan. Live,
  recomputed-every-visit, matching `/transfers`'s no-persistence pattern.
  Building this surfaced a real gap: there was no local data for a round
  that hasn't been played yet (`player_game_stats` is played-games-only).
  Fixed with a new `schedule` table (every game, played or not — full
  resync per season, populated by `sync_db.py` at no extra network cost)
  and `engine.lineup.team_dates_from_schedule`, a sibling to the existing
  (untouched) `team_dates_for_round`, validated to produce byte-identical
  output across all 47 rounds of E2025. End-to-end tested against the real,
  live E2026 round 1 (2026-09-25) — including tracing one surprising-looking
  exclusion (a 20.6-projected player benched) down to the correct reason:
  his club (Monaco) isn't part of the 2026-27 EuroLeague at all — which led
  directly to the next entry. See `docs/technical_notes.md` → "Season
  schedule" and `docs/testing_log.md` → "Wired the lineup builder into the
  app" for the full write-up.
- **"GONE" player marking**, as of 2026-09-14: the flip side of the NEW
  badge — a player with real history who's fallen off *every* current club
  roster (their club left the competition, e.g. Monaco this season — user-
  confirmed against the real 2026-27 club list — or they're unsigned/
  released) previously kept showing a stale team with a normal-looking
  projection, with nothing marking them as no longer part of the league.
  `engine.rosters.merge_roster` now returns a third value, `gone_player_ids`
  (`get_pool` is a 3-tuple; every call site updated). Per the user's
  explicit direction, these players **vanish from the draft board and
  transfer suggestions entirely** (not just flagged) but still show —
  visibly greyed out with a `GONE` badge, prompting a drop/trade — on a
  manager's own roster page if already owned. Live-tested including the
  "already owned" path (drafted one to a test manager directly, confirmed
  the greyed-out rendering, then cleaned up). See `docs/testing_log.md` →
  "\"GONE\" player marking" for the full write-up, including a related-but-
  separate gap found and *not* fixed this session (below).
- **Randomize-draft dev tool** (`/dev/randomize-draft`, a "Developer tools"
  section on `/sync`), as of 2026-09-14: `engine/dev_draft.py` wipes all
  current ownership and re-drafts every seeded manager a fresh, valid,
  exclusive 13-player roster (weighted-random, reusing
  `engine.roster._weighted_sample_without_replacement`). Explicitly
  dev/testing only, labeled as such in the UI — requested so there's
  always a realistic full 12-manager league to develop/test the lineup
  builder etc. against without hand-drafting 156 players. Live-tested for
  real: 156 picks across the 12 seeded managers, then immediately exercised
  `/lineup` against the fresh roster to confirm the full "randomize a
  draft, then predict lineups" loop the user asked for actually works
  end-to-end in the running app. Left in place afterward (unlike other test
  data this session) — populating a working league is this tool's actual
  purpose.
- **Bulk draft import from a CSV export**, as of 2026-09-16: the user's
  real draft will happen inside a friend's draft-room app, which exports a
  CSV of the completed draft — `engine/draft_import.py`
  (`parse_draft_csv`/`distinct_managers`/`resolve_manager_picks`) plus
  three `app.py` routes (`/draft/import` upload page with drag-and-drop +
  file picker, `/draft/import/preview` shows a manager-matching step,
  `/draft/import/commit` writes the picks) let the user upload that export
  instead of logging 156 picks by hand. Player identity is resolved by
  reusing `engine.fantasy_pool.resolve_pool_rows` and `TEAM_CODE_MAP`
  as-is (the export's team codes were confirmed to match the Fantasy
  sheet's own codes exactly) — same normalized-name matching, same
  synthetic-ID fallback for a genuinely unresolved player. The parser reads
  the pick-log section of the export (one row per pick) and stops cleanly
  before an optional "Final rosters" trailer some exports append, without
  needing to special-case it. The commit step maps each CSV manager name
  to a real manager (auto-suggested on exact name match, editable or
  skippable) with an optional "clear existing ownership first" checkbox
  for the initial-teams-import case. Live-tested end-to-end against the
  real running app/DB using the user's own sample export (48 picks/6
  managers): all 48 resolved to real player_ids with zero synthetic
  fallbacks (including tricky cases like suffix/hyphenated-surname
  matches), and the imported picks correctly showed up as drafted (with
  the right owner) on the real `/draft` and `/managers/<id>` pages. See
  `docs/testing_log.md` → "Bulk draft import from a draft-room app's CSV
  export" for the full write-up. Not yet tested against a real (non-mock)
  export from the friend's app — revisit if that shape turns out to
  differ from the mock export this was built against.
- **Manual projection overrides for brand-new players**, as of 2026-09-20:
  a player with zero box-score history anywhere (E2023+) — e.g. a
  mid-season transfer from another league — previously got a flat 0.0
  placeholder from `engine.rosters.merge_roster` with no way to give them a
  real score, since there's no local data to compute one from. The user
  proposed a workflow rather than an automated heuristic: ask a Claude Code
  session to research such a player (web search on recent form/role/other-
  league stats) and propose a projected PIR, then persist that number.
  Built: a `manual_projections` table (`engine/db.py`:
  `set_manual_projection`/`clear_manual_projection`/`load_manual_projections`),
  `merge_roster` substitutes a manual value in place of 0.0 when present
  (flagged separately as `estimated_player_ids`, distinct from
  `new_player_ids`), and `set_manual_projection.py` is the plain CLI to set/
  clear/list overrides — no research logic of its own, that's the Claude
  session's job on request. `app.py::get_pool` is now a 4-tuple (all 7 call
  sites updated); `draft.html`/`manager_roster.html` show an `EST` badge
  next to `NEW` when a projection came from a manual override.
  Deliberately NOT built: any automated in-app LLM call (would need an API
  key + per-call cost, a real new piece of infrastructure the user declined
  in favor of the on-demand workflow) and the separate multi-season-fallback
  gap (a player with history in an *older* season, not this one — still the
  open "known gap" below, unaffected by this change). See
  `docs/testing_log.md` → "Manual projection overrides for brand-new
  players" for the full write-up, including the live round-trip test
  against a real E2026 roster player.
- **"Pitch view" on `/lineup`**, as of 2026-09-16: a Biwenger-style court
  graphic (5 starters positioned by role — Center/Forward/Guard — plus a
  checkmarked bench column with the 6th man tagged) rendered above each of
  the existing Day 1/Day 2 tables, via a new `templates/_pitch.html` macro
  and matching CSS in `static/style.css`. Built here rather than on the
  Managers tab (the user's other suggested spot) because it's a 1:1 visual
  match for `/lineup`'s existing per-round `initial`/`recommended` lineup
  objects, which the raw 13-man `/managers/<id>` roster has no equivalent
  of. No real player photos: confirmed live that the EuroLeague `/people`
  endpoint returns an empty `images: {}` for every player, so avatars are
  initials-based monograms instead. See `docs/testing_log.md` → "'Pitch
  view' visual on the lineup builder" for the full write-up, including the
  one open item: this was verified structurally (curl + HTML assertions,
  no headless-browser tooling available in this sandbox), not yet
  eyeballed in an actual browser.
- **Injury report scraping (basketnews.com)**, as of 2026-09-20: the user's
  idea — `engine/injuries.py` scrapes basketnews.com's daily-updated
  EuroLeague injury report (plain server-rendered HTML, confirmed live —
  not part of the EuroLeague API, so treated as best-effort) into a new
  `injuries` table (`sync_injuries.py`, same "full snapshot replace, resolve
  player_id live at read time" pattern as `engine.fantasy_pool`, reusing
  `resolve_pool_rows` exactly as `engine.draft_import` does). Only the
  `Out` status actually changes anything: `app.py`'s `/lineup` route zeroes
  that player's decision value rather than hard-excluding them (degrades
  gracefully if more than 3 roster players are `Out` at once, and needed no
  changes to `engine/lineup.py` itself, since the existing brute-force
  search already handles "worth 0" correctly on its own) — every other
  status (Doubtful/Questionable/Uncertain/Game-time/Expected/Ready) is
  shown as an informational badge only (draft board, manager roster, all
  three lineup-builder tables), deliberately not turned into a numeric PIR
  discount. `app.py::get_pool` is now a 5-tuple (all 6 call sites updated).
  Live-tested against the real page and the real E2026 DB: 30/30 players
  scraped, 29 resolved to a real player_id (1 unresolved — "Jimmy Clark
  III", a known suffix-splitting edge case shared with
  `engine.draft_import`, not fixed), and — the actual payoff — a real
  manager's real 11.0-projected `Out` player (fractured hand) correctly got
  excluded by `/lineup` ahead of two teammates projected at 3.8 and 1.8,
  confirming the zeroed value overrides raw projection in the real
  brute-force selection, not just in isolation. See `docs/testing_log.md` →
  "Injury report scraping (basketnews.com)" for the full write-up.
- **Watchlist + transfer PIR compare tool**, as of 2026-09-23: a personal,
  global (not per-manager — confirmed with the user, this is their own
  planning tool) shortlist of players, via a new `watchlist` table
  (`engine/db.py`, same style as `manual_projections`) and
  `add_to_watchlist`/`remove_from_watchlist`/`load_watchlist` helpers.
  Toggled with a ☆/★ button next to every player on `/draft`, reviewed on a
  new `/watchlist` page, and flagged with a ★ badge wherever a watched
  player appears elsewhere (draft board, `/transfers`). Separately,
  `engine/transfers.py` gained `projected_gain(drop, add)`, extracted from
  the delta calculation `suggest_transfers` already did internally for its
  automatic same-position suggestions, and exposed on `/transfers` as a
  manual "Compare a transfer" tool, not restricted to the `MIN_UPGRADE_GAP`
  threshold like the automatic suggestions. Live-tested end-to-end against
  the real, fully-drafted E2026 DB: watch/unwatch round-tripped correctly
  on both `/draft` and `/watchlist`, and the compare tool's displayed gain
  matched an independently-computed delta exactly while the existing
  automatic suggestions table kept rendering the same numbers after the
  `projected_gain` refactor. **Reworked same-day** per direct user
  feedback on the initial dropdown version: two same-position dropdowns let
  you pick a mismatched position with no warning, and silently showed only
  free agents. Replaced with a two-step table flow — pick a roster player
  to drop from a table, which then shows every *same-position* player
  leaguewide as Add candidates (a new `engine.ownership.owner_map` bulk
  query resolves each one's owning manager, shown in an **Owner** column,
  or "Free agent") — position mismatches are no longer possible since
  there's nothing else to pick, and real ownership is visible instead of
  hidden. Re-verified against the same real DB: all 110 Forward candidates
  for a real drop independently confirmed as Forward (0 mismatches, also
  checked for Center), real owner names resolved correctly, and the result
  box still computed the same delta as before. **Reworked again same day**:
  the user preferred dropdowns back over the tables ("better for the
  eyes") and wanted the result box shown above the pickers, not below.
  Final shape: the result box renders first, then a Drop `<select>` (all
  13 roster players) and, once one's picked, an Add `<select>` still
  scoped to that position, each option's text carrying the owner info the
  table's Owner column used to show (`"... - <manager name>"` or
  `"... - Free agent"`) plus a `★ ` prefix for watchlisted players - same
  auto-submit-on-change pattern as every other filter dropdown in the app.
  No `app.py` changes needed for this last pass, only the template. See
  `docs/testing_log.md` → "Watchlist + transfer PIR compare tool",
  "Transfer compare tool: position-filtered table, owner shown", and
  "Transfer compare tool: back to dropdowns, result on top" for the full
  write-ups.

- **Points tracker** (`/tracker`), as of 2026-09-26: the user records the
  lineup they *actually* played each round (a day-1 lock plus the final
  post-swap lineup, a slot for all 13 players), independent of `/lineup`'s
  suggestion. It only tracks the user's own team, set once and stored in a
  new `app_settings` table. Scores are never stored: they're recomputed from
  `player_game_stats` via `engine/tracker.py`. Each round shows:
  - my score, and the no-swap score (the day-1 lineup, never swapped);
  - the tool's suggestion, snapshotted when the user saves (`tool_day1` /
    `tool_final`);
  - the exact hindsight-best lineup, brute-forced over 286 exclusions × 3
    formations. This is exact because any lineup could have been locked in
    on day 1;
  - an optional official in-game total, with its difference from our score.

  Plus a season cumulative inline-SVG chart. Tables: `tracked_lineups`,
  `tracked_lineup_players`, `tracked_rounds`, `app_settings`. Other changes:
  - `/lineup`'s logic was extracted into `app.py::compute_recommendation`
    (verified byte-identical output), and it plus `get_pool` gained a
    `before_round` cutoff. The tracker always uses it, so a suggestion
    snapshotted after a round was synced never sees that round's results.
  - `final_rule_violations` rejects a final lineup that breaks a swap rule
    relative to the saved day 1 (hard restriction since 2026-09-26, per the
    user).
  - The round table shows PIR+bonus *and* Pts (after the slot multiplier)
    per player. Showing only the pre-multiplier value made the user think a
    32-PIR captain was scored as 32 rather than 48.

  Tested with `tests/test_tracker.py` plus an end-to-end run on a DB copy.
  See `docs/testing_log.md` → "Points tracker (`/tracker`)".
- **Two data fixes, 2026-09-26** (found the day after E2026 round 1):
  - `EuroleagueClient.list_games` no longer serves the season game list
    from disk cache. A pre-season cache had frozen every game as unplayed,
    so `sync_db.py` never fetched round 1.
  - Once round 1 was synced, the live pool's hard switch to the current
    season zeroed ~every projection (the 3-game minimum). Live projections
    now blend E2025 + E2026 as one timeline
    (`engine.projections.blend_season_rows`).

  See the two 2026-09-26 entries in `docs/testing_log.md`.
- **Captain rule in `swap_after_day1`**, 2026-09-26: the captaincy can only
  stay with the day-1 captain or move to a player who hasn't played yet.
  The final re-solve is now exact (formation × legal captain, scoring
  starters + sixth + captain), checked against a brute force of every legal
  final lineup (`tests/test_lineup.py`).
- **Pages can be screenshotted for real**: headless Chrome from the Windows
  install works from WSL. Run
  `"/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new
  --screenshot='C:\...\x.png' --window-size=1200,1400 http://localhost:<port>/...`
  against a `flask --app app run --port <port>` server, then view the PNG.
  Use this instead of structural-only HTML checks.

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
  accuracy, could still help as a narrow tiebreaker in the lineup builder's
  captain/swap choices (the lineup builder itself now exists — see below —
  but this specific tiebreaker idea hasn't been tried against it).
- **Projected-minutes / rigorous team-strength features (`proj/` package)**:
  tried 2026-09-21, at the user's request, as a much more rigorous
  follow-up attempt at both a minutes-allocation model and the
  opponent-strength idea above. Full pipeline built and validated against
  real data — availability/p_active, an injury-down-weighted minutes
  prior with a ridge correction, water-filling/Monte-Carlo team
  allocation, coach-rotation concentration, and (separately) ridge-
  regularized adjusted offense/defense ratings with a preseason
  net-rating projection. **Neither feature cleared its acceptance bar**:
  the minutes pipeline is worse than a naive "last season's own rate"
  baseline overall (driven by the newcomer fallback — no depth-chart data
  exists anywhere in this project, per `reports/data_audit.md` §2 — and a
  ridge correction that overfits the single available training
  transition), and the team-strength projection loses
  to a flat "predict league average" baseline on both available season
  transitions, independently reconfirming the `engine/team_strength.py`
  verdict above with a much more careful method. One isolated real win
  worth remembering: the minutes ridge's `team_changed` feature gives a
  genuine ~20% MAE improvement specifically for players who switched
  teams. Not wired into `engine/`/`app.py`
  (`config.yaml`'s `integration.use_new_features` stays `false`) — kept
  as standalone, tested, unintegrated infrastructure, same posture as
  `engine/team_strength.py`. Full detail across four reports:
  `reports/data_audit.md`, `reports/milestone2_backtest_baselines.md`,
  `reports/milestone3_minutes.md`, `reports/milestone4_team_strength.md`.
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
- The deferred UI/architecture questions in `docs/technical_notes.md`
  (hosting/deployment target — local-only was the working assumption for
  the draft-tracking app above, confirmed by the user for that feature,
  but full deployment target for the wider project is still open).
- `sync_db.py` and `sync_rosters.py` are both manual commands today ("run
  after each gameweek" / "run before a draft or after transfer news") -
  automating either trigger (cron/scheduled task) is a small later step, not
  urgent given trades/rounds are infrequent in this league.
- New-player valuation is done as an on-demand manual-override workflow
  (see "Manual projection overrides for brand-new players" in "Current
  status") rather than an automated heuristic — use it: when a NEW player
  shows up, ask a Claude Code session to research them (web search on
  recent form/role/other-league stats), then run `set_manual_projection.py`
  with the agreed number. The separate multi-season-fallback gap (below)
  is still open.
- The lineup builder itself is still live-recomputed only. What the user
  actually picks is now persisted separately, by the points tracker
  (`/tracker`, 2026-09-26), for the user's own team only.
- The team-strength tiebreaker question the "Opponent-strength adjustment"
  entry above leaves open is now actually testable, since `/lineup` exists
  — worth trying if opponent-aware lineup decisions come up again.
- **Known gap, found but not fixed 2026-09-14**: `known_player_ids` (the
  NEW-badge check) looks across ALL locally-synced seasons (E2023-E2025),
  but `build_projections` (the actual value) only uses the prior + current
  season (blended since 2026-09-26, `app.py::_load_pool_rows`), so history
  from E2023/E2024 alone never counts. A player with history
  in an *earlier* season but not the one currently used for projections
  (found via real examples: `ZIZIC, ANTE`, `DEJULIUS, DAVID`, both on
  Besiktas's real E2026 roster) is correctly not flagged NEW, but still
  gets an unhelpful flat 0.0 with nothing distinguishing that from
  "genuinely brand new." A third case, distinct from NEW and GONE — no
  design decided yet on how (or whether) to surface it.

## Where things were left off (2026-09-14 session)

Nothing is mid-flight or running in the background (the local dev Flask
server from testing this session was left running in the background of
that session only — start fresh with `python app.py` for a new session) -
safe to pick up directly from any of the "Natural next steps" above, or
from scratch on something new. What happened this session, most recent
first:

1. Added a randomize-draft dev tool (`/dev/randomize-draft` on `/sync`) so
   there's always a realistic full 12-manager league to develop against —
   see "Current status" above. Live-tested the full loop it exists for:
   randomized a real draft (156 picks, 12 managers), then immediately hit
   `/lineup` against it to confirm the "randomize a draft, then predict
   lineups" flow actually works end-to-end.
2. Added "GONE" player marking, prompted directly by the user spot-
   checking item 3's lineup-builder output against real domain knowledge
   (Monaco isn't in the 2026-27 EuroLeague) — see "Current status" above
   for the full summary. Also resolved a "where are the Besiktas players"
   question along the way (they're there, mostly just low-value/sorted to
   the bottom) and found — but deliberately did not fix — a related,
   distinct gap (see "Explicitly NOT done yet" above).
3. Wired the lineup builder into the app (`/lineup`) — see "Current
   status" above for the full summary. Found and fixed a real
   architectural gap along the way (no local schedule data for
   not-yet-played rounds), added a `schedule` table + `sync_db.py`
   extension + `engine.lineup.team_dates_from_schedule`, and validated all
   of it against the real, live 2026-27 schedule (confirmed already
   published: 380 games, round 1 on 2026-09-25) rather than only
   historical data. Used the `/plan` workflow for this one (two Explore
   agents to map `engine.lineup`/`engine.roster`/`poc_run.py` and
   `engine.ownership`/`engine.db`/`app.py` conventions, then a Plan agent
   for the file-by-file design) given the scope. Test draft data used to
   validate it was cleaned up afterward.
4. Brainstormed a team-strength/opponent-adjustment idea at the user's
   request, then prototyped and backtested it (`engine/team_strength.py`,
   `team_strength_backtest.py`) — no demonstrable predictive win, not wired
   into projections; see "Explicitly NOT done yet" above.
5. Added a `/sync` page: buttons to trigger `sync_db.py`/`sync_rosters.py`
   from the browser (background subprocess per script, log tail, an
   auto-refreshing status view, a same-kind-already-running guard) instead
   of only from the terminal. Live-tested end-to-end via real HTTP triggers
   of both scripts, watched them complete and the DB update.
6. UI polish on the Flask app, per the user's direction: added
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
7. Built current-roster sync (`sync_rosters.py`, v2 `/people` endpoint) and
   "new to the league" marking (zero-value placeholder + `NEW` badge for
   any rostered player with no local box-score history), so transferred
   players show their real team and brand-new players are visible instead
   of silently absent from the draft board. Live-tested against the real
   E2026 season, including catching and handling two real upstream data
   quirks (duplicate transfer rows, transient dual-active rows) before they
   hit the DB. See "Current status" above and `docs/testing_log.md` for the
   full write-up. New player *values* are explicitly NOT estimated yet —
   the user is thinking through how to source additional context for that;
   revisit when they have an approach.
8. Evaluated a user-supplied research document on EuroLeague data sourcing
   and PIR-prediction methodology against this project's own validated
   findings — mostly corroborated (same endpoints, same PIR formula, same
   field quirks), one correction (the doc conflated v2's confirmed deep
   historical coverage with the legacy Boxscore endpoint, which this
   project already proved is current-season-only), and the licensing/
   Sportradar section doesn't apply here (see "Licensing" above). The
   `/people` endpoint it surfaced is what led directly to item 7.
9. Earlier sessions (2026-09-10 through 2026-09-13): built the heuristic
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
