"""Read-only proof of observed simulated paper results, never a live-trading gate.

This module reads ONLY the isolated lab_* tables. Missing observations are never
interpolated, fabricated or filled with historical data. Trades are recorded at
the actual time the paper simulator saw a Kraken ticker price.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


def _tables_available(conn: sqlite3.Connection) -> bool:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('lab_accounts','lab_marks','lab_fills')")}
    return {"lab_accounts", "lab_marks", "lab_fills"} <= names


def _benchmark(price: float, start_price: float, capital: float, cfg: dict) -> float:
    """Net passive buy/hold/liquidate using the SAME conservative paper costs."""
    if price <= 0 or start_price <= 0 or capital <= 0:
        return capital
    fee = float(cfg["backtest"]["taker_fee_pct"]) / 100
    impact = (float(cfg["backtest"]["slippage_pct"]) / 100 +
              float(cfg["backtest"]["spread_bps"]) / 20000)
    entry = start_price * (1 + impact)
    qty = capital / (entry * (1 + fee))
    return qty * price * (1 - impact) * (1 - fee)


def _mark_rows(conn, name: str, limit: int) -> list[dict]:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lab_marks)")}
    observed = "observed_ms" if "observed_ms" in cols else "NULL AS observed_ms"
    origin = "quote_source" if "quote_source" in cols else "NULL AS quote_source"
    rows = conn.execute(
        "SELECT candle_ts, equity, mark_price, " + observed + ", " + origin +
        " FROM lab_marks WHERE name=? ORDER BY candle_ts DESC LIMIT ?",
        (name, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def forward_history(conn: sqlite3.Connection, cfg: dict, name: str = "all",
                    limit: int = 720, fills_limit: int = 100) -> dict:
    """Return bounded observed equity and real-time simulated fill history.

    'all' aggregates only timestamps witnessed for EVERY active research bot.
    It does not forward-fill one bot's missing observations to fake continuity.
    """
    limit = max(1, min(int(limit), 1440))
    fills_limit = max(1, min(int(fills_limit), 500))
    empty = {"enabled": bool((cfg.get("research_league") or {}).get("enabled")),
             "simulation": True, "real_money": False, "mode": "PAPER",
             "name": name, "bots": [], "observations": [], "fills": [],
             "stats": {"capital": 0, "net_pnl": 0, "realized_pnl": 0,
                       "completed_trades": 0, "fees": 0, "win_rate_pct": None,
                       "max_drawdown_pct": 0, "benchmark_net_pnl": 0,
                       "recorded_marks": 0, "observed_hours": 0, "observed_days": 0,
                       "open_positions": 0, "started_ms": None},
             "note": "Forward equity marks are created only after the experiment begins; "
                     "a model backtest is not forward evidence."}
    if not empty["enabled"] or not _tables_available(conn):
        return empty
    accounts = [dict(r) for r in conn.execute(
        "SELECT * FROM lab_accounts ORDER BY name")]
    available = [r["name"] for r in accounts]
    if name != "all" and name not in available:
        raise ValueError("Unknown research paper experiment")
    selected = accounts if name == "all" else [a for a in accounts if a["name"] == name]
    if not selected:
        return empty
    initial = float((cfg.get("research_league") or {}).get("allocation_usd", 20))
    marks_by_name = {a["name"]: _mark_rows(conn, a["name"], limit) for a in selected}
    if name == "all":
        # A portfolio mark is valid only if all three paper accounts have
        # observed that same finished hourly candle, never forward-filled.
        common = set.intersection(*(set(m["candle_ts"] for m in marks_by_name[a["name"]])
                                    for a in selected))
        dates = sorted(common)[-limit:]
        lookup = {a["name"]: {r["candle_ts"]: r for r in marks_by_name[a["name"]]}
                  for a in selected}
        rows = []
        for ts in dates:
            parts = [lookup[a["name"]][ts] for a in selected]
            observed = ([p["observed_ms"] for p in parts]
                        if all(p["observed_ms"] is not None for p in parts) else [])
            rows.append({
                "candle_ts": ts,
                "observed_ms": max(observed) if observed else None,
                "equity": sum(p["equity"] for p in parts),
                "benchmark_equity": sum(_benchmark(p["mark_price"], a["hold_start_price"],
                                                   initial, cfg)
                                         for a, p in zip(selected, parts)),
                "quote_source": "kraken_ticker" if all(
                    p["quote_source"] == "kraken_ticker" for p in parts) else "mixed_or_legacy"})
    else:
        acc = selected[0]
        rows = [{
            "candle_ts": p["candle_ts"], "observed_ms": p["observed_ms"],
            "equity": p["equity"],
            "benchmark_equity": _benchmark(p["mark_price"], acc["hold_start_price"],
                                           initial, cfg),
            "quote_source": p["quote_source"] or "legacy_unknown"}
            for p in marks_by_name[acc["name"]]]
    capital = initial * len(selected)
    for p in rows:
        p["net_pnl"] = p["equity"] - capital
        p["benchmark_net_pnl"] = p["benchmark_equity"] - capital
    keys = [a["name"] for a in selected]
    # Name membership is SQL-parameterized, not concatenated from user input.
    placeholders = ",".join("?" * len(keys))
    fill_rows = [dict(r) for r in conn.execute(
        "SELECT id, name, ts, side, qty, price, fee, pnl, reason "
        "FROM lab_fills WHERE name IN (" + placeholders + ") "
        "ORDER BY id DESC LIMIT ?", (*keys, fills_limit))]
    realized = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM lab_fills WHERE side='sell' "
        "AND name IN (" + placeholders + ")", keys).fetchone()[0]
    trades = sum(int(a["closed_trades"]) for a in selected)
    wins = sum(int(a["wins"]) for a in selected)
    fees = sum(float(a["fees"]) for a in selected)
    equity = sum(float(a["last_equity"]) for a in selected)
    valid_observed = [int(p["observed_ms"]) for p in rows if p["observed_ms"] is not None]
    days = {datetime.fromtimestamp(ts / 1000, timezone.utc).date() for ts in valid_observed}
    benchmark_pnl = rows[-1]["benchmark_net_pnl"] if rows else 0.0
    stats = {
        "capital": capital, "equity": equity, "net_pnl": equity - capital,
        "realized_pnl": float(realized), "fees": fees,
        "completed_trades": trades, "wins": wins,
        "win_rate_pct": (100 * wins / trades if trades else None),
        "max_drawdown_pct": max(float(a["max_drawdown"]) for a in selected) * 100,
        "benchmark_net_pnl": benchmark_pnl, "recorded_marks": len(rows),
        "observed_hours": len({int(v // 3_600_000) for v in valid_observed}),
        "observed_days": len(days),
        "open_positions": sum(float(a["qty"]) > 0 for a in selected),
        "started_ms": min(int(a["started_ms"]) for a in selected),
    }
    return {**empty, "bots": available, "observations": rows, "fills": fill_rows,
            "stats": stats}
