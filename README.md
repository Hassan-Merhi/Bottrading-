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
- moves test-passed candidates into paper trading (up to `paper.max_bots`);
- fetches live Kraken candles, checks them, and runs each bot: stop sync,
  target / exit signal, entries on freshly closed candles;
- enforces the daily loss limit and the kill switch, checks the graduation
  gate (30+ days, profitable after fees, drawdown < 10%, 20+ trades, beat
  holding), returns live bots that broke their limits to paper, and gives
  approved bots a live allocation when LIVE is unlocked;
- nightly: evolution (own process), daily Telegram report, database backup.

Other commands: `report`, `digest`, `fetch-mood`, `backup`, `watchdog` (run it
from an external scheduler to catch a dead machine), `dashboard`, `confirm-live`.

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

All of these only look at days that have fully closed, so a backtest never
sees the future (tests check this).

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

Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` for alerts, the daily report,
`/status`, and the Approve / Decline buttons on graduation alerts. Only your
chat is listened to. Set `DASHBOARD_TOKEN` to approve from the dashboard too.

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

- Verify `taker_fee_pct` and `min_order` in `bots/config.yaml` against Kraken's
  current fee schedule and pair minimums. Live orders also use Kraken's
  reported minimums, whichever is stricter.
- `starting_balance_usd` / `paper.allocation_usd` are $20 (the live per-bot
  cap), so tiny positions get refused just like on the exchange.
