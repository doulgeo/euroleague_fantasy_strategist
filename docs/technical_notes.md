# Technical notes: data source, engine design, validated results

## Data source

Two public (undocumented, unauthenticated) official EuroLeague endpoint
families were evaluated; the choice and every quirk below was confirmed by
actually hitting the endpoints, not just read from documentation.

**Primary: `api-live.euroleague.net/v2`**
```
GET https://api-live.euroleague.net/v2/competitions/{E|U}/seasons/{seasonCode}/games?limit=1000
GET https://api-live.euroleague.net/v2/competitions/{E|U}/seasons/{seasonCode}/games/{gameCode}/stats
```
- `E`/`U` = EuroLeague / EuroCup competition code. `seasonCode` e.g. `E2025`
  = the 2025-26 EuroLeague season.
- `games?limit=1000` returns the *entire* season's schedule in one call
  (envelope `{"data": [...], "total": N}`) — `limit` isn't capped low, no
  pagination/offset needed in practice. Each game entry has `local`/`road`
  club codes, `round`, `date` (ISO), and `local.score`/`road.score` (usable
  for a team win/loss signal).
- `.../games/{gameCode}/stats` returns `{"local": {...}, "road": {...}}`,
  each with `coach`, `players[]` (each `{player: {person, position,
  positionName, club, ...}, stats: {...}}`), `team`, and `total` (team
  totals row — separate key, not mixed into `players[]`).
- Per-player `stats` fields used: `timePlayed` (**seconds**, not `MM:SS`),
  `valuation` (= official PIR), `points`, `fieldGoalsMade2/Attempted2`,
  `...Made3/Attempted3`, `fieldGoalsMadeTotal/AttemptedTotal`,
  `freeThrowsMade/Attempted`, `totalRebounds`, `offensiveRebounds`,
  `defensiveRebounds`, `assistances`, `steals`, `turnovers`, `blocksFavour`,
  `blocksAgainst`, `foulsCommited` (sic), `foulsReceived`, `plusMinus`,
  `startFive` (bool, starter flag), `dorsal`.
- **Position taxonomy confirmed**: `player.position` = `1`→Guard, `2`→Forward,
  `3`→Center (also human-readable in `positionName`). Maps directly onto the
  roster's position requirements (see `game_rules.md` for the actual counts —
  5 Guard/5 Forward/3 Center, corrected from an earlier wrong 4-4-2 belief).
- **Historical depth confirmed to E2000** by actually fetching `E2000` games
  and box scores — real data came back (e.g. Ginobili's 2000-01 Virtus
  Bologna line), 0 PIR-recompute mismatches.

**Fallback/cross-check only: `live.euroleague.net/api/Boxscore`**
```
GET https://live.euroleague.net/api/Boxscore?gamecode={game_code}&seasoncode={season_code}
```
- `Stats[].PlayersStats[]` per team, fields: `Player_ID` (has trailing
  whitespace — strip it), `Minutes` (`"MM:SS"` or literal `"DNP"` string),
  `Points`, `FieldGoalsMade2/Attempted2`, etc., `Valuation`, `Plusminus`.
  Team-totals pseudo-row (`tmr`/`totr`) is a **separate key**, not embedded
  in `PlayersStats` — no filtering needed there.
- **No `position` field at all** in this response — legacy-sourced rows get
  `position=None`, which the roster/lineup logic can't use. Not an issue in
  practice since v2 is primary and this is rarely invoked (0 fallbacks
  across every real run so far).
- **Confirmed NOT a historical source**: returns `HTTP 200` with an
  **empty body** (not 404) for old seasons — tested directly on `E2000`
  game 169, zero content length. Works fine on current-season games. The
  original research report implied this had deep historical coverage too;
  that turned out to be wrong. `engine/data.py`'s `_get_json` treats an
  empty 200 body as "no data" (returns `None`) precisely because of this.

**PIR formula** (verified to reproduce `valuation`/`Valuation` exactly,
0 mismatches across every game checked so far):
```
PIR = PTS + REB + AST + STL + BLK + FoulsDrawn
    - (FGA_total - FGM_total) - (FTA - FTM)
    - TO - BlocksAgainst - FoulsCommitted
```
(`engine/data.py::recompute_pir`)

