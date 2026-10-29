# Phase 8 — Live Profitability, Measurement and Scaling Controls

**Implementation status: automated review software only.**
There are no funded live bots, no qualified Phase 7 live trading pilot, and
no real-money profitability evidence today. This phase cannot honestly
claim that a strategy is profitable or that the $100 app-wide capital
ceiling can be increased. The safe behavior is to keep real scaling OFF.

## What the deployed monitoring computes

Read-only `/api/scaling/readiness` and `/api/status.scaling` report
individual account-level **REAL** results, separate from PAPER and
historical backtests:

- Real days elapsed since live account creation **and** witnessed equity
  history span (cannot be satisfied by backdating a start timestamp).
- Count and continuity of actual UTC days with persisted live equity marks;
  recent equity valuation and a fresh Kraken public market candle.
- Reconciled closed live trade counts based on persisted buy/sell fills,
  bounded by the closed-trade counter; aggregate fees and realized net P&L.
- Realized profit factor, net marked-to-market return after fees, maximum
  drawdown, and a fee/slippage/spread-adjusted passive buy-and-hold baseline.
- Presence of an original Phase 7 paper-evidence certificate for the
  current genome and live confirmation epoch.

By default, even eligibility for a **new manual scaling review** requires
60 elapsed real-money days, 45 observed days, no monitoring gap above
3 days, at least 30 real settled round trips, profit factor >=1.20,
more than 5% net return, max drawdown strictly under 5%, outperformance
of net holding, a valid pilot certificate and current valuation. The
source ledger/stop safeguards from Phases 6 and 7 still apply.

No read-only analysis, Telegram message or green CI run **automatically**
updates the allocation, wallet, enabled mode, risk caps or exchange
positions.

## Scaling is explicitly disabled

```yaml
scaling:
  enabled: false
  owner_signoff: false
  max_total_capital_usd: 0
```

These separate settings do **not** override
`MODE=PAPER`, `live_enabled: false`, `pilot.enabled: false`,
`pilot.max_total_capital_usd: 0` or
`risk.max_total_capital_usd: 0` in the default production
configuration. The exchange order path is unchanged.

Even a passing review results in the read-only `review_eligible`
label, **not** real money being added. The application-level capital
hard ceiling of $100 is not increased by Phase 8.

## Requirements still outside software validation

1. Phase 4 must actually find a robust candidate.
2. Phase 5 must collect a genuine 30-day PAPER sample.
3. Phase 6 must be proven against the actual Kraken exchange, including
   order-book permissions, spot holdings, fees, partial fills, stop-loss
   orders and order reconciliation.
4. Phase 7 must run a separately authorized, funded micro-pilot and
   collect new, genuine live trades for the 60-day Phase 8 review.
5. A **new, separate owner decision** must identify the exact bot,
   scale amount and loss budget. Real exposure can be lost, and past
   statistical performance does not guarantee future profitability.

### Audit and caveats

Persisted live fills are locally recorded exchange responses, not an
independent external audit. The daily marks are time-series samples, not
an intraday volatility measure. CCXT/market public closes may differ
from a real executable fill. Positive after-fee returns alone are not
sufficient for a confident production scale decision; independent broker
statement reconciliation and additional live sampling remain essential.

A testing fixture can fabricate observations **inside unit tests only**.
It does not update the Render production database, produce investment
evidence or authorize the production exchange.
