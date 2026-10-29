"""Forward paper-trading evidence from actual observed quotes and persisted fills.

Hourly snapshots are append/update by real observation hour, not synthetic
backfilled days. The graduation clock starts at account creation; restarting
does not reset evidence, and changing a timestamp alone cannot produce sessions.
All figures are PAPER simulations; they do not establish live profitability.
"""
from __future__ import annotations

import math
import sqlite3

from bots.execution.model import market_fill_price
from bots.risk.ledger import DAY_MS

HOUR_MS = 3_600_000


def net_hold_return(start: float | None, end: float | None, cfg: dict) -> float | None:
    if (start is None or end is None or not math.isfinite(start)
            or not math.isfinite(end) or start <= 0 or end <= 0):
        return None
    bt = cfg["backtest"]
    slippage = float(bt["slippage_pct"]) / 100
    spread = float(bt.get("spread_bps", 0))
    fee = float(bt["taker_fee_pct"]) / 100
    buy = market_fill_price(start, "buy", slippage, spread) * (1 + fee)
    sell = market_fill_price(end, "sell", slippage, spread) * (1 - fee)
    return sell / buy - 1


def observe(conn: sqlite3.Connection, acc, price: float, now: int, cfg: dict) -> bool:
    """Record a valid forward quote without inventing observations for missed time."""
    if not math.isfinite(price) or price <= 0 or now < acc["started_at"]:
        return False
    from bots.risk.ledger import Ledger
    equity = Ledger.equity(acc, price)
    if not math.isfinite(equity) or equity < 0:
        return False
    conn.execute(
        "INSERT INTO paper_observations "
        "(account_id, hour, ts, price, equity, hold_return, fees, closed_trades, max_drawdown) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(account_id, hour) DO UPDATE SET "
        "ts=excluded.ts, price=excluded.price, equity=excluded.equity, "
        "hold_return=excluded.hold_return, fees=excluded.fees, "
        "closed_trades=excluded.closed_trades, max_drawdown=excluded.max_drawdown "
        "WHERE excluded.ts>=paper_observations.ts",
        (acc["account_id"], now // HOUR_MS, now, price, equity,
         net_hold_return(acc["hold_start_price"], price, cfg), acc["fees"],
         acc["trades"], acc["max_drawdown"]))
    conn.commit()
    return True


def evidence(conn: sqlite3.Connection, account_id: int, now: int) -> dict:
    """Return real recorded sessions and settled trades, never counters alone."""
    row = conn.execute(
        "SELECT COUNT(*) samples, COUNT(DISTINCT hour / 24) days,"
        "MIN(ts) first_ms, MAX(ts) last_ms "
        "FROM paper_observations WHERE account_id=? AND ts<=?",
        (account_id, now)).fetchone()
    fills = conn.execute(
        "SELECT SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END), "
        "COALESCE(SUM(fee),0), "
        "COALESCE(SUM(CASE WHEN side='sell' AND pnl>0 THEN pnl ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN side='sell' AND pnl<0 THEN -pnl ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN side='sell' THEN pnl ELSE 0 END),0) "
        "FROM fills WHERE account_id=? AND mode='paper' AND ts<=?",
        (account_id, now)).fetchone()
    hours = [int(r[0]) for r in conn.execute(
        "SELECT hour FROM paper_observations WHERE account_id=? AND ts<=? ORDER BY hour",
        (account_id, now))]
    max_gap = max((right - left for left, right in zip(hours, hours[1:])), default=0)
    buys, sells, fees, gross_wins, gross_losses, realized = fills
    count = min(int(buys or 0), int(sells or 0))
    factor = min(gross_wins / gross_losses, 100.0) if gross_losses > 0 else (
        100.0 if gross_wins > 0 else 0.0)
    return {
        "observed_days": int(row["days"] or 0),
        "observed_hours": int(row["samples"] or 0),
        "max_observation_gap_hours": max_gap,
        "first_observation_ms": row["first_ms"],
        "last_observation_ms": row["last_ms"],
        "verified_trades": count,
        "fees": float(fees), "realized_pnl": float(realized),
        "profit_factor": float(factor),
    }


def history(conn: sqlite3.Connection, account_id: int, limit: int = 168) -> list[dict]:
    """Last seven days of hourly history for diagnostics and report links."""
    rows = conn.execute(
        "SELECT ts, price, equity, hold_return, fees, closed_trades, max_drawdown "
        "FROM paper_observations WHERE account_id=? ORDER BY hour DESC LIMIT ?",
        (account_id, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]
