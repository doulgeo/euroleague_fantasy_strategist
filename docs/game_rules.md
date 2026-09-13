# EuroLeague Fantasy — game rules (draft mode)

Source: the user's own account of the rules, refined over several rounds of
correction during project brainstorming — the roster size and active-squad
model in particular went through multiple wrong assumptions before landing
here. This is domain knowledge the engine's logic depends on directly; see
`technical_notes.md` for how each rule maps to code, and for which parts are
still explicit assumptions rather than confirmed rules.

## Roster (draft mode)

- 12 managers in the league. Each manager's full roster is exactly **13
  players: 5 Guards, 5 Forwards, 3 Centers** (draft mode has no head coach).
  *(An earlier belief that this was a 10-player 4-4-2 roster, taken from a
  more general rules description, was wrong and corrected by the user after
  checking.)*
- **Draft mode rosters are exclusive**: each real EuroLeague player can
  belong to only one manager's team league-wide. Selection happens live
  (auction/Zoom call/whatever the group does), and a Draft Commissioner
  manually enters the resulting rosters.
- Player values **do not fluctuate** with performance in draft mode — the
  100-credit budget only matters during the live draft/auction itself.
- Free-agent pool exists: 12 × 13 = 156 drafted vs. ~200+ contracted
  EuroLeague players.
- Trades between managers are rare in this group's experience. **Transfer
  windows sit between rounds** (between game turns), not continuously open.

## Active squad — the part that was wrong for a while

Before every round ("turn"), from your 13-player roster you choose an
**active squad of 10**, leaving exactly **3 players excluded**. Excluded
players score zero and **cannot be swapped in at all** that round, no matter
what. (This is distinct from being benched — see below.)

The exclusion must still leave you able to field a valid starting formation
(at minimum: 2 Guards, 1 Forward, 1 Center among the active 10) — you can't,
say, exclude all 3 of your Centers.

## Scoring tiers within the active 10

This is a three-tier hierarchy, not a simple starter/bench split:

| Tier | Count | Scoring |
|---|---|---|
| Starters | 5 | Full points. One of the five is **captain**: doubles (2x). |
| Sixth man | 1 | Full points too, but **never captain-eligible**. |
| Bench | 4 | **Half points** — and this is automatic, not conditional on being swapped in. A bench player who's never touched all round still scores at half rate for whatever they actually do. |
| Excluded | 3 | Zero, always, no swap can change this. |

## Starting five formation

The 5 starters must be one of exactly **three valid formations** (Guard,
Forward, Center): **2-2-1, 2-1-2, or 3-1-1**. Derived rule, confirmed by the
user: minimum 2 Guards, Forward and Center each capped at 2 and floored at
1. (1-2-2, despite satisfying "no 3 Forwards/no 3 Centers", is *not* valid —
the floor is specifically 2 Guards minimum, not 1.)

## Scoring — Performance Index Rating (PIR)

PIR is the fantasy currency, not raw points. Per player, per game:

| Action | Effect |
|---|---|
| Point scored | +1 |
| Rebound | +1 |
| Assist | +1 |
| Steal | +1 |
| Turnover | -1 |
| Block performed | +1 |
| Block suffered | -1 |
| Foul drawn | +1 |
| Foul committed | -1 |
| Missed field goal | -1 |
| Missed free throw | -1 |

Plus a **team win bonus: +10% of that player's fantasy score for the round**
if their team won.

**Captain**: doubles (2x) whatever they score, and must be one of the 5
starters (not the sixth man). **Confirmed by the user (2026-09-13): captain
can be reassigned mid-round at the day-2 swap window** — e.g. captain plays
badly Thursday, you name a new captain among your Friday starters. Doubling
is per-stage: the day-1 captain's already-banked points stay doubled at
whatever they were; reassigning only affects points not yet banked. See
`technical_notes.md` for how this maps to `compute_round_score`.

## The turn-based substitution mechanic

A EuroLeague round is normally played over **two days** (commonly Thu/Fri or
Tue/Wed, though real schedules can have reschedules landing on other dates —
don't hardcode specific weekdays; derive "day 1" vs "later" from the actual
game dates in a given round).

- **Day 1**: whichever of your 6 full-scoring slots (5 starters + sixth man)
  play that day, their points are earned and **banked permanently** — a
  later swap does not take those points away.
- **Day 2 (next morning)**: having seen day-1 results, you can swap any
  full-scoring slot whose game is already over for a same-position bench
  player who plays day 2 — same-position only (confirmed: "you cannot swap
  in a guard for a forward"). Promoting a bench player this way upgrades
  them from half points to full points for their still-upcoming game — a
  real gain, not just an "activation."
- **Golden rule**: fill your 6 full-scoring slots with day-1 players first
  wherever possible. Starting a day-1 player preserves the option to *also*
  capture a day-2 player's points (at full rate) in that same slot later;
  starting a day-2 player from the outset forfeits that day-1 opportunity
  for good.

## This league's specifics

- **12 managers**, draft mode, roster exclusivity as above.
- **Head-to-head format**: matched against one opponent per round; total
  season points matter only as a tiebreaker.
- Captain doubling confirmed; win bonus is +10% as documented above.
- Trades are rare; transfer windows sit between rounds.
- The user (not all 12 managers) will be the one using this tool. They plan
  to manually log opponents' rosters/transfers/trades themselves and
  explicitly asked for a UI to make that easy, rather than auto-tracking it.
  That UI has not been built yet.
