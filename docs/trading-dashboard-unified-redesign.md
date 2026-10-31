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

## Phase 3 — implemented on the SAME branch

- New dedicated `/learning` page with distinct counts for finished evolution
  generations, uniquely evaluated candidate genomes, historical holdout passes,
  failure events and saved public research sources.
- Clear step-by-step journey from historical optimization to separate paper
  verification; stages use **different units**, not a mathematically misleading
  conversion funnel. All historical testing explicitly labelled **not forward
  profitability**.
- Actual stored best/middle/worst training fitness values charted by generation;
  never interpreted as dollar profit or future trading success.
- Top historical candidates have plain-English explanations, training return,
  hold return, drawdown and trade counts, with advanced fitness behind an expander.
- Failure causes classified into costs/fees, too few signals, negative returns,
  risk, validation, and other. Horizontal bar charts use counts of recorded
  **failure events**, not unique bot IDs.
- Redesigned `/graveyard` with original diagnosis text and strategy details,
  filters for latest/all completed runs, category filters and safe pagination.
- Research sources stored during daily Google News collection are searchable at
  `/research/sources`, with optional publisher filters, collection timestamp,
  original source links, and plainly stated limits on news accuracy. Unsafe
  external URL schemes are not rendered as clickable links.
- Research feedback notes are shown as reading/diagnostic suggestions; public
  content cannot place trades, auto-change code, or bypass approval rules.
- New read-only JSON endpoints: `/api/learning/summary`,
  `/api/learning/sources`, and `/api/learning/failures`.
- Regression cases added for historical counts, test-pass scope, empty states,
  categorization, original source escaping, filtered news, rejection pagination,
  no account mutations and preserved protected approval API.
- No changes to trade execution, evolution selection logic, research collection,
  risk caps, storage schema, live mode, or approval requirements.

## Phase 4 — implemented on the SAME branch

- Dedicated `/safety` page: plain-English active status, recent completed-loop
  heartbeat, closed-candle freshness across the real configured Kraken pairs,
  live-account exposure and warnings for any active guard lock.
- Warns about kill switch, restart lock, reconciliation lock, owner pause, entry
  halt, unexpired daily loss halt, invalid risk config, stale worker/feed, and
  active live accounts while the displayed mode is PAPER.
- Pair-level data freshness, last refresh errors and recorded risk events appear
  in expandable and accessible detail views. **No button on this page clears
  a lock, calls the exchange, changes orders or re-enables trading.**
- `/settings`: read-only values and plain-language help for real-money risk caps,
  pilot and independent signoffs, capital scaling, maker/taker execution model,
  simulated accounts, research toggles, qualification thresholds and UTC jobs.
  Values are sourced from the existing loaded config and risk evidence; no new
  settings update/write API exists.
- `/approvals`: separate readable owner approval queue for existing
  `ready_to_graduate` standard paper bots, with observed forward-paper day/trade
  evidence, original strategy description and risk explanation. Unqualified
  research bots are excluded. Approve/decline forms require the same existing
  `DASHBOARD_TOKEN`; if unavailable the page remains review-only and instructs
  use of the original authenticated Telegram path.
- **No new approval endpoint or bypass:** both buttons POST to the unchanged
  `/bots/{bot_id}/{approve|decline}` route, which performs the same server-side
  constant-time token check and `decide_graduation` call. New confirm dialogue
  is user-interface feedback only, never authorization.
- Existing approved/declined POST redirect now opens `/approvals`, so the
  user sees the updated queue. Legacy `/advanced#bots` still contains all its
  original controls.
- `/api/safety/overview` gives a read-only structured snapshot with a strict
  allowlist of settings, *never* exchange keys, passwords or token values.
- Main sidebar Safety and Settings entries open their dedicated pages.
- Added regression cases for all safety locks and read-only behavior, unchanged
  token-gated approvals, no live fund allocation from approval, original routes,
  absent token behavior and no secret value disclosure.
- No change to RiskGuard, exchange reconciliation, historical qualification,
  funded pilot gates, hard capital ceiling, trade execution, DB schema, or worker.

## Phase 5 — verified in isolated staging; production release authorized

- A separate **Render free-plan staging service**, `trading-bots-phase5-qa`,
  ran the repository's **entire pytest suite** from this feature branch as
  part of a real build. Python compilation and all 269 tests succeeded,
  zero failures, on code commit `d9597267a6abba42593bb35350d04a7858929d04`.
- The first independent test attempt found 1 failure among 268 tests. It was
  an overconstrained non-emptiness assertion for the strict `trend_pullback`
  entry strategy on a random fixture. The test was corrected to verify signal
  causality for every strategy and a **separate deterministic positive-control**
  case proves `trend_pullback` can produce genuine entries. Strategy code,
  order sizing and risk controls were NOT changed.
- Staging build and web process run on an independent empty database, with
  `MODE=PAPER`, no exchange API credentials and **no trading worker**.
  Production capital/database and existing user account are unaffected.
- Staging returned HTTP success and valid content for `/healthz`, `/`,
  `/api/status`, `/bots`, `/research/forward`, `/learning`, `/safety`,
  `/settings`, `/approvals` and `/graveyard`.
- Read-only browser QA clicked the six primary destinations plus approvals
  and graveyard; confirmed readable content, consistent fake-money warnings,
  Simple/Advanced controls, working light/dark switch and no broken pages.
  The isolated empty staging DB correctly showed **missing market data and
  uninitialized accounts**, not invented profits, trading activity or health.
- Tests cover incorrect authorization tokens, owner-approval flow, risk lock
  preservation, safe external source URLs, paper/live separation, no fabricated
  equity, no future signals, transaction fee reporting and CSV filters.
- Existing GitHub Actions jobs still cannot start (account billing lock,
  0 steps executed). The independent Render test run is the executable
  verification; GitHub's red status is **not** a passing CI indicator.
- Merge PR #23 into `main` using the verified head SHA, allow the existing
  `main` Render service to auto-deploy, then check the actual production
  endpoints and deployment state before calling the release done.


## Important truth about results

Research bot balances are never reported as an approved bot, and experiment
P&L is never the same as real-money earnings. The preexisting strict
approval/pilot and live-account logic is not changed.

The PR workflow may fail to start because the owner's GitHub Actions billing
lock; do not claim passing CI from that status. Use alternative verification,
then resolve CI before rollout.
