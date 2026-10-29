# Phase 7 — controlled real-money pilot: code ready, funding blocked

Phase 7 implements a **disabled-by-default** one-bot pilot framework. It does
**not** claim the bot is profitable, does not create a Kraken API key, does not
fund the exchange, and does not authorize live orders. A successful deploy is
not a successful real-money trial.

## Pilot gate checklist

All conditions must hold together, in addition to the original risk system:

1. A strategy must pass the Phase 4 one-time historical test with the current
   audited rules, including fees/slippage and independent unseen data.
2. It must complete Phase 5's 30 *elapsed* days, 21 observed UTC days, 20
   recorded closed round trips, drawdown/profit-factor/after-fee requirements,
   no long monitoring gaps, and better-than-hold performance. **Zero
   strategies currently satisfy this.**
3. The owner approves **that specific qualified bot** in the graduation
   workflow. Its approval decision and originating historical candidate must
   exist in the persisted record. Approval alone is not enough.
4. Live order placement still requires `MODE=LIVE`, `live_enabled: true`,
   matching owner-typed `LIVE_CONFIRMED.txt`, a verified non-withdrawing
   Kraken key, no kill/crash/reconciliation locks, valid market data and
   adequate **actual** free Kraken USD. A new Phase 6 real-exchange audit
   must succeed, and untracked/missing stop orders halt entries.
5. The **independent pilot** configuration also requires `enabled: true`,
   `owner_signoff: true`, `exchange_signoff: true`, positive explicit
   pilot capital and a bot cap (1 by default). The initial production
   configuration ships with **all three approvals false and $0 pilot capital**.
   These fields are not pre-approvals and must never be automatically changed.
6. A historical+paper certification is sealed to the bot, genome and
   `live_epoch` only upon the initial allocation. The certification remains
   auditable after the paper account stops recording new quotes; a changed
   genome, outdated source rules or fresh capital confirmation invalidates
   the old certification.
7. Orders must stay under the *smaller* of global and pilot per-bot and
   aggregate caps. The pilot can run only one bot by default, and triggers
   the emergency kill at $2 cumulative mark-to-market live loss or the
   configured global kill level, whichever is lower. A pilot bot is demoted
   when its equity drawdown exceeds 5% (or a tighter existing threshold).
   A stop order does **not** guarantee execution at its trigger price.

## Current defaults

- `pilot.enabled: false`
- `pilot.owner_signoff: false`
- `pilot.exchange_signoff: false`
- `pilot.require_qualified_paper: true` — mandatory for production
- `pilot.max_bots: 1`
- `pilot.max_total_capital_usd: 0`
- `pilot.max_per_bot_usd: 20`
- `pilot.max_live_loss_usd: 2`
- `pilot.max_live_drawdown_pct: 5`
- Existing `live_enabled: false`, `risk.max_total_capital_usd: 0`,
  Render `MODE=PAPER` unchanged.

The real order-path unit tests use an explicit **test-only simulated fixture**
that disables paper qualification to test isolated exchange behavior; do not
copy the fixture settings into any production configuration.

## Read-only monitoring

- `/api/pilot/readiness` exposes pilot signoff/configuration blockers and
  current allocated live accounts, without secrets.
- `/api/status` includes the same `pilot` status.
- The main dashboard shows the PILOT OFF state and missing approvals.

## Required manual steps before ever moving funds

1. Review at least 30 days of *new* forward-paper trading evidence from a
   genuinely qualified bot. No fabricated trades, dates or backtest-only
   performance may substitute for forward evidence.
2. Independently inspect actual Kraken account assets, recent fills, open
   orders, partial fills, fee tier, asset exposure and CCXT stop-loss
   semantics. A mocked exchange test and Phase 6 open-order-ID audit are
   insufficient to prove real Kraken reconciliation.
3. Confirm non-withdrawal Kraken API permissions, network restrictions where
   possible, exchange access, balance and independent incident alerts. Do
   not place API keys in Git or user-visible logs.
4. Rehearse an emergency disconnect and kill switch with a true sandbox
   or other exchange-approved zero-risk environment. Any unresolved
   cancellation, fill or protective stop blocks funding.
5. Agree explicitly with the owner on the exact permitted real-dollar
   capital, maximum risk and pilot dates. Only then should an authorized
   operator separately configure the mutually required gates, and retain
   a human-monitored abort procedure.

**Do not launch a funded pilot until those steps are independently verified
and approved.** Neither these source changes nor green CI demonstrate actual
profitability or live-exchange readiness.
