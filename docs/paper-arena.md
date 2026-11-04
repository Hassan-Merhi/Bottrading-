# Paper arena: evolved bots trade fake money and evolution learns from it

## Why

The real-money path is deliberately strict: a bot must pass every walk-forward
window, a robustness check, validation and a one-time test before it may even
paper-trade on the graduation path. Every nightly run has ended with "Nothing
graduates", which is the right answer for real money, but it also meant no
evolved bot ever traded forward and the system never learned anything from
live prices. Evolution restarted each night from random bots with only the
backtest graveyard as memory.

The arena separates the two jobs. The real-money gates are unchanged. The
arena is where bots practise.

## How it works

1. **Admission.** When an evolution run finishes, its final generation is
   ranked and the best bots fill the free arena slots (`paper_arena.max_bots`,
   default 20), capped per coin and per entry rule so twenty SOL EMA crosses
   do not crowd out everything else. A genome enters the arena once; one that
   retired is never re-admitted.
2. **Trading.** Every worker loop, each arena bot looks at the latest closed
   Kraken candle of its own timeframe (15m, 1h or 4h) and acts on its signals
   with `allocation_usd` (default $100) of fake money. Fills are simulated at
   the observed price with the taker fee, spread and slippage; no intrabar
   best-case fills; missed candles are never replayed. This is the same
   simulation the research league uses.
3. **Retirement.** A bot whose drawdown reaches `retire_drawdown_pct` (25%),
   or that is under water after `retire_min_trades` (10) forward trades, is
   retired. Its genome goes into the graveyard with the cause
   "lost money forward (paper arena)" and its forward results, so the
   graveyard penalty steers newcomers away from that region of strategy space.
4. **Seeding.** A bot that is ahead after `winner_min_trades` (5) forward
   trades is a forward winner. The next evolution run puts forward winners at
   the front of its carried-over population, ahead of the previous run's
   backtest elite, because real prices have said something about them that a
   backtest cannot.

So the loop is: evolve, trade forward, bury the losers, breed from the
winners, evolve again.

## What it is not

- Not a path to real money. Arena bots never enter the graduation paper
  slots, never create an approval, and never touch an exchange. The Phase 4
  and 5 gates and the risk guard are untouched.
- Not a profit claim. A bot being a cent ahead after five trades is weak
  evidence, which is why it only earns a seat in the next run's starting
  population, not a relaxation of any gate.
- Not free of fees. Arena bots pay the same 0.80% taker fee as everything
  else, so most of them will lose and retire. That is the information the
  graveyard needs.

## Where to look

- Dashboard `/arena`: bots trading, each one's strategy, days, trades, result,
  drawdown, and the retired list with reasons. API: `/api/arena`, and
  `paper_arena` inside `/api/status`.
- Daily Telegram report: one line with active, ahead, winners, retired, net.
- Bot detail pages under `/bots` work for arena bots too, since they share the
  paper account tables.
- Graveyard: filter the diagnosis "lost money forward (paper arena)".

## Settings

```yaml
paper_arena:
  enabled: true
  max_bots: 20
  allocation_usd: 100
  max_pair_share: 0.3
  max_family_share: 0.3
  retire_drawdown_pct: 25
  retire_min_trades: 10
  winner_min_trades: 5
```

Raise `max_bots` for more forward evidence per night; each bot costs one
candle fetch per loop, so keep it in line with the worker's poll interval.
