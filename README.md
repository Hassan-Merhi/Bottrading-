# Evolving Crypto Trading Bots

A population of rule-based, long-only spot trading bots evolves on Kraken
historical data. Losers are retired to a graveyard with a readable cause of
death, and new bots are steered away from repeating those mistakes. Winners
breed. See `BLUEPRINT.md` for the full specification.

**Status: phases 1–4 built** (data, backtester, genome + strategies, evolution
+ graveyard). RiskGuard, paper trading, reporting and live trading (phases 5–9)
are not built yet. **Nothing in this code base can place an order.**

Backtest results are simulations. They never guarantee future profits.

## Setup

```bash
pip install -r requirements.txt
python -m pytest
```

## Usage

```bash
# 1. Get history. Kraken's OHLC endpoint only serves the latest 720 candles,
#    so years of history come from one of these:
python -m bots.main fetch          # rebuild 15m candles from Kraken's public trade history
                                   # (slow: hours per pair, resumable if interrupted)
python -m bots.main import-csv XBTUSD_15.csv --pair BTC/USD --timeframe 15m
                                   # or import Kraken's downloadable OHLCVT CSVs (fast)
python -m bots.main update         # top up the latest candles
python -m bots.main status         # show what is stored

# 2. Evolve (200 bots x 100 generations by default, see config.yaml)
python -m bots.main evolve
python -m bots.main evolve --synthetic   # random-walk data, checks the pipeline only
```

Each run prints per-generation best / median / worst fitness and writes a
report to `runs/<run_id>.md`. Everything (candles, generations, graveyard,
candidates) is stored in `bots/data/bots.db`.

## How evolution avoids fooling itself

- Data is split by time: **train** (oldest 60%), **validation** (next 20%),
  **test** (newest 20%).
- Every generation is scored only on train, across consecutive walk-forward
  windows. Fitness = mean(window Sharpe) − 0.5 × std, minus penalties for
  drawdown above 15%, fewer than 20 trades, and failing to beat holding, minus
  a graveyard-similarity penalty.
- Validation is used once, at the end, to choose finalists from the final
  generation. Each finalist gets exactly one test-set check, stored per genome
  and never recomputed. Passing (profitable after fees) only makes a bot
  eligible for paper trading.
- The backtester fills at the next candle's open (no look-ahead), charges taker
  fees and slippage on both fills, assumes the stop hit first when stop and
  target share a candle, fills gapped stops at the open, and refuses orders
  under Kraken's minimums. Tests verify each of these.

## Before trusting results

- Verify `taker_fee_pct` and `min_order` in `bots/config.yaml` against Kraken's
  current fee schedule and pair minimums.
- `starting_balance_usd` is 20 (the live per-bot cap), so tiny positions get
  refused just like they would be on the exchange.
