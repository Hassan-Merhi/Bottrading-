# Evolving Crypto Trading Bots — Build Blueprint

> **For Claude Code:** Build this project exactly as specified, in the phase order at the bottom. The risk rules in section 6 are non-negotiable: never weaken, skip, or make them optional. Every order must pass through `RiskGuard`. Ask the owner before changing anything in section 6.

---

## 1. Goal

A population of small trading bots competes on Kraken crypto markets (BTC, ETH, and a few liquid pairs). Bots that lose are retired, and their failures are logged so new bots avoid repeating them. Bots that win "breed": they pass on their strategy with small variations. Only bots proven in simulation and paper trading ever touch real money, and then only within hard limits the owner sets.

**Owner's priority: never lose money.** The system enforces this as follows:
- **Default and permanent mode is PAPER (fake money, real prices) = $0 risk.** The system stays here until the owner explicitly unlocks live trading.
- In LIVE mode, real loss is possible but **hard-capped** (section 6). Live trading is never risk-free, and the dashboard must say so plainly.

---

## 2. Tech stack

- **Python 3.11+**
- **ccxt** for the Kraken connection (public data plus authenticated trading)
- **SQLite** locally; Postgres optional for deployment
- **pandas / numpy** for indicators and backtests
- **FastAPI + a simple HTML page** for the dashboard
- **Telegram bot** for phone alerts and the daily report
- **pytest** for tests
- All secrets in environment variables: `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. Never commit them.

### Folder structure
```
bots/
  config.yaml          # all limits and settings (section 6 values live here)
  data/                # market data fetch + storage
  strategy/            # genome definition + signal generation
  backtest/            # backtester with fees and slippage
  evolution/           # population, fitness, breeding, death, graveyard
  paper/               # live-price paper trader
  live/                # real trading (locked by default)
  risk/                # RiskGuard: the only path to place an order
  report/              # dashboard + Telegram
  tests/
  main.py              # scheduler / orchestrator
