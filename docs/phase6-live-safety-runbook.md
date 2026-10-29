# Phase 6 — secure real-money execution readiness

**Status: infrastructure only.** Default deployment remains PAPER. Do not edit
`live_enabled: false` or `risk.max_total_capital_usd: 0` as part of this phase.

## Fail-closed controls

1. A live session still needs all original gates: `MODE=LIVE`,
   `live_enabled: true`, an owner-created `LIVE_CONFIRMED.txt` matching the
   strict configured cap, a verified non-withdrawing Kraken API key and
   persistent kill/crash locks clear. Neither deployment nor a candidate
   approval satisfies these gates automatically.
2. Before authorizing new capital, the worker performs **read-only** Kraken
   reconciliation. Every open Kraken order must match a recorded live pending
   entry or protective stop. A missing stop, unexpected/manual order, query
   failure, ambiguous ID or negative/nonfinite USD balance locks new live
   entries. Stops and risk-reducing exits remain available.
3. A failed stop cancellation cannot be treated as success. The worker may
   not send a second market sell while an earlier exchange stop's state is
   uncertain. After cancellation it fetches the order again and checks whether
   the stop already filled. A new stop is read back from Kraken for trigger,
   side and quantity; uncertain protection creates a sticky lock.
4. A kill switch revokes the confirmation and attempts to close all exposures.
   **If Kraken cannot confirm liquidation, the live account is retained**
   (including open quantity and order ID) for explicit recovery. It is never
   falsely marked flat or retired. Emergency loss limits are protective
   targets; slippage, outages, exchange behavior and unresolved orders can
   cause losses in excess of configured thresholds.
5. The persistent `live_reconciliation_halt` lock is not cleared by a
   successful periodic quote or retry. Only an authenticated owner audit and
   explicit `--acknowledge` clear it. The audit sends no orders.

## Manual read-only preflight for the owner

```bash
python -m bots.main audit-live
```

If and **only if** the Kraken account, open orders, spot balances and stored
positions have been independently verified, the owner can acknowledge a
successful read-only audit:

```bash
python -m bots.main audit-live --acknowledge
```

An audit command is not authorization to activate live trading. It does not
modify the required `MODE`, `live_enabled`, allocation caps, crash lock or
capital-confirmation file.

**Kraken API security:** Create a separate exchange key with Query Funds,
Query Open Orders & Trades, Query Closed Orders & Trades, Create Orders and
Cancel Orders ONLY; disable Withdrawal Funds, transfer and deposit permissions.
Use the least privileges possible, store keys only in the Render secret
environment, never in Git, database, Telegram or logs. An authenticated
`WithdrawMethods` permission probe must be denied before the key is accepted.
Where supported, enable MFA and an IP allowlist for the exchange account.
Render network egress IPs must be established before configuring an allowlist;
do not assume stable egress on the Starter plan.

## Incident response

1. Immediately send `/kill confirm` or use RiskGuard's kill switch. It
   revokes the capital file; an active position is not considered closed
   until Kraken confirms the fill.
2. Independently examine **Kraken Pro** for open stop orders, open limit
   entries, fills, base-asset balances and order history; manually resolve
   any untracked or unprotected positions. An orphaned stop can still sell
   after its apparent associated bot is closed.
3. Preserve the persistent SQLite database and logs for investigation.
4. Reconcile the exchange account with ledger records; contact the exchange
   if an order status is ambiguous. Do not restart live mode as a workaround.
5. Perform a successful read-only audit; explicitly acknowledge, then
   separately complete the pre-existing owner re-confirmation workflow.
   Continue in PAPER if uncertainty remains.

## Required before Phase 7 can open a funded pilot

- At least one **genuinely** qualifying strategy must complete Phase 4 unseen
  verification and the 30-day Phase 5 forward PAPER trial, not seeded data or
  retrospective fabricated observations.
- Confirm exact Kraken stop order semantics, precision, permissions and remote
  reconciliation against the **actual** exchange/API version. Stub-based CI
  tests cannot establish how a live order will execute.
- Independently reconcile spot base-asset holdings, open/closed orders,
  partial fills, failed order creation acknowledgments and actual maker/taker
  fees before funding. Current preflight covers resting-order IDs and USD
  availability, **not a complete asset/trade ledger reconciliation**.
- Review long-running Render instance availability, database recovery, secure
  secrets and independent outage notifications. Run the kill-switch and
  stop/cancel failure drills with an exchange-approved sandbox if available.
- The owner must personally authorize any real capital activation after
  reviewing the above. No step in Phase 6 supplies that authorization.

Stop losses are exchange-managed market-trigger orders and **not guaranteed
execution prices**. See the Kraken stop loss documentation and CCXT order
semantics before using a real key.
