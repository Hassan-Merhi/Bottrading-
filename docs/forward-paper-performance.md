# Timestamp-proven experimental paper performance

**Status:** PAPER mode only. Starting the isolated lab does not require deposits,
exchange API credentials, real orders, or a live pilot.

## Where to look

* `/research/forward`: filter all three experiments or select one; compare
  simulated equity and a passive buy-and-hold benchmark with modeled execution
  costs; inspect the full event ledger.
* `/api/research/forward?name=all&limit=720`: bounded JSON evidence.
* `/api/research/forward.csv?name=all&kind=equity`: export hourly observations.
* `/api/research/forward.csv?name=all&kind=trades`: export fills and realized P&L.
* Main dashboard `#league`: mini history/chart and link to full view.

## Exactly what is forward evidence?

When the worker sees a **newly closed** hourly Kraken candle, it calculates
the signal from historical candles ending at that close, fetches an **observed,
current Kraken ticker** quote and simulates an entry, exit or equity mark. It
stores a distinct `observed_ms` time, `candle_ts`, ticker quote source and
net-liquidation equity. Existing early marks from before this schema change
retain NULL observation timestamps; their observation times are **not**
invented. No older signals or missing hours are ever replayed.

The P&L accounts for configured bid/ask spread, slippage, both taker-fee legs
and current marked-to-market liquidation costs. Closed sells record realized
P&L including the original buy and final sell cost. A simulator can suffer
worse live fills or losses. A simulation is not a record of real Kraken orders.

Charts do not forward-fill missing observations. The all-bots line includes
only matching closed-hour marks observed for *every* experiment.

## Operational boundaries

The lab writes only `lab_accounts`, `lab_marks`, and `lab_fills`.
It never writes to `candidates`, `deployed_bots`, real/paper approved `accounts`,
`fills` or any live-exchange object. It cannot graduate or allocate real capital.

The original strict qualification remains separate: 30 elapsed paper days,
21 actually observed days, 20 verified settled round trips and the unchanged
fee-adjusted risk, holdout, and manual signoff requirements.

## Testing limitations

GitHub Actions jobs are blocked by the repository owner's billing lock,
so workflow status alone does not establish test outcomes. Added regression
tests cover fresh observation provenance, idempotence, loss of a market
observation, separate account ledgers, fee accounting and CSV export. Runtime
deploy and HTTP endpoints must still be smoke tested.
