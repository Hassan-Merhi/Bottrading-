# Evolving Crypto Trading Bots

A population of rule-based, long-only spot trading bots evolves on Kraken
historical data. Losers are retired to a graveyard with a readable cause of
death, and new bots are steered away from repeating those mistakes. Winners
breed. Survivors are checked once on unseen data, then paper-trade on live
prices. Only bots that pass the paper gate **and** that you approve can touch
real money, and only inside hard caps. See `BLUEPRINT.md` for the full spec.

**The system runs in PAPER mode (fake money, real prices) by default and stays
there until you deliberately unlock LIVE.** Live trading is never risk-free:
when unlocked, real money can be lost up to the configured caps. Backtest and
paper results never guarantee future profits.

## Setup

```bash
pip install -r requirements.txt
python -m pytest            # 133 tests, including the section 6.8 risk proofs
```

## Pipeline

```bash
# 1. History. Kraken's OHLC endpoint only serves the latest 720 candles, so:
python -m bots.main fetch          # 3 years of 15m candles + Fear & Greed, ~2 minutes
                                   # (data.history_source: Coinbase USD candles by default,
                                   # or kraken_trades: Kraken's own trades, hours per pair), or
python -m bots.main import-csv XBTUSD_15.csv --pair BTC/USD --timeframe 15m
python -m bots.main status

# 2. Evolution (200 bots x 100 generations). Writes runs/<id>.md.
python -m bots.main evolve
python -m bots.main evolve --synthetic   # random-walk data: pipeline check only

# 3. The always-on worker + dashboard (http://localhost:8000)
python -m bots.main run
```

The worker, every `worker.poll_seconds`:
- independently refreshes closed Kraken 15-minute candles for **every whitelisted pair**
  about every `worker.market_refresh_seconds` (default: five minutes), even if
  there are zero active trading bots; it runs the refresh as a separate short
  process, avoiding overlap with evolution and initial history backfills;
- checks per-pair freshness and recent 15-minute candle continuity; any stale,
  missing or gapped data, or a failed refresh, blocks **new entries across the
  system** until the feeds recover, without disabling existing risk-reducing exits;
  `/api/status` exposes `market_data.pairs` and the dashboard shows how many
  of the configured pairs are fresh. Nightly evolution waits for healthy data;
- moves test-passed candidates into paper trading (up to `paper.max_bots`,
  at most `max_per_pair` per coin and `max_per_entry_type` per rule, so the
  slots teach something about different strategies);
- fetches live Kraken candles, checks them, and runs each bot: stop sync,
  target / exit signal, entries on freshly closed candles;
- enforces the daily loss limit and the kill switch, checks the graduation
  gate (30+ days, profitable after fees, drawdown < 10%, 20+ trades, beat
  holding), returns live bots that broke their limits to paper, and gives
  approved bots a live allocation when LIVE is unlocked;
- nightly: evolution (own process), daily Telegram report, database backup.

Other commands: `report`, `digest`, `fetch-mood`, `backup`, `watchdog` (run it
from an external scheduler to catch a dead machine), `dashboard`, `confirm-live`.

## Phase 7: controlled pilot admission — disabled until separate authorization

The first funded pilot is **not launched**. The deployed configuration
remains `MODE=PAPER`, `live_enabled: false`, `risk.max_total_capital_usd: 0`,
and `pilot.max_total_capital_usd: 0`. Phase 7 adds extra non-bypassable-in-code
pilot signoffs, strict initial per-bot/capital/loss/drawdown ceilings, and a
binding certificate that an individual bot passed Phase 4 and genuinely
completed the Phase 5 forward paper trial before any live account is created.
It is not enough to manually set a bot's database status to approved.
Certification is bound to the actual paper account, strategy and live epoch.

Use the read-only `/api/pilot/readiness` endpoint or the dashboard to view
pilot blockers. Tests use simulated exchange responses only; a funded live
pilot cannot safely start without real Kraken reconciliation, ongoing
monitoring and separate owner approval. See
[Phase 7 pilot operating plan](docs/phase7-controlled-pilot.md).

## Phase 6: fail-closed live infrastructure (no live activation)

This phase prepares **only** the safety infrastructure. The production default
is still `MODE=PAPER`, `live_enabled: false`, and **$0** authorized live capital.
No candidate has yet completed the independent forward-paper qualification.

Before live exposure, RiskGuard queries Kraken for every open exchange order
and verifies each is accounted for by a recorded entry or protective stop.
Untracked orders, missing stops, ambiguous API results and uncertain stop
cancellations cause a persistent `live_reconciliation_halt` lock. Kill
handling retains unsettled positions instead of falsely writing them off;
stops are read back after placement. Read-only CLI:
`python -m bots.main audit-live`. Explicit owner-only acknowledgment after
the order book has been reconciled:
`python -m bots.main audit-live --acknowledge`. Neither command places
orders or overrides the separate live-capital confirmation gates.

