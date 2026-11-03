# Experiment (3 Nov 2026): can anything survive the fees, and does carrying bots across runs help?

Two questions from the owner, after every nightly evolution run on the deployed
service ended with "Nothing graduates from this run":

1. If entries were cheaper (maker fee with resting limit orders, or a lower
   taker fee), would any strategy class pass the gates?
2. If each nightly run continued from the previous run's best bots instead of
   restarting from random ones, would the search improve over time?

**Answer to both: no.** Nothing passed validation under any cost assumption,
and carrying bots over preserved the previous best fitness but did not raise
it. Details below. Nothing here is evidence that any configuration will be
profitable; it is evidence about which ones cannot be.

## Data

The experiment machine could not reach Kraken, Coinbase or the Fear & Greed
API, so it used Binance USDT 15-minute candles (public dataset built from
Binance 1-minute klines, CC0) for the six whitelisted pairs the dataset
covers: BTC, ETH, SOL, XRP, DOGE, ADA. 1 July 2023 to 30 October 2026,
116k candles per pair, 19 to 20 maintenance gaps of under 14 hours. Binance
and Kraken prices for these pairs differ by a few basis points, far below the
1.8% round-trip cost being tested. No Fear & Greed history was available, so
bots with a mood filter could not trade and died early; three quarters of
newcomers have the filter off, so the search was not materially narrowed.

The standard 60/20/20 split produced:

| period | dates | BTC | ETH | SOL | XRP | DOGE | ADA |
|---|---|---|---|---|---|---|---|
| train window 1 | 2023-07 to 2023-12 | +39% | +19% | +453% | +34% | +36% | +112% |
| train window 2 | 2023-12 to 2024-06 | +45% | +47% | +36% | -25% | +34% | -37% |
| train window 3 | 2024-06 to 2024-12 | +53% | +1% | +37% | +349% | +163% | +129% |
| train window 4 | 2024-12 to 2025-06 | +15% | -26% | -18% | +9% | -48% | -33% |
| validation | 2025-06 to 2026-03 | -38% | -21% | -46% | -41% | -44% | -53% |
| test | 2026-03 to 2026-10 | +17% | +13% | +13% | -8% | -19% | -30% |

The validation period is a deep bear market for every pair. That matters for
reading the results: a long-only bot that correctly stays out of a falling
market produces almost no trades there, and the gate then rejects it for
"too few trades" rather than for losing money.

## Runs

All runs: 200 bots x 100 generations, 4 walk-forward train windows, the
production gates unchanged, `python -m bots.main evolve` equivalent through
a driver that only changed the cost model or the carry-over setting.

| scenario | entry | taker | maker | final best fitness | passed train gate | passed validation | graduated |
|---|---|---|---|---|---|---|---|
| production (4 seeds) | market | 0.80% | 0.40% | 1.05 to 1.36 | 1 to 4 of 40 | 0 | 0 |
| maker + limit entries | limit | 0.80% | 0.40% | -3.05 | 0 of 40 | 0 | 0 |
| cheaper venue / Tier 3 | market | 0.40% | 0.22% | 1.86 | 13 of 40 | 0 | 0 |
| Binance-like | market | 0.10% | 0.10% | 2.17 | 16 of 40 | 0 | 0 |
| carry-over, 4 chained runs | market | 0.80% | 0.40% | 1.17, 1.27, 1.26, 1.26 | 3 to 6 of 40 | 0 | 0 |

### Maker fee with resting limit entries

Useless as implemented, exactly as the comment in `config.yaml` warns. On
15-minute and longer candles the next bar opens at the previous close, so the
backtester cannot prove a 120-second resting bid filled; 3,218 bots died of
"limit fills unverified at candle resolution" and 100% of the final generation
made zero trades. Testing maker entries honestly needs tick or order-book
data, not OHLC candles.

### Lower taker fee

Cheaper fees change which bots die (fee deaths fall from 2,705 to 1,798 to
799 as the fee drops from 0.80% to 0.40% to 0.10%) and let more bots through
the train gate, but every one of them then failed validation. Typical
reasons: 2 to 8 trades where 12 are required, drawdown 13% to 26% against a
10% limit, profit factor 0.00 to 1.14 against 1.15. The bear-market validation
period, not the fee, is the wall here. The design is long-only, so no setting
can make money while every coin halves.

Out-of-sample check of the 20 best final-generation bots of each run,
ignoring the gates entirely (so this is the most favourable possible reading):

| scenario | profitable on validation | profitable on test | profitable on both | median trades per period |
|---|---|---|---|---|
| production | 1 of 20 | 0 of 20 | 0 of 20 | 6 to 9 |
| 0.40% taker | 18 of 20 | 7 of 20 | 7 of 20 | 6 to 8 |
| 0.10% taker | 1 of 20 | 2 of 20 | 0 of 20 | 3 to 7 |
| carry-over, 4th run | 0 of 20 | 0 of 20 | 0 of 20 | 7 to 9 |

The 0.40% result looks better until you notice the 0.10% result is worse. If
lower fees created a real edge, cheaper would not do worse than cheap. With
six to nine trades per period the ranking is noise, and seven bots out of
twenty being profitable twice is what picking the top of a 200-bot search
gives you by chance. The best single bot, SOL/USD 4h EMA 29/46 cross with a
BTC trend filter, made +13% on validation and +11% on test at the 0.40% fee
over about 14 trades. That is one bot chosen after the fact, out of 200,
with 14 trades. It is not evidence of an edge and must not be deployed on it.

### Carrying the population across runs

Implemented as `evolution.carry_over_fraction` (default 0.25): each run starts
with that share of its population taken from the top of the previous run's
final generation, the rest newcomers as before. Four chained runs on the
production cost model:

| run | best fitness at generation 0 | best fitness at generation 99 |
|---|---|---|
| 1 (nothing to carry) | -0.64 | 1.17 |
| 2 | 1.17 | 1.27 |
| 3 | 1.27 | 1.25 |
| 4 | 1.26 | 1.26 |

Four independent runs with fresh random seeds reached 1.36, 1.25, 1.05 and 1.31.
Carry-over makes each run begin where the last one ended, and then it sits
there: the search has converged on a local optimum (SOL/USD 4h EMA cross
with a trend filter, target near 15%) that the fees and the bear-market
validation period will never let through. Continuing the search from that
point does not help, because the problem is not that the search was too
short. The setting is kept because it costs nothing and stops nightly runs
from forgetting what they found, but it is not expected to produce a
graduate.

## What this means

- Kraken Tier 1 taker fees make the current design unworkable, and that part
  is not fixable by searching harder. The round trip costs about 1.8% and the
  search already concentrates on 4-hour candles with 12% to 15% targets to
  survive it, which leaves too few trades to prove anything.
- Removing the fee problem does not by itself produce a strategy that passes.
  Long-only rules on liquid majors have no measurable out-of-sample edge in
  this data at any fee level tested.
- Maker-fee entries cannot be evaluated with candle data. That experiment
  needs a different data source before it can say anything.
- The gates are doing their job. Every configuration that looked promising
  in training failed out of sample, which is exactly the overfitting the
  gates exist to catch. Loosening them would deploy noise.
- No part of this experiment is a promise of profit. A strategy that passed
  every gate would still be a hypothesis until it had traded forward.
