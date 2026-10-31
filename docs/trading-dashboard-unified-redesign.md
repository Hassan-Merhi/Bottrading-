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

## Phase 2 — pending

- Dedicated bot detail views with status explanations and sparklines.
- Expand results to flexible periods, fee breakdown, individual coin views,
  filters and audited exports.

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
