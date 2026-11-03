"""Paper cross-venue arbitrage against pre-positioned inventory.

The account starts with `size_usd` of cash AND `size_usd` worth of coin on
every venue, because real cross-venue arbitrage without inventory on both
sides needs a transfer that takes minutes and costs a withdrawal fee, by
which time the gap is gone. A trade is: buy on the cheap venue, sell the same
quantity on the dear venue, both as taker market orders at the walked book
price plus slippage. Fake money only; nothing here can reach an exchange.
"""
from __future__ import annotations

import math
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS arb_paper_books (
 pair TEXT NOT NULL,
 venue TEXT NOT NULL,
 cash REAL NOT NULL,
 qty REAL NOT NULL,
 start_cash REAL NOT NULL,
 start_qty REAL NOT NULL,
 started_ms INTEGER NOT NULL,
 PRIMARY KEY (pair, venue)
);
CREATE TABLE IF NOT EXISTS arb_paper_trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ts INTEGER NOT NULL,
 pair TEXT NOT NULL,
 buy_venue TEXT NOT NULL,
 sell_venue TEXT NOT NULL,
 qty REAL NOT NULL,
 buy_price REAL NOT NULL,
 sell_price REAL NOT NULL,
 fees REAL NOT NULL,
 net_usd REAL NOT NULL,
 net_pct REAL NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _seed(conn: sqlite3.Connection, pair: str, venues: list[str], mid: float,
          inventory_usd: float, now: int) -> None:
    """Cash and coin worth `inventory_usd` on every venue: a buffer of a few trades,
    because each arbitrage drains cash on one venue and coin on the other."""
    for v in venues:
        conn.execute(
            "INSERT OR IGNORE INTO arb_paper_books(pair,venue,cash,qty,start_cash,start_qty,"
            "started_ms) VALUES(?,?,?,?,?,?,?)",
            (pair, v, inventory_usd, inventory_usd / mid, inventory_usd, inventory_usd / mid, now))


def tick(conn: sqlite3.Connection, cfg: dict, edges: list[dict], now: int | None = None,
         allow_entries: bool = True) -> dict:
    """Take at most one arbitrage per pair from this scan's edges, if any clears the bar."""
    acfg = cfg.get("arbitrage") or {}
    if not acfg.get("enabled", False):
        return {"enabled": False, "trades": 0}
    now = int(now if now is not None else time.time() * 1000)
    ensure_schema(conn)
    size_usd = float(acfg.get("size_usd", 20))
    inventory_usd = size_usd * float(acfg.get("inventory_multiple", 3))
    min_edge = float(acfg.get("min_net_edge_pct", 0.10)) / 100
    slippage = float(acfg.get("slippage_pct", 0.05)) / 100
    vcfgs = acfg.get("venues") or {}
    mins = (cfg.get("backtest") or {}).get("min_order") or {}
    trades = 0
    by_pair: dict[str, list[dict]] = {}
    for e in edges:
        by_pair.setdefault(e["pair"], []).append(e)
    for pair, cands in by_pair.items():
        venues = sorted({c["buy_venue"] for c in cands} | {c["sell_venue"] for c in cands})
        mid = sum((c["buy_price"] + c["sell_price"]) / 2 for c in cands) / len(cands)
        if not (math.isfinite(mid) and mid > 0):
            continue
        _seed(conn, pair, venues, mid, inventory_usd, now)
        if not allow_entries:
            continue
        best = max(cands, key=lambda c: c["net_pct"])
        if best["net_pct"] < min_edge or best["net_usd"] <= 0:
            continue
        qty = best["qty"]
        om = mins.get(pair, {})
        if qty * mid < float(om.get("min_cost_usd", 5)) or qty < float(om.get("min_qty", 0)):
            continue
        buy = conn.execute("SELECT cash, qty FROM arb_paper_books WHERE pair=? AND venue=?",
                           (pair, best["buy_venue"])).fetchone()
        sell = conn.execute("SELECT cash, qty FROM arb_paper_books WHERE pair=? AND venue=?",
                            (pair, best["sell_venue"])).fetchone()
        buy_px = best["buy_price"] * (1 + slippage)
        sell_px = best["sell_price"] * (1 - slippage)
        buy_fee = float(vcfgs[best["buy_venue"]]["taker_fee_pct"]) / 100
        sell_fee = float(vcfgs[best["sell_venue"]]["taker_fee_pct"]) / 100
        cost = qty * buy_px * (1 + buy_fee)
        proceeds = qty * sell_px * (1 - sell_fee)
        if buy is None or sell is None or buy["cash"] < cost or sell["qty"] < qty:
            continue  # inventory exhausted on one side: the real constraint of this trade
        fees = qty * buy_px * buy_fee + qty * sell_px * sell_fee
        net = proceeds - cost
        conn.execute("UPDATE arb_paper_books SET cash=cash-?, qty=qty+? WHERE pair=? AND venue=?",
                     (cost, qty, pair, best["buy_venue"]))
        conn.execute("UPDATE arb_paper_books SET cash=cash+?, qty=qty-? WHERE pair=? AND venue=?",
                     (proceeds, qty, pair, best["sell_venue"]))
        conn.execute(
            "INSERT INTO arb_paper_trades(ts,pair,buy_venue,sell_venue,qty,buy_price,sell_price,"
            "fees,net_usd,net_pct) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now, pair, best["buy_venue"], best["sell_venue"], qty, buy_px, sell_px, fees, net,
             net / cost))
        trades += 1
    conn.commit()
    return {"enabled": True, "trades": trades}


def equity(conn: sqlite3.Connection, marks: dict[str, float]) -> dict:
    """Current value of all paper inventory versus simply having held it.

    `marks` maps pair -> current mid price. The arbitrage result is the
    difference: coin bought cheap and sold dear leaves more total value than
    the untouched starting inventory marked at the same price.
    """
    rows = conn.execute("SELECT * FROM arb_paper_books").fetchall()
    total, hold = 0.0, 0.0
    for r in rows:
        px = marks.get(r["pair"])
        if px is None:
            continue
        total += r["cash"] + r["qty"] * px
        hold += r["start_cash"] + r["start_qty"] * px
    return {"equity": total, "hold_value": hold, "arbitrage_pnl": total - hold}
