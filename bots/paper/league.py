"""Isolated forward-only research paper league.

No integration with deployed_bots, candidates, approvals, or RiskGuard live paths.
Prices come from CLOSED Kraken candles already observed by the market refresh.
All fills are simulated using conservative taker fees, spread and slippage.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import replace

import pandas as pd

from bots.data.fetch import TIMEFRAME_MS
from bots.data.loader import resample, series_from_df
from bots.strategy.genome import Genome
from bots.strategy.signals import compute_signals, stop_pct_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_accounts (
 name TEXT PRIMARY KEY,
 pair TEXT NOT NULL,
 genome_json TEXT NOT NULL,
 cash REAL NOT NULL,
 qty REAL NOT NULL DEFAULT 0,
 entry_price REAL,
 entry_cost REAL,
 entry_ts INTEGER,
 stop_price REAL,
 target_price REAL,
 peak_equity REAL NOT NULL,
 last_equity REAL NOT NULL,
 max_drawdown REAL NOT NULL DEFAULT 0,
 fees REAL NOT NULL DEFAULT 0,
 closed_trades INTEGER NOT NULL DEFAULT 0,
 wins INTEGER NOT NULL DEFAULT 0,
 hold_start_price REAL NOT NULL,
 last_candle_ts INTEGER NOT NULL,
 started_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_fills (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 ts INTEGER NOT NULL,
 side TEXT NOT NULL,
 qty REAL NOT NULL,
 price REAL NOT NULL,
 fee REAL NOT NULL,
 pnl REAL,
 reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_marks (
 name TEXT NOT NULL,
 candle_ts INTEGER NOT NULL,
 equity REAL NOT NULL,
 mark_price REAL NOT NULL,
 PRIMARY KEY(name, candle_ts)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def research_genomes() -> dict[str, Genome]:
    """Three explainable, fixed experiments; NOT validated or eligible for graduation."""
    base = dict(pair="BTC/USD", timeframe="1h", entry_type="ema_cross",
                fast_period=12, slow_period=48, rsi_low=35, rsi_high=68,
                breakout_window=24, stop_loss_pct=3.0, take_profit_pct=8.0,
                position_size_pct=100.0, max_trades_per_day=2,
                trend_filter="none", mood_min=0, mood_max=100,
                stop_mode="fixed", trail_pct=0, max_hold_candles=120)
    return {
        "BTC trend": Genome.from_dict(base),
        "ETH pullback": Genome.from_dict({**base, "pair": "ETH/USD", "entry_type": "rsi_reversion",
                                          "rsi_low": 38, "rsi_high": 70, "max_hold_candles": 72}),
        "SOL breakout": Genome.from_dict({**base, "pair": "SOL/USD", "entry_type": "breakout",
                                           "breakout_window": 20, "max_hold_candles": 60}),
    }


def _series(conn: sqlite3.Connection, pair: str, now: int):
    # Newest 1500 x 15-minute observations, never invented historical data.
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE pair=? AND timeframe='15m' AND ts + ? <= ? ORDER BY ts DESC LIMIT 1500",
        (pair, TIMEFRAME_MS["15m"], now)).fetchall()
    if len(rows) < 250:
        return None
    df = pd.DataFrame([tuple(r) for r in reversed(rows)],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    # Reject incomplete timestamps instead of treating a gapped bucket as executable.
    one_hour = resample(df, "15m", "1h")
    if len(one_hour) < 55:
        return None
    latest = int(one_hour.ts.iloc[-1])
    if now < latest + TIMEFRAME_MS["1h"] or now - (latest + TIMEFRAME_MS["1h"]) > 2 * TIMEFRAME_MS["1h"]:
        return None
    return series_from_df(pair, "1h", one_hour)


def _fill(conn, name, now, side, qty, price, fee, pnl, reason):
    conn.execute(
        "INSERT INTO lab_fills(name,ts,side,qty,price,fee,pnl,reason) VALUES(?,?,?,?,?,?,?,?)",
        (name, now, side, qty, price, fee, pnl, reason))


def tick(conn: sqlite3.Connection, cfg: dict, now: int | None = None,
         allow_entries: bool = True) -> dict:
    """Process each NEW closed hourly candle once; never replay missed simulated trades."""
    lab = cfg.get("research_league") or {}
    if not lab.get("enabled", False):
        return {"enabled": False, "processed": 0}
    now = int(now if now is not None else time.time() * 1000)
    ensure_schema(conn)
    initial = max(5.0, float(lab.get("allocation_usd", 20)))
    fee = float(cfg["backtest"]["taker_fee_pct"]) / 100
    # Full configured spread is paid over a round trip, half on each leg.
    impact = float(cfg["backtest"]["slippage_pct"]) / 100 + float(cfg["backtest"]["spread_bps"]) / 20000
    if not all(math.isfinite(x) and 0 <= x < 0.1 for x in (fee, impact)):
        raise ValueError("invalid paper execution cost model")
    processed = 0
    for name, genome in research_genomes().items():
        s = _series(conn, genome.pair, now)
        if s is None:
            continue
        candle = int(s.ts[-1])
        mark_price = float(s.close[-1])
        if not math.isfinite(mark_price) or mark_price <= 0:
            continue
        row = conn.execute("SELECT * FROM lab_accounts WHERE name=?", (name,)).fetchone()
        if row is None:
            # Begin now with no retroactive signals/fills or fabricated observed days.
            conn.execute("INSERT INTO lab_accounts("
                         "name,pair,genome_json,cash,peak_equity,last_equity,"
                         "hold_start_price,last_candle_ts,started_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                         (name, genome.pair, genome.to_json(), initial, initial,
                          initial, mark_price, candle, now))
            conn.execute("INSERT OR IGNORE INTO lab_marks VALUES(?,?,?,?)",
                         (name, candle, initial, mark_price))
            conn.commit()
            continue
        if candle <= row["last_candle_ts"]:
            continue
        cash, qty = float(row["cash"]), float(row["qty"])
        entry_cost, entry_price, entry_ts = row["entry_cost"], row["entry_price"], row["entry_ts"]
        stop, target = row["stop_price"], row["target_price"]
        total_fees, trades, wins = float(row["fees"]), int(row["closed_trades"]), int(row["wins"])
        peak, drawdown = float(row["peak_equity"]), float(row["max_drawdown"])
        entry, exit_ = compute_signals(s, genome)
        closing_reason = None
        if qty > 0:
            if mark_price <= stop:
                closing_reason = "stop observed at close"
            elif mark_price >= target:
                closing_reason = "target observed at close"
            elif genome.max_hold_candles and entry_ts and now - entry_ts >= genome.max_hold_candles * TIMEFRAME_MS["1h"]:
                closing_reason = "time exit"
            elif bool(exit_[-1]):
                closing_reason = "signal exit"
        exited = False
        if closing_reason:
            # No intrabar best-case fills: exit at the observed close with adverse costs.
            px = mark_price * (1 - impact)
            gross = qty * px
            f = gross * fee
            realized = gross - f - float(entry_cost)
            cash += gross - f
            total_fees += f
            trades += 1
            wins += int(realized > 0)
            _fill(conn, name, now, "sell", qty, px, f, realized, closing_reason)
            qty, entry_price, entry_cost, entry_ts, stop, target = 0.0, None, None, None, None, None
            exited = True
        if allow_entries and not exited and qty == 0 and bool(entry[-1]):
            px = mark_price * (1 + impact)
            budget = cash * genome.position_size_pct / 100
            size = budget / (px * (1 + fee))
            order_min = cfg["backtest"]["min_order"].get(genome.pair, {})
            if size * px >= float(order_min.get("min_cost_usd", 5)) and size >= float(order_min.get("min_qty", 0)):
                f = size * px * fee
                entry_cost = size * px + f
                cash -= entry_cost
                qty, entry_price, entry_ts = size, px, now
                stop = px * (1 - stop_pct_now(s, genome) / 100)
                target = px * (1 + genome.take_profit_pct / 100)
                total_fees += f
                _fill(conn, name, now, "buy", qty, px, f, None, "signal entry")
        liquid_value = qty * mark_price * (1 - impact) * (1 - fee)
        equity = cash + liquid_value
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak) if peak > 0 else drawdown
        conn.execute(
            "UPDATE lab_accounts SET cash=?,qty=?,entry_price=?,entry_cost=?,entry_ts=?,"
            "stop_price=?,target_price=?,peak_equity=?,last_equity=?,max_drawdown=?,"
            "fees=?,closed_trades=?,wins=?,last_candle_ts=? WHERE name=?",
            (cash, qty, entry_price, entry_cost, entry_ts, stop, target, peak, equity,
             drawdown, total_fees, trades, wins, candle, name))
        conn.execute("INSERT OR IGNORE INTO lab_marks VALUES(?,?,?,?)",
                     (name, candle, equity, mark_price))
        processed += 1
    conn.commit()
    return {"enabled": True, "processed": processed}


def snapshot(conn: sqlite3.Connection, cfg: dict) -> dict:
    if not (cfg.get("research_league") or {}).get("enabled", False):
        return {"enabled": False, "bots": [], "pnl": 0, "real_money": False}
    # If deployment has not initialized the research DB yet, don't show a crash.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lab_accounts'").fetchone():
        return {"enabled": True, "bots": [], "pnl": 0, "real_money": False}
    initial = float((cfg.get("research_league") or {}).get("allocation_usd", 20))
    bots = []
    for row in conn.execute("SELECT * FROM lab_accounts ORDER BY name"):
        r = dict(row)
        pnl = r["last_equity"] - initial
        bots.append({"name": r["name"], "pair": r["pair"], "equity": r["last_equity"],
                     "pnl": pnl, "return_pct": 100 * pnl / initial,
                     "fees": r["fees"], "trades": r["closed_trades"], "wins": r["wins"],
                     "max_drawdown_pct": 100 * r["max_drawdown"],
                     "in_position": r["qty"] > 0, "started_ms": r["started_ms"],
                     "last_candle_ms": r["last_candle_ts"]})
    return {"enabled": True, "real_money": False, "simulation": True,
            "allocation_usd": initial, "bots": bots,
            "capital": initial * len(bots), "pnl": sum(b["pnl"] for b in bots),
            "fees": sum(b["fees"] for b in bots),
            "completed_trades": sum(b["trades"] for b in bots)}
