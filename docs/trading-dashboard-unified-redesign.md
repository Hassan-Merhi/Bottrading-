# One-branch dashboard redesign

**Branch:** `feature/trading-dashboard-unified-redesign`

Each phase builds on this branch. Do NOT merge or deploy to production until
all requested phases are completed, reviewed and safety-checked.

## Phase 1 — implemented

- The default `/` page becomes a concise beginner-friendly home screen.
- Four primary indicators: fake starting capital, net experimental fake P&L,
  completed experiment round trips, and actual live allocations.
- Live-market freshness and heartbeat status with warnings; avoid claiming
  profitability when zero trades have finished.
- Existing 3 research experiment cards remain separate from the qualified
  paper pipeline.
- Real historical forward performance graph only when observations exist,
  otherwise an accurate empty-state explanation.
- Six shared destinations: Home, My Bots, Performance, Learning & Research,
  Safety, Settings. Existing routes continue working.
- Consistent responsive visual system and sidebar / mobile navigation on
  `/`, `/advanced`, `/research/forward`, and `/graveyard`.
- Simple default and explicit Advanced switch. Advanced contains all original
  settings, pipeline, bot detail, approvals, logs and technical charts.
- Light/dark preference and reduced-motion rules.
- No trading, risk, fee, market data, database or permission changes.

## Phase 2 — implemented on the SAME branch

- Dedicated My Bots directory at `/bots`: research-only experiments are visually
  separate from standard-pipeline paper and LIVE accounts.
- Individual research profiles at `/bots/research/<slug>`: simple strategy
  explanation, active position status, paper allocation, modeled fees, settled
  trades, date-scoped equity, and observed simulated activity.
- Standard paper/live account profile at `/bots/account/<account_id>`: a
  clearly labelled mode, account allocation, return, stored balance chart,
  strategy description and recorded fills. Paper accounts show forward evidence;
  LIVE accounts display real-money warnings, not a paper badge.
- Improved Performance at `/research/forward`: filters for Today, 7 days,
  30 days, All time and individual experiment; USD KPI tiles; hover/touch/keyboard
  chart inspection; cost-adjusted buy-and-hold benchmark.
- Trading costs broken into buy fees, sell fees and their total, with no second
  deduction from already-net profit.
- Searchable transaction ledger: name, coin, reason; buy/sell and profitable/
  losing sale filters. Explanatory expanders show reason, fill ID and quantity.
- New `/api/performance` and `/api/performance.csv`: exact selected window
  and transaction filters; export marked equity or recorded fills. CSV formula
  injection protection for spreadsheet-opening clients.
- Period P&L uses only the first and last witnessed forward equity marks in the
  selected date window; if fewer than two, reports **Not enough data**. Original
  all-time account P&L is shown separately and never presented as period profit.
- A 25k-mark/fill bounded safety limit avoids enormous unresponsive pages and
  warns when full history exceeds this limit.
- Regression coverage added for real observation windows, missing-data
  honesty, fees, win rates, trade search and filters, CSV safety, and research/
  paper/LIVE isolation.
- Existing legacy endpoints and protected approval POST actions unchanged.
- No strategy, order, risk, validation or live-trading code altered.

## Phase 3 — pending

- Learning and research summaries, validation funnels, failure charts,
  readable strategy details and sources.

## Phase 4 — pending

- Dedicated Safety and Settings pages, health explanations and protected
  approval interfaces. All existing risk checks and authorization gates stay
  effective.

## Phase 5 — pending

- End-to-end checks (account separation, desktop/mobile/browser, timing,
  no-fabricated-results, approvals), review and deliberate production rollout.

## Important truth about results

Research bot balances are never reported as an approved bot, and experiment
P&L is never the same as real-money earnings. The preexisting strict
approval/pilot and live-account logic is not changed.

The PR workflow may fail to start because the owner's GitHub Actions billing
lock; do not claim passing CI from that status. Use alternative verification,
then resolve CI before rollout.
