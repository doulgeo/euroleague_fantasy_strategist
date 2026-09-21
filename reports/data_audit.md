# Data audit — projected-minutes / team-strength feature request

Milestone 1 of the proposed `proj/` package (see the feature request this
responds to). This is audit only — no `proj/` modules exist yet. Everything
below was checked live against `euroleague.db` and the EuroLeague API on
2026-09-21, not assumed from prior docs.

## 1. What actually exists

### 1.1 Schema (`player_game_stats`, `engine/db.py` / `engine/data.py::FIELDNAMES`)

One row per player-game. Columns: `source, season_code, game_code, round,
game_date, team, opponent, home_away, team_win, player_id, player_name,
position, is_starter, played, minutes_seconds, points, fg2m, fg2a, fg3m,
fg3a, ftm, fta, oreb, dreb, reb, assists, steals, turnovers, blocks,
blocks_against, fouls_committed, fouls_drawn, plus_minus, pir_official,
pir_recomputed, pir_diff`.

This already covers essentially everything the spec's §1.1 expected schema
asks for (season, game_id, date, team, opponent, home flag, player_id,
name, minutes, started, pts, reb off/def, ast, stl, blk, blocks_against,
fouls committed/drawn, turnovers, FGA/FGM, 3PA/3PM, FTA/FTM, PIR) **except**:
- **Final score / number of overtimes per game is not persisted.**
  `list_games()` (the schedule fetch) receives `local.score`/`road.score`
  from the API but `normalize_schedule`/the `schedule` table discard it —
  only team codes, round, date, played flag are kept. Overtime count *can*
  be inferred as a proxy from summed team minutes (see 1.2), but a real
  score/OT column doesn't exist today. Small schema addition if needed for
  §4.1 pace normalization.

Row counts: E2023 7,883 rows / 331 games / 296 players / 18 teams · E2024
7,863 / 330 / 306 / 18 · E2025 9,540 / 402 / 351 / 20. 25,286 rows total.

### 1.2 Data-quality checks (spec §1.2)

- **Duplicate player-game rows**: 0. `(season_code, game_code, player_id)`
  is the table's primary key, enforced at the DB level, not just checked.
- **Minutes format**: not an issue — `minutes_seconds` is already an
  integer (seconds), converted from the API's `timePlayed` (seconds) or
  legacy `"MM:SS"`/`"DNP"` string at ingest (`engine/data.py`). No raw
  `"MM:SS"` strings reach the DB.
- **DNP vs absent rows**: DNPs are present as explicit rows (`played=0`,
  `minutes_seconds=0`), not missing — 2,274 such rows, and `played=0`
  count matches `minutes_seconds=0` count exactly (no partial-minute DNPs,
  no non-zero-minute `played=0` rows).
- **Mid-season transfers (player with >1 team in a season)**: 20 total —
  E2023: 7 players, E2024: 6, E2025: 7. Confirmed the raw data already
  keeps these as separate per-team rows (the `team` column is per-row, not
  merged) — stint separation the spec asks for is already the natural
  shape, nothing to build here.
- **Name/ID crosswalk across seasons**: **0 mismatches.** Checked both
  directions — no `player_name` maps to >1 `player_id`, and no
  `player_id` maps to >1 `player_name`, across all 25,286 rows /
  E2023–E2025. The EuroLeague API's own player codes are stable; no
  crosswalk-building work is actually needed, contrary to what the spec
  assumed might be required.
- **Minutes-per-team-game sum check**: computed `SUM(minutes_seconds)` per
  `(season, game, team)` across all 2,126 team-games. Distribution: 2,032
  at exactly 200 min (regulation), 78 at 225 (1 OT), 12 at 250 (2 OT), 2 at
  275 (3 OT), and one single game (E2023 game 170, IST vs MAD) at 300 on
  both sides (4 OT — unusual but internally consistent on both teams, not
  an obvious data error). **2,125 of 2,126 team-games (99.95%) land
  exactly on a 25-minute-per-OT grid** — the box-score data is clean here.
- **PIR recomputation**: `pir_diff` is 0 for all 25,286 rows (already
  validated pre-existing, reconfirmed here) — 0 mismatches.
- **18→20 team transition**: confirmed directly — E2023/E2024 = 18 teams
  each, E2025 = 20 teams. Games-per-team already varies within a season
  too, not just across seasons (E2025 ranges 38–44 games/team depending on
  when the count was taken and BCL/playoff paths) — per-game or
  per-100-possession rates, never raw per-season totals, is the right
  call, matching spec §1.4.

### 1.3 Existing related code (do not rewrite — see ground rules)

- `engine/team_strength.py` + `team_strength_backtest.py`: a prior,
  simpler attempt at exactly the "team strength" idea in this request —
  per-team PIR-allowed over a trailing window, leakage-safe. **Backtested
  2026-09-14 and found to have no demonstrable predictive value** (+0.050
  correlation with projection error, ~0.13% best-case MAE improvement) —
  not wired into `build_projections`. See `docs/testing_log.md` → "Team
  strength index". This request's §4 is a substantially more rigorous
  version (adjusted ratings via ridge regression, pace, matchup
  multipliers) — worth trying properly, but the project's history here is
  a real prior negative result on a cruder version of the same idea, not a
  blank slate.