**Rate limiting**: no published quota. Client paces itself at one request
per ~6.5s, retries on 429 honoring `Retry-After` if present, backs off on
5xx. A full ~400-game season backfill would take ~40+ minutes cold; POC runs
instead bound themselves to a round window (`fetch_season(min_round,
max_round)`) to keep first-run latency reasonable — e.g. rounds 8-23
(~160 games) took ~17 minutes cold, then is instant on every rerun since
every response is cached to disk under `raw/` (gitignored, safe to delete).

**Licensing**: explicitly not a concern per the user — personal/friends-only,
never monetized or redistributed.

## Engine architecture

```
engine/
  data.py         EuroleagueClient (HTTP + cache + pacing), normalize_v2/
                  normalize_legacy (raw JSON -> flat row dict), recompute_pir,
                  fetch_season (bulk fetch + normalize for a round window)
  projections.py  build_projections: leakage-safe heuristic per-player
                  projection as of a cutoff round
  roster.py       Roster (13-player, 5G/5F/3C, validated dataclass),
                  ActiveSquad (10-of-13 per-round selection, 3 excluded,
                  validated against formation feasibility), sample_roster +
                  sample_active_squad (POC-only samplers)
  lineup.py       build_lineup, swap_after_day1, compute_round_score - the
                  three-tier (starter/sixth-man/bench) scoring model and
                  the day-1/day-2 golden-rule swap logic
  transfers.py    suggest_transfers - naive same-position upgrade finder
```
`explore_client.py` and `poc_run.py` are thin CLIs on top of `engine/`.

### Projections (`engine/projections.py`)