See [Phase 6 safety and recovery runbook](docs/phase6-live-safety-runbook.md)
for credential privileges, operational response and the additional
**real Kraken API / asset reconciliation** work required before a funded pilot.
Passing CI with a simulated Kraken connector is not proof of live readiness.

## Phase 5: forward paper trading and independent performance evidence

A candidate with a valid Phase 4 audit automatically enters **PAPER** trading
when a diverse paper slot is open and all public market feeds are healthy.
The bot uses only simulated capital. The system stores actual witnessed
quotes, simulated equity, after-fee buy/sell fills, drawdown, and buy-and-hold
baseline snapshots in SQLite. Data is upserted at most once per UTC hour,
survives process restarts, and is never retroactively filled for hours when the
worker was offline.

The paper graduation gate now requires ALL of these before an owner can
consider unlocking real trading: 30 **actually elapsed** calendar days since
account start AND first recorded market observation, >=21 distinct UTC days
with witnessed market prices, no monitoring gap above 72 hours, a fresh validated
price, >=20 reconciled completed buy/sell transactions (not a manually bumped
trade counter), positive simulated P&L after fees, realized profit factor >=1.10,
maximum drawdown below 10%, and outperformance of fee/spread/slippage-adjusted
buy-and-hold. An API failure cannot produce a new forward observation or
qualify a bot. A deep paper drawdown causes automatic retirement.

The dashboard displays each paper bot's verified days, round trips, profit,
fees, monitoring gap, and benchmark; Telegram daily reports include each
paper bot's forward progress. Read-only endpoints:
`/api/status` contains `paper_forward`; `/api/paper/{bot_id}/history`
returns the last 168 hourly snapshots by default (up to 720).
Simulated results are not actual exchange execution and **do not imply
guaranteed or proven future profit**. If the Phase 4 gates produce zero
candidates, there is nothing to forward-test: do not claim that 30 days have
already passed or create fake sample trades to satisfy the gate.

A paper graduation is just *eligible for owner review*; Phase 6 must harden
real-money credentials, reconciliation, deployment and kill switches before
any owner-authorized real trading. `live_enabled: false` and total permitted
live capital of zero remain unchanged.

## Trader knowledge the bots can evolve to use

Each is a gene, so evolution decides whether it helps. Every one must pass the
same backtest, unseen-data check and 30-day paper gate as any other rule.

- **Market mood:** the Crypto Fear & Greed Index (daily, history from 2018).
  Bots can restrict entries to a mood range, e.g. "only buy when fearful".
  `python -m bots.main fetch-mood` downloads the history; the worker refreshes
  it hourly. Missing or stale mood data blocks mood-filtered entries.
- **Trend filters:** only enter when the coin itself, or BTC, closes above its
  N-day moving average (20–200 days).
- **Volume and volatility:** two extra entry types: volume spike (volume N× its
  average on a green candle making a new short-term high) and volatility
  squeeze breakout (Bollinger band width in its lowest 20%, then a close above
  the upper band).
- **Trailing stop and time exit:** `trail_pct` raises the stop to a fixed
  distance under the highest price since entry (0 = off); `max_hold_candles`
  closes a trade after that many candles (0 = off). In paper and live trading
  both run in software on top of the hard stop resting on the exchange, and
  every sale still goes through RiskGuard.
- **Volatility-sized stops:** a bot's stop is either a fixed percent or
  `atr_mult` × ATR(`atr_period`) below entry, decided at the signal candle, so it
  widens in wild markets and tightens in calm ones. The ATR stop is always
  clipped into the fixed stop's 0.5–5% range, so one trade never risks more
  than the widest fixed stop. Paper and live entries use the same number.

All of these only look at days that have fully closed, so a backtest never
sees the future (tests check this).

## Phase 2: conservative execution simulation

Backtest results now use explicit Kraken-tier maker/taker fees, assumed full spread
(`backtest.spread_bps`, default 10 basis points), and slippage, with both market
price and paper fills using `bots.execution.model.market_fill_price`. These are
**assumptions**, not measured Kraken order-book execution costs. Widen them for
stress tests rather than treating any one backtest return as guaranteed.

- A limit buy is priced at a *known* prior-candle close minus the configured
  `execution.limit_bid_offset_bps`. A two-minute resting buy is **not** assumed
  to have filled just because a 15-minute, 1-hour, or 4-hour candle touched
  its price at some unknown later time. An opening cross is charged the **taker**
  rate, and a maker fill requires time resolution no coarser than the timeout.
  Unconfirmable orders are counted in `unfilled_limit_orders`. This approach
  is intentionally conservative and can undercount genuine fills.
