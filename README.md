# EuroLeague Fantasy Strategist

A personal (non-commercial) tool for a 12-manager private **draft-mode**
EuroLeague Fantasy league. It tracks real box-score data, projects player
fantasy value, ranks players for the draft, and helps manage rosters/trades
for the season.

Not affiliated with, endorsed by, or licensed by Euroleague Basketball. Built
for personal use among friends — never monetized or redistributed.

## What it does

- **Projections** — a transparent, hand-auditable heuristic (no ML) that
  projects each player's next-game fantasy score (PIR) from recency-weighted
  recent form plus a team win-rate bonus.
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
  that hasn't been played yet), picks which 3 of 13 to exclude, the day-1
  starting lineup and captain, and the day-2 swap plan - auto-optimizing
  the starting formation (or force one manually) to maximize total
  projected score, shown in a totals box alongside the recommendation.
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
set_manual_projection.py  Set/clear/list manual projection estimates for players with no box-score history
docs/           Fuller technical notes, game rules, and a validation log
```

## Status

Actively developed. See `docs/testing_log.md` for a running record of what's
been validated and how.