```

---

## 3. The bot "genome" (its strategy DNA)

Each bot is a set of parameters. Strategies are rule-based, simple, and explainable.

| Gene | Range / options |
|---|---|
| pair | BTC/USD, ETH/USD, SOL/USD (liquid pairs only) |
| timeframe | 15m, 1h, 4h |
| entry_type | ema_cross, rsi_reversion, breakout, macd |
| fast/slow periods | 5–50 / 20–200 |
| rsi_low / rsi_high | 15–40 / 60–85 |
| breakout_window | 10–100 candles |
| stop_loss_pct | 0.5%–5% (required, every trade) |
| take_profit_pct | 1%–15% |
| position_size_pct | 10%–100% of the bot's own allocation |
| max_trades_per_day | 1–10 |

**Spot only. Long only.** No shorting, margin, leverage, or futures, ever. This guarantees a bot can never lose more than the money it holds.

---

## 4. Evolution: birth, death, learning, breeding

### Data split (prevents fooling ourselves)
Download 2–4 years of candles. Split by time:
- **Train** (oldest ~60%): bots evolve here
- **Validation** (next ~20%): used to pick survivors
- **Test** (newest ~20%): touched only once per final candidate. If it fails here, it doesn't graduate.

Use walk-forward windows, so a strategy must work across several periods, not one lucky stretch.

### Backtest realism (mandatory)
- Kraken fees configurable in `config.yaml`. Default to the taker fee and verify against Kraken's current fee schedule.
- Slippage: 0.05% per fill, configurable
- Kraken minimum order sizes respected (e.g. $5 / 0.0001 BTC)
- No look-ahead: signals use only closed candles

### Fitness score
Fitness = risk-adjusted return (Calmar or Sharpe) on validation, after fees, with penalties for:
- max drawdown above 15%
- fewer than 20 trades (too little evidence)
- failing to beat simply holding the coin

### Death
A bot dies if any of these hold:
- drawdown exceeds its limit
- below starting balance after N trades
- bottom 50% of the population each generation

### Learning from mistakes: the Graveyard
Every dead bot is stored with its genome, cause of death (e.g. "stopped out repeatedly in sideways market"), and the market conditions at the time. When creating new bots:
- penalize genomes too similar to graveyard entries that died for the same reason
- store a short readable failure summary per generation, shown on the dashboard

### Breeding
Top 10–20% survive. New bots come from:
- **crossover**: genes mixed from two winners
- **mutation**: small random changes (e.g. ±10% on a period)
- **10% random newcomers** to avoid everyone becoming identical

Population: 200 bots, 100+ generations per run. Log each generation's best, median, and worst.

---

## 5. Pipeline: from simulation to real money

1. **Backtest evolution** — $0 risk
2. **Test-set check** — the candidate must stay profitable after fees on never-seen data
3. **Paper trading** — $0 risk. Live Kraken prices, simulated fills, minimum **30 days**
4. **Graduation gate.** All of these must be true:
   - profitable after fees over 30+ paper days
   - max paper drawdown under 10%
   - at least 20 paper trades
   - beat holding the coin over the same period
   - **owner approves manually** via the dashboard or Telegram button
5. **Live** (only if the owner has unlocked live mode) — capped risk under section 6

A live bot that breaks its limits returns to paper. It never gets more money automatically.

---

## 6. RISK RULES (non-negotiable; enforced in code)

### 6.1 Single gate
- `risk/guard.py` exposes `RiskGuard.place_order()`. **It is the only code allowed to call the exchange's order functions.** Add a test that fails if any other module imports or calls ccxt order methods.
- RiskGuard checks every rule below **before** every order. Any failure → order refused, logged, Telegram alert.

### 6.2 Mode lock
- `MODE=PAPER` is the default. If the setting is missing or unclear → PAPER.
- LIVE requires **all three**: `MODE=LIVE` in env, `live_enabled: true` in config, and a file `LIVE_CONFIRMED.txt` containing the owner's typed capital limit. Missing any one → PAPER.

### 6.3 Money limits (values in config.yaml, owner sets them)
- `max_total_capital_usd`: default **0** (paper only). The owner may raise it, never above **100**.
- `max_per_bot_usd`: default 20
- `max_live_bots`: default 3
- `daily_loss_limit_usd`: default 3. If hit → close positions, stop all live trading until next day, alert.
- `total_drawdown_kill_usd`: default 10. If total live losses reach this → close everything, cancel all orders, switch to PAPER, and require the owner to re-confirm. **Never auto-restart.**

### 6.4 Trade rules
- Every live trade must have a stop-loss order placed on Kraken immediately after entry. If the stop can't be placed → close the position at once.
- Spot only, long only, no leverage or margin. Reject any order with those flags.
- Liquid pairs only (from the config whitelist).
- No more than one open position per bot.

### 6.5 House-money rule (protects the owner's original money)
When a live bot's balance reaches **2× its starting allocation**, it sets aside its original stake as "protected" and never trades with it again. From then on, it only risks profits. Protected funds are shown separately on the dashboard.

### 6.6 API key safety
- Kraken API key permissions: **query + trade only. No withdrawal permission.** On startup, the bot checks that it cannot withdraw; if it can, it refuses to run and alerts the owner.
- The bot never moves money off the exchange.

### 6.7 Fail-safe behavior
- Lost connection, API errors, or unexpected data → stop opening new trades. Existing stop-losses remain on Kraken.
- Any crash → restart in PAPER until the owner confirms.
- Heartbeat alert to Telegram if the system hasn't reported in 2 hours.

### 6.8 Required tests
Unit tests proving: orders bypassing RiskGuard are impossible; LIVE can't activate with a missing condition; each limit stops trading when hit; leverage/short orders are rejected; kill switch closes everything and stays off.

---

## 7. Reporting (so the owner can chill)

**Daily Telegram message:** paper and live P&L, best bot, bots retired today and why, protected funds, any risk events.

**Instant alerts:** a bot ready to graduate (with approve/decline buttons), any risk rule triggered, kill switch, system down.

**Dashboard pages:** population leaderboard, generation progress chart, graveyard with causes of death, paper vs live results, current limits and mode (big banner: PAPER or LIVE), and a clear statement that live trading can lose money up to the configured caps.

---

## 8. Deployment

- Runs as one always-on worker (Render background worker, Replit deployment, or a small VPS).
- Evolution runs nightly; the paper and live traders run continuously.
- Database backups daily.

---

## 9. Build order (phases)

1. **Data:** fetch and store Kraken historical candles (public API, no keys needed).
2. **Backtester:** fees, slippage, minimums, no look-ahead. Tests.
3. **Genome + strategies:** the four entry types. Tests.
4. **Evolution engine + graveyard.** Run it and show results.
5. **RiskGuard + full test suite** (section 6.8), before any exchange order code exists.
6. **Paper trader** on live prices.
7. **Telegram + dashboard.**
8. **Deploy in PAPER mode.** Run 30+ days.
9. **Live module.** Locked by default, built only after the owner reviews paper results.

Stop and report to the owner after phases 4, 5, and 8.

## 10. Never
- Never add leverage, margin, futures, or shorting.
- Never let code other than RiskGuard place orders.
- Never auto-increase real-money limits.
- Never request or use withdrawal permissions.
- Never describe expected profits as guaranteed.