- Stop losses represent hard exchange-resident orders and can trigger
  intrabar; a gap through the level exits at the worse opening price.
  Targets and trailing signals are software-managed in this system. Historical
  testing therefore requires a close-price trigger and a sale at the following
  open rather than granting a fill at an unobserved intrabar high.
- Paper trading no longer reuses old OHLC lows to fill newly entered orders.
  It requires an observed executable quote to cross the resting buy, respects
  the limit price and expiration, and includes the same assumed spread and
  slippage for market fills. Historical backtests, paper fills, and real
  exchange fills still differ in latency, depth, queue position, gaps and
  partial fills. Do not describe this as tick-level or order-book replay.
- The risk guard sizes market buys using spread plus slippage and reserves
  taker fees; limit buys reserve the full bid and taker fee, so paper orders
  cannot silently overspend the bot's allocation. All live capital permissions
  and confirmation requirements are unchanged.

For materially stronger claims about fill quality, record or source timestamped
quotes/trades (ideally order-book depth) and replay the exact placement,
two-minute timeouts, partial fills and cancellation acknowledgments.
Validation against real future paper execution remains required.

## Phase 4: independent historical verification (not proof of future earnings)

The training period uses multiple chronological walk-forward windows and must
contain at least two distinct market regimes. After training, candidates are
screened on a separate validation period. The first 24 hours following each
validation and test boundary are embargoed before simulated orders begin.
Validation requires a positive return **after** trading costs, at least 12
filled trades, maximum drawdown <=10%, profit factor >=1.15, consistency in at
least two of three validation slices, and a positive result when maker/taker
fees are multiplied by 1.25 and spreads/slippage are doubled.

Only the survivors can spend a one-time unseen-data test: >=20 filled trades,
positive net result, maximum drawdown <=10%, profit factor >=1.15,
nonnegative Sharpe and returns above net buy-and-hold. This is deliberately
hard; **zero qualified candidates is an acceptable result**. The original
Kraken fee assumptions and real execution limits remain unchanged.

For every new test the system persists the market pair, timeframe, exact
period and candle count, SHA-256 of OHLCV test data, a SHA-256 over the fee
configuration / verification settings AND tested source code, plus the
failure reasons. Tests are never recomputed for the same genome, even if a
config, code or test period changes; mismatched legacy evaluations cannot
be described as current-phase success and do not automatically deploy to
paper. Only five new candidates per roughly 30-day holdout epoch may
consume that historical test. The quota **limits** multiple-testing bias;
it does not remove it, and earlier historical trials are not magically
made out-of-sample by a software change. Only newly collected forward
market data can add genuinely unseen evidence.

A limit entry that cannot be proved from available OHLC bars remains
unfilled. The simulator cannot reproduce the exchange order book, exact
queue priority, partial fills or true two-minute tick timing. The 30-day
forward paper check and explicit owner authorization for live trading still
apply. All historical results are hypothetical, not an assurance of profits.

## The gate before paper trading

Evolution scores every bot on walk-forward windows of the training period.
Before a top bot may spend its one-time check on the unseen test period it must
(`evolution.gate`): have made money in **every** training window on its own, so
a bot that only worked in one lucky stretch never reaches paper trading; still
make money in most of its nearby variants (every numeric gene nudged by up to
10%), so a lucky setting is not mistaken for an edge; and show trade results
that beat chance (mean trade return over its standard error of at least 2.0
across 10+ trades). The run report lists each held-back bot with its reason.
The test check itself passes only with a profit, at least `min_test_trades` trades
and a drawdown under `max_test_drawdown_pct`; the run report names the rule a
finalist failed. Each bot's daily equity is kept in `equity_history`, which the
dashboard draws as a small chart on every bot card.

## AI analyst (information only)

Every morning (`analyst.hour_utc`) the worker writes a market briefing from
crypto news RSS feeds and any public Telegram channels you list in
`analyst.telegram_channels` (read from their public web page; no account or
group joining). With `ANTHROPIC_API_KEY` set, Claude writes it: mood, main
themes, hype and pump-and-dump red flags, things to watch. Without a key you
get a plain headline digest. It arrives on Telegram (`/digest` resends it) and
on the dashboard.

The analyst cannot trade. No trading module imports it, it has no access to
RiskGuard, accounts or exchanges, and the sources are handed to Claude as
untrusted data with instructions not to follow anything inside them. It gives
no buy or sell calls. Run it by hand with `python -m bots.main digest`.