- `engine/projections.py`: the current heuristic (recency-weighted rolling
  PIR mean, ≥3-game minimum, team win-rate bonus). This request adds two
  *new* modules alongside it and is explicit about not rewriting it —
  noted, will not touch it except at the narrow `project_player`/
  `project_team` integration seam in §5.
- No existing minutes-projection code at all — `engine/projections.py`
  tracks a minutes *trend* (recency-weighted) but does not model
  allocation, availability, or role class. §3 is genuinely new work, not a
  rewrite.

## 2. Required inputs (§2) — status

| Input | Status |
|---|---|
| `roster_2026.csv` (team, player_id, name, position, age) | **Available, not as CSV.** The `rosters` table (290 rows, E2026, from the live `/people` endpoint) has team/player_id/name/position/dorsal. **Age**: not in the table, but `/people`'s underlying `person.birthDate` is present for 100% of 307 active E2026 players checked live just now — exact birthdate, not estimated. **Depth-chart role**: genuinely not available anywhere in this project or the API. Real gap — falls back to spec §3.4's own fallback (position-group median × 0.7, flagged) for every player, not just true newcomers, unless sourced externally. |
| `coaches.csv` (team, coach, seasons/previous teams) | **Available, not as CSV, and not previously known to this project.** Checked live: `/people` for E2026 includes `type == "E"` (`typeName: "Coach"`) rows — 20 head coaches, one per team, with name + club. Separately, **every cached per-game stats response already contains each side's coach** (`{"coach": {"code": ..., "name": ...}}`) — this is in the 76MB `raw/v2_stats` cache for E2023–E2025 already on disk, just discarded by `normalize_v2` before it reaches `FIELDNAMES`/the DB. So historical coach-per-team-per-game, and current coach-per-team, can both be derived from data already fetched/fetchable — no external coaches.csv needed. This does mean a small `engine/data.py` change (stop discarding the `coach` field) plus a new persisted table, which is in scope for building §3.6, not a blocker. |
| `injuries.csv` (player_id, expected_games_missed, source) | **Partially available.** The `injuries` table (30 rows, basketnews.com scrape, `sync_injuries.py`) has a qualitative `status` (Out/Doubtful/Questionable/Uncertain/Game-time/Expected/Ready) + free-text `comment`, not a numeric `expected_games_missed`. No games-missed count anywhere. Spec §3.2's injury override path (`p_active = 1 - missed/season_games`) can't be driven numerically as written — would need either (a) a hand-maintained numeric override table (same pattern as the existing `manual_projections` table) or (b) treating only `status == "Out"` as a binary override, degrading gracefully otherwise. Recommend (b) to start, consistent with how `app.py`'s `/lineup` route already treats `Out` today. |
| `fixtures_2026.csv` (optional) | **Available.** `schedule` table has E2026: 380 games, round 1 = 2026-09-25, currently 0 played (preseason). |
| Preseason box scores (optional) | **Not available.** No E2026 games played yet as of 2026-09-21. Optional per spec — not a blocker. |

**Net**: nothing here actually blocks starting §3/§4. The one real, unfillable
gap is **depth-chart role for newcomers** — the spec already has a fallback
for that case, so it degrades rather than blocks. Coaches data requires a
small code change (persist a field that's already being fetched/cached) but
not new external input. Numeric injury severity requires picking between a
manual-override table or a binary-Out approximation — a design decision, not
a missing-data blocker.

## 3. Sample-size reality check

Only 3 seasons (E2023, E2024, E2025) → exactly 2 season-to-season
transitions to validate minutes/team-strength projections on, and (per
spec §4.3) ~56 team-seasons (18+18+20), not the ~36 the spec's own text
estimated — still a small-N ridge regression, 2–4 parameters max, as the
spec's ground rules already require. Flagging so results are read with
that in mind, not as a blocker.

## 4. Recommendation before building §3–§5

This is a large addition (new `proj/` package, 7 modules, config, tests,
5 milestones) on top of a project whose established discipline (see
`docs/technical_notes.md` "ML-vs-heuristic comparison", and the
`team_strength.py` history in §1.3 above) is *nothing ships unless it
demonstrably beats the current baseline on a backtest* — both the ML
regression pivot and a cruder team-strength attempt were built, backtested
honestly, and **not** wired in because neither cleared that bar. Nothing
here changes that discipline; §3.9/§3.10 and §4.7/§4.8's own acceptance
gates already encode the same "ship it only if it wins, else ship
shrunk-naive and say so" rule. Proceeding to Milestone 2 (backtest harness
+ baselines) next, per the spec's own milestone order, unless redirected.
