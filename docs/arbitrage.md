# Arbitrage scanner and paper arbitrage

Added after the November 2026 experiment showed that the strategy bots cannot
beat Kraken's Tier 1 fees. Arbitrage is the one family of trades with a
structural reason to exist, so this module measures whether it exists for you,
at your fees, at your size, before a cent is risked. It is built like the rest
of the system: read-only against the exchanges, fake money, numbers first.

## What it does

Every `arbitrage.scan_seconds` (default two minutes) the worker:

1. Fetches the public order book for each configured pair on each configured
   venue (Kraken and Coinbase by default, both quoted in USD).
2. For every pair and every ordered venue pair (buy on A, sell on B) walks the
   book for a `size_usd` order and computes the raw gap and the net result
   after both taker fees and slippage. Thin books and crossed books produce no
   observation rather than a fake one.
3. Stores each observation, so over days you can see how often a net edge
   existed and how large it was. The dashboard page is `/arbitrage`, the API is
   `/api/arbitrage`, and the daily Telegram report carries a summary.
4. Paper-trades: a fake account holds `inventory_multiple` trades' worth of
   cash and coin on every venue. When the best edge for a pair clears
   `min_net_edge_pct` after all costs, it buys on the cheap venue and sells the
   same quantity on the dear one at the walked prices plus slippage. Its result
   is its total inventory value minus what the untouched starting inventory
   would be worth at the same prices, so market moves cancel and only the
   arbitrage is counted.
5. If `arbitrage.funding.enabled`, fetches current funding rates from Kraken
   Futures and reports the annualised carry of a long-spot, short-perpetual
   position net of four taker fills, with the break-even holding period.

Commands:

```bash
python -m bots.main arb-scan      # one pass, prints every edge it saw
python -m bots.main arb-report    # summary of everything stored so far (--json for detail)
```

## What it does not do

- It never sends an order. There is no live arbitrage path in this codebase.
- It does not model transfers. Real cross-venue arbitrage without inventory on
  both sides means withdrawing coin from one exchange to the other, which takes
  minutes to hours and costs a withdrawal fee; the gap is gone long before.
  That is why the paper account is pre-funded on both venues, and why the
  inventory buffer running out is a real constraint, not a simulation artefact.
- It does not compare USD to USDT venues unless you enable them. Binance is in
  the config but off: a USD/USDT gap includes the stablecoin's own basis and
  would show edges that are not arbitrage.
- It does not include your exchange's withdrawal fees, API latency, partial
  fills or the other side moving between your two orders. All of these make
  real results worse than the paper ones.

## The arithmetic, so you can check it

Buy `q` coins at `a` on venue A, sell at `b` on venue B, slippage `s` per leg,
taker fees `fA` and `fB`:

    paid     = q * a * (1 + s) * (1 + fA)
    received = q * b * (1 - s) * (1 - fB)
    net      = received - paid

The raw gap `b / a - 1` that leaves exactly zero is

    (1 + s)(1 + fA) / ((1 - s)(1 - fB)) - 1

| venues and fees | break-even gap |
|---|---|
| Kraken Tier 1 (0.80%) and Coinbase (0.60%), 0.05% slippage | 1.51% |
| two venues at 0.40%, 0.05% slippage | 0.90% |
| two venues at 0.10%, 0.05% slippage | 0.30% |

BTC and ETH between two large USD venues normally differ by hundredths of a
percent. At retail fees the scanner is expected to record thousands of
observations with a net edge below zero. That is not a bug; it is the answer.

Funding carry: with rate `r` per interval of `h` hours, spot fee `fs`, futures
fee `ff`, slippage `s`, held for `d` days:

    gross per year = r * 8766 / h
    round trip     = 2 (fs + ff) + 4 s
    net per year   = gross - round trip * 365.25 / d

At a typical 0.01% per 8 hours, 0.80% spot and 0.05% futures fees, the gross is
about 11% a year, the round trip costs 1.9%, and the position has to be held
about 63 days before it has paid for itself. Funding also goes negative in
falling markets, when the position pays instead of earning.

## Reading the results

- If the "share of time profitable" on `/arbitrage` stays at 0% for a few
  weeks, cross-venue arbitrage does not exist at your fee tier and size. Lower
  fees (a higher Kraken tier, or a venue with maker rebates) or a different
  class of trade are the only ways forward.
- If it is above zero but the paper result is small, compare it with the
  effort and the risks listed above before considering anything real.
- Funding carry is a yield trade, not a fast profit. If the net per year is
  positive at your fees, it still needs a futures account, margin and a
  position size where exchange minimums do not matter. That is a separate
  decision with its own approvals, not something this module will ever do on
  its own.