## Telegram and dashboard

Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` for alerts, the daily report, the
nightly evolution summary (with the full report attached), and the Approve /
Decline buttons on graduation alerts. Only your chat is listened to. Set
`DASHBOARD_TOKEN` to approve from the dashboard too.

Commands you can send the bot: `/status`, `/bots` (every running bot with its
return), `/digest`, `/pause` (no new trades until `/resume`; exits and stops
keep working), and `/kill confirm` (close every live position, cancel all
orders, lock live mode until `confirm-live`), and `/evolve` (start a run now).
If a nightly run dies (for example the host runs out of memory) you get an
alert, and the worker retries it once that day.

**Off-site backup.** The nightly backup also sends a compressed copy of the
database without price candles (a few hundred KB) to your Telegram chat
(`worker.offsite_backup`). If the server's disk is ever lost, restore by
unzipping it over `bots.db` in `BOTS_STATE_DIR`; candles re-download.
`python -m bots.main backup --send` does the same on demand.

## Risk rules (section 6), enforced in `bots/risk/guard.py`

- **Single gate.** `RiskGuard.place_order()` is the only code that calls
  exchange order methods. Everything else gets a `ReadOnlyExchange`. A test
  scans the whole code base for order / cancel / withdraw calls elsewhere.
- **Mode lock.** LIVE needs all of: `MODE=LIVE` env, `live_enabled: true`,
  `LIVE_CONFIRMED.txt` (in `BOTS_STATE_DIR`, default the project root)
  containing exactly `max_total_capital_usd`, a verified Kraken key, no kill
  switch and no crash lock. Anything missing or unclear: PAPER.
- **Money limits** in `config.yaml` with a hard $100 ceiling in code. Daily loss
  limit closes live positions and pauses until the next UTC day. The total
  drawdown kill closes everything, cancels all orders, revokes
  `LIVE_CONFIRMED.txt`, and stays off until you run `confirm-live` with a new file.
- **Trades:** spot, long only, whitelisted pairs, one position per bot, any
  order flag refused, a stop-loss on Kraken right after each live entry (or the
  position is closed at once).
- **House money:** at 2x its allocation a live bot locks its original stake.
- **API key:** must be query + trade only. At startup the worker proves the key
  cannot withdraw (a withdrawal-permission query must be refused) or it refuses
  to run.
- **Fail-safes:** API errors or bad data halt new entries (exits still work);
  an unclean restart comes back in PAPER until `confirm-live`; heartbeat alert
  after 2 hours of silence.

Locks never block reducing risk: exits and stop handling keep working in
PAPER mode for any live position that still exists.

## Unlocking LIVE (only after reviewing 30+ days of paper results)

1. Create a Kraken API key with Query Funds, Query Open/Closed Orders & Trades,
   Create & Modify Orders, Cancel/Close Orders. **No withdraw permission.**
2. Set `KRAKEN_API_KEY` / `KRAKEN_API_SECRET`.
3. In `bots/config.yaml`: `live_enabled: true` and `max_total_capital_usd`
   (at most 100).
4. Write that same number into `LIVE_CONFIRMED.txt` in `BOTS_STATE_DIR`.
5. Set `MODE=LIVE` and restart. The dashboard banner turns red: LIVE.
6. Approve graduating bots from Telegram or the dashboard.

## Deploying (PAPER)

`Dockerfile` and `render.yaml` describe one always-on service (worker +
dashboard) with a persistent disk at `/data` for the database, backups and
`LIVE_CONFIRMED.txt`. It needs a paid instance; free instances sleep and have
no disk. Leave the Kraken keys unset while paper trading.

## Before trusting results

- Fees are Kraken Pro's Tier 1 rates since 9 July 2026: `maker_fee_pct` 0.40%
  and `taker_fee_pct` 0.80%. Entries rest as limit buys at the bid
  (`execution.entry_order: limit`) and pay the maker rate when they fill;
  every exit is a market order at the taker rate. A round trip costs about
  1.25% with slippage, which is still why most strategies die. In the
  backtest a limit entry counts as filled only when the candle later trades
  below its open, so bots cannot assume fills they would not have got. An
  entry that has not filled after `limit_timeout_s` is withdrawn. Re-check
  the fee schedule occasionally.
- `min_order` sizes are deliberately strict guesses. Live orders use the larger
  of the config value and Kraken's reported minimum.
- The pair whitelist is BTC, ETH, SOL, XRP, LINK, ADA and DOGE against USD.
  Adding a pair later is safe: the worker downloads only the missing history.
- `starting_balance_usd` / `paper.allocation_usd` are $20 (the live per-bot
  cap), so tiny positions get refused just like on the exchange.