Deliberately simple and transparent — no ML. Per player, using only games
**strictly before** the cutoff round (leakage safety is load-bearing: this is
what makes the POC's backtest meaningful rather than circular):
- Recency-weighted rolling mean PIR over the last 10 played games (linear
  ramp weights 1..n, not exponential — simple and legible over tuned).
- Same weighting for a minutes trend.
- Volatility = sample stdev of recent PIR (floor/ceiling context, not
  currently consumed by lineup logic but there for future captain-risk
  reasoning).
- Team win-rate over the team's last 10 games, as a stand-in win-probability
  feeding the +10% win bonus: `projected_pir_with_bonus = projected_pir +
  team_win_rate * 0.10 * projected_pir`.
- Players with fewer than 3 prior games are excluded (too little signal).

Unaffected by the roster/active-squad rules correction below — this module
is position/roster-agnostic.

**Why heuristic-first, not ML**: agreed with the user early on. EuroLeague
has far less data volume than NBA (a season is ~340-400 games total, not
30k+), so a from-scratch ML model risks overfitting noise. The plan is:
prove a transparent, hand-auditable heuristic is actually useful (this POC),
*then* decide whether a regression/tree model demonstrably beats it before
adding that complexity. Nothing has crossed that bar yet.

### Roster & active squad (`engine/roster.py`)

Two distinct concepts, easy to conflate:

- **`Roster`**: the full 13-player squad (5 Guard/5 Forward/3 Center,
  validated in `__post_init__`). Changes rarely (only via rare trades).
- **`ActiveSquad`**: which 10 of those 13 are "in" for a given round, with
  the other 3 explicitly tracked as `excluded` (score zero, can't be
  swapped in). Validated to retain at least 2 Guards/1 Forward/1 Center
  active — the minimum needed to field *any* valid starting formation.

`sample_roster` and `sample_active_squad` are both explicitly POC-only.
`sample_active_squad` weights *toward* excluding lower-projected players
(a real manager wouldn't randomly bench their stars) and retries on an
invalid draw. **Real rosters and real exclusion choices** will eventually
come from actual 12-manager draft/ownership data and actual manager
decisions — neither exists yet (see CLAUDE.md).

### Lineup, the tiered scoring model, and the day-1/day-2 mechanic (`engine/lineup.py`)

This is the most subtle part of the whole engine, and it went through a
significant correction — see "Second correction" below before assuming
anything here is simpler than it looks.

**Three-tier scoring**, all drawn from the 10-player `ActiveSquad`:
- 5 **starters** (one of the three valid formations — see `game_rules.md`),
  full points, one of them **captain** (2x).
- 1 **sixth man**, full points, never captain-eligible.
- 4 **bench**, **half points** — scored automatically whether or not they
  were ever swapped in. This is the single biggest behavioral difference
  from the old (wrong) model, where non-swapped bench scored zero.

**Key functions:**
- `team_dates_for_round(rows, round_no)`: which calendar date each team
  plays on, in a given round — derived from real game dates in the data,
  **not** hardcoded to Thursday/Friday, because real schedules have
  reschedules (e.g. round 33 of E2025 had games on both 2026-03-24/25 *and*
  2026-03-31).
- `_availability_rank`: 0 = plays on the round's earliest date ("day 1"),
  1 = plays later ("day 2"), 2 = not playing this round at all.
- `build_lineup(active_squad, team_dates, value_fn)`: tries all three valid
  formations (2-2-1, 2-1-2, 3-1-1), fills each preferring rank-0 (day-1)
  players first per the golden rule then by `value_fn` descending, and picks
  whichever formation yields the best (day-1 coverage, then total value)
  outcome. Sixth man = best remaining player by `value_fn`, any position.
  Captain = highest `value_fn` among the five starters only. `value_fn` is
  injected so the same code drives both the real projection-based
  recommendation and a hindsight/actual-results pass for backtesting.
- `swap_after_day1(lineup, team_dates, value_fn)`: for each of the 6
  full-scoring slots (5 starters + sixth man) whose occupant already played
  day 1, finds the best same-position bench player who plays later and
  swaps them in **if `value_fn` on them is `> 0`** — promoting them from
  half points to full points for their still-upcoming game. Captain is
  recomputed over the resulting five starters afterward (sixth man still
  never captain-eligible).
- `compute_round_score(initial, final, team_dates, actual_pir)`: day-1
  scoring comes from `initial` (whoever occupied which tier when those games
  were actually played), day-2/later scoring comes from `final` (post-swap).
  Each player's contribution is multiplied by their tier at the relevant
  stage: 2x if that stage's captain, 1x if starter/sixth-man, 0.5x if bench.
  Captain doubling is per-stage (`initial.captain` for day-1,
  `final.captain` for day-2) so reassigning captain at the swap window only
  affects not-yet-banked points.

**Assumptions made where rules weren't fully pinned down** (see also the
docstring at the top of `lineup.py`):
- The sixth-man slot has a position "type" fixed at whoever holds it, same
  as a starter slot — a swap into it must match that position.
- The starting formation *shape* (which of the three valid combos) is fixed
  at the initial day-1 lock and doesn't get reshuffled at the swap window —
  only *who* fills an already-typed slot can change.
- Captain reassignment at the swap window is allowed but only affects
  points not yet banked (see `compute_round_score`'s per-stage doubling) —
  **confirmed by the user 2026-09-13** (previously this was an unconfirmed
  best-guess interpretation).

### Bug found and fixed #1: swap scoring discarded banked points

First implementation scored lineups by summing the **final** (post-swap)
lineup's players' full actual PIR, full stop. That's wrong: it silently
discarded whatever a swapped-out day-1 player had already earned. Symptom
was concrete and reproducible: "engine-recommended" scored *worse* than
"no-swap" on a real backtest round — the swap had correctly captured a bench
player's day-2 points but the scoring function threw away the day-1
captain-doubled output of the player who got swapped out.

Fixed by splitting scoring into day-1 (from `initial`) + day-2 (from
`final`), **and** adding a `value_fn > 0` guard in `swap_after_day1`
(previously it would swap in a bench player even with a negative or zero
projected value, which made the "best-possible-hindsight" benchmark not
actually optimal — hindsight should always have the option to just not swap
and bank the smaller half-points value instead of promoting to a bigger
loss). Together these restore the structural guarantee: **no-swap score ≤
best-possible-hindsight score, always**. "Recommended" (decision-time,
projection-based) is a *softer* claim — it can occasionally score below
no-swap in one realized round if a projection was simply wrong; that's
expected forecasting noise, not a logic bug, and `poc_run.py` prints a note
(not a warning) when that happens.

### Correction #2: the roster/active-squad model was fundamentally wrong

After the above bug was fixed and validated, the user flagged that the
lineup mechanics still weren't right: there's a concept of 3 players
excluded from the active squad each round (can't be swapped in at all), and
a "6th player" who scores full points. Working through it with the user
surfaced that the roster itself was also wrong — **13 players (5G/5F/3C)**,
not the originally-stated 10 (4G/4F/2C). That original number came from a
general rules description the user pasted early on and turned out not to
match this specific league (or was simply misremembered) — confirmed wrong
only after the user explicitly checked.

This required rebuilding `roster.py` (added `ActiveSquad`) and `lineup.py`
(three-tier scoring, multi-formation search) rather than a small patch. The
lesson for future sessions: **treat roster/rules facts as provisional until
independently reconfirmed**, especially ones that were stated early and
never revisited — they're exactly the kind of foundational assumption that
quietly invalidates everything built on top if wrong. If another rules
surprise shows up, re-derive from first principles (as was done here for
the valid-formations set: derived from "min 2 Guards, max 2 Forward, max 2
Center" rather than just hardcoding the three examples given) rather than
patching around the symptom.

## Validated backtest results

Backtest methodology: pick a cutoff round in the completed E2025 season,
build projections using only rounds strictly before it (no leakage), sample
a roster + active squad, run the full recommendation pipeline for the
cutoff round, then score against what actually happened. Doubles as both
the test harness and a first read on whether the heuristic has any value.

Run with a fixed `--seed 1` across four different cutoff rounds, **after**
the roster/tiered-scoring correction (these numbers are not comparable to
any earlier ones recorded before that correction — different scoring model
entirely):

| Round | No-swap | Engine-recommended | Best-possible hindsight |
|---|---|---|---|
| 15 | 96.0 | 101.0 | 103.0 |
| 18 | 135.0 | 170.5 | 182.5 |
| 20 | 59.5 | 59.5 | 60.5 |
| 22 | 71.0 | 71.0 | 88.5 |

The invariant `no-swap ≤ recommended ≤ best-possible` held in all four.

**Reading on this**: the swap logic still confirmed to help or be neutral,
never hurt. The gap to "best-possible hindsight" is narrower here than it
was under the old (wrong) model in a couple of rounds (e.g. round 20: 59.5
vs 60.5) — plausible, since half-points-by-default bench scoring reduces
the total variance a single swap decision can capture, but this was only a
4-round spot-check.

**Superseded by two further rounds of evaluation** — see
`docs/testing_log.md`, entries "Broader multi-season backtest" then
"Elaborate backtest: phase split, confidence intervals, loss analysis,
hyperparameter sensitivity" (the current authoritative one). Headline,
regular-season-only (91 rounds, 2730 trials, three seasons E2023-E2025):
recommended beats or ties no-swap in 96% of trials (76% outright beat),
+9.83 PIR mean gain per round (95% CI ±0.42), ~50% of available swap upside
captured on average, stable across a 4x range of the rolling-window
hyperparameter. **Important methodology note**: an earlier version of this
evaluation included playoff rounds and looked meaningfully worse late in
the season (28% beat-rate in the final calendar tercile) — that turned out
to be a sampling artifact (random league-wide roster draws don't account
for team elimination in playoffs), not a real engine weakness. Playoff
rounds are now excluded by default in `backtest_eval.py`
(`classify_phase()`, detected from actual per-round team counts rather than
a hardcoded round number, since team count changed between seasons).

## Known limitations / stand-ins (all deliberate, not oversights)

- **Free agent pool** = "everyone in the fetched pool not on the sampled
  roster." Real transfer suggestions need actual 12-manager ownership data,
  which doesn't exist yet.
- **Persistence**: raw API responses cache to disk (`raw/`, gitignored,
  source of truth for re-normalization); normalized rows also load into
  `euroleague.db` (SQLite, `engine/db.py`) via `sync_db.py`, the queryable
  store the rest of the app should read from. Still no ownership/draft
  tables — see below.
- **`sample_roster` and `sample_active_squad` are testing tools**, not how
  real rosters/exclusions will be entered — that's a manual-entry UI the
  user wants but hasn't been built.
- **Legacy-endpoint rows have `position=None`** — harmless today since v2 is
  primary and legacy is a rare fallback, but would need a lookup (e.g. the
  `people` endpoint) if legacy ever became load-bearing.

## Open questions / paused decisions

These were raised and then explicitly deferred by the user ("you are going
too fast... let's see what sort of data we get" — i.e. validate data/engine
before committing to app-level architecture). Revisit before building the
actual app shell:

- **Frontend approach**: lightweight server-rendered UI (simpler, faster to
  build) vs. a full SPA (React/Vite + API backend, more polished/interactive
  for a drag-drop lineup builder). Not decided.
- **Deployment target**: local-only (just the user) vs. hosted so the other
  11 managers could view rankings too. Not decided; local-only was the
  recommended default but never confirmed.
- **Historical backfill sequencing**: resolved — backfilled E2023-E2025 (not
  full history back to E2000; projections are within-season only, so older
  seasons only add backtest sample size, and pre-2023 eras are less
  representative anyway). See `backfill.py` and the testing log.

None of these block continuing engine work (broader backtesting, real
ownership tracking design, etc.) — they only matter once actual app/UI
construction starts.
