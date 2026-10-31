# Experimental research and paper league

This project has two independent workflows.

**Strict pipeline:** evolution -> historical validation -> untouched holdout -> at least
30 days of forward paper -> manual approval -> gated live pilot. Nothing in this
change bypasses or relaxes that workflow.

**Research league:** three deliberately unqualified, explainable strategies
(BTC EMA trend, ETH RSI rebound, SOL price breakout) on observed 1-hour
Kraken closed candles. Each starts with $20 of completely simulated funds.
These bots are isolated in `lab_accounts`, `lab_fills`, and `lab_marks`.
They are never written to `deployed_bots`, `accounts`, `candidates`, or
`fills`. They cannot graduate, spend funds, or submit exchange orders.

## Research

The daily `research` command fetches Google News RSS searches covering
execution costs, strategy robustness, and market regimes. Optionally set
`GOOGLE_SEARCH_API_KEY` and `GOOGLE_SEARCH_CX` in Render to extend this
to Google Programmable Search (requires your configured search engine).
Without those, **Google News is the only Google search source**.

Research preserves titles, sources, citations and bounded feedback from
completed evolution runs. It cannot fetch arbitrary user URLs, execute
untrusted text, rewrite strategies, or authorize real money. Strategy
adaptation continues through the genetic evolution / graveyard feedback
using historical observed results. AI news summaries remain information
only. This is intentionally safer than letting web content or an AI agent
issue live trading commands or rewrite its own risk limits.

## Fees and fills

Spot Tier-1 configuration uses maker 0.40% and taker 0.80% per side as of
2026-10-31; the experiment assumes **taker on both legs** and adverse
half-spread plus market slippage on each leg. Positions are marked at a
fee-aware liquidation value. It never retroactively fabricates trades for
previously elapsed candles, and it does not use an unseen candle high to
claim a best-case fill. A stop/target is executed at the observed close
and can incur gaps. Every run uses only completed Kraken candles present
in storage, not research headlines.

The dashboard sections `#league` and `#research` and read-only endpoints
`/api/research/league` and `/api/research/sources` show these results.
The existing `/api/status` also includes `research_league` and `research`.

## Operations

- Default MODE is PAPER; live_enabled=false; pilot and scaling caps $0.
- Research runs daily after 06:00 UTC and stores latest findings in SQLite.
- The league seeds on a fresh candle and records no historical fills at
  inception. Subsequent new closed hourly bars simulate trades.
- No direct Google search without optional credentials; no guarantee that
  a public query returns quality articles or profit-making strategies.
- Keep these numbers clearly separate from qualified paper and real P&L.
- False profitability is worse than zero activity: maintain the unchanged
  validation gates, true execution costs and original risk caps.
