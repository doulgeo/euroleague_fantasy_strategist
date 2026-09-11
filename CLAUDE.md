# EuroLeague Fantasy assistant — project context

Personal (non-commercial) project: a tool to track EuroLeague Fantasy stats,
rank players by fantasy potential, and recommend lineups/transfers for a
12-manager private **draft-mode** league the user plays in with friends.
Licensing/ToS concerns are explicitly out of scope per the user — this will
never be monetized or redistributed.

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
  `lineup.py`, `transfers.py`) built and backtested against real 2025-26
  season results, on the **corrected** roster/scoring model (13-player
  roster, 5G/5F/3C; 10 active per round with 3 excluded; three-tier scoring
  of starters/sixth-man/half-point-bench — see `docs/game_rules.md`). The
  day-1/day-2 swap logic is confirmed to never hurt and never exceed the
  theoretical best-possible ceiling, across 4 different test rounds (see
  `docs/technical_notes.md` for the numbers).
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
- No database — everything runs from on-disk JSON cache + in-memory Python.
- No real 12-manager ownership/draft tracking (who owns whom, transaction
  history). The POC's "free agents" = pool minus one sampled roster, a
  stand-in.
- No UI of any kind. The user wants one specifically for logging opponents'
  transfers/trades (manual entry, since trades are rare in this league and
  the commissioner enters draft results by hand) — not yet started.
- No ML model. Heuristic-first was a deliberate choice (transparent, no
  training data needed, prove it's useful before adding complexity) — see
  "Why heuristic-first" in `docs/technical_notes.md` if that decision needs
  revisiting later.
- Frontend approach, hosting/deployment target, and full historical backfill
  sequencing were raised as open questions earlier and explicitly deferred
  by the user ("let's see what data we get first") — still open, see
  `docs/technical_notes.md` → "Open questions / paused decisions".

## Quick start

```bash
cd euroleague-fantasy
pip install -r requirements.txt

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

- Broader multi-round evaluation of the heuristic (average performance over
  many rounds, not just spot-checks) before investing in anything fancier.
- Real ownership/draft tracking: a place to record the 12-manager draft
  results and ongoing transactions, which is a prerequisite for real
  (non-stand-in) transfer suggestions.
- The deferred UI/architecture questions in `docs/technical_notes.md`.
