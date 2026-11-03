"""Observe live order books on several venues and record every cross-venue edge.

Read-only against the exchanges: only public order books and funding rates
are fetched. Observations are stored so the dashboard can show, over days,
how often an edge net of fees actually existed and how large it was.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time

from bots.arb.model import cross_edge, funding_carry, walk_book

SCHEMA = """
CREATE TABLE IF NOT EXISTS arb_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ts INTEGER NOT NULL,
 pair TEXT NOT NULL,
 buy_venue TEXT NOT NULL,
 sell_venue TEXT NOT NULL,
 qty REAL NOT NULL,
 size_usd REAL NOT NULL,
 buy_price REAL NOT NULL,
 sell_price REAL NOT NULL,
 gross_pct REAL NOT NULL,
 net_pct REAL NOT NULL,
 net_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS arb_obs_ts ON arb_observations(ts);
CREATE INDEX IF NOT EXISTS arb_obs_pair ON arb_observations(pair, ts);
CREATE TABLE IF NOT EXISTS arb_funding (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ts INTEGER NOT NULL,
 venue TEXT NOT NULL,
 symbol TEXT NOT NULL,
 rate REAL NOT NULL,
 interval_hours REAL NOT NULL,
 annual_gross_pct REAL NOT NULL,
 annual_net_pct REAL NOT NULL,
 breakeven_days REAL
);
CREATE TABLE IF NOT EXISTS arb_scan_log (
 ts INTEGER NOT NULL,
 venue TEXT NOT NULL,
 ok INTEGER NOT NULL,
 detail TEXT NOT NULL,
 PRIMARY KEY (ts, venue)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def venue_symbol(pair: str, venue_cfg: dict) -> str:
    """BTC/USD on a USDT venue becomes BTC/USDT. The quote mismatch is a real
    basis risk (USDT is not USD), which is why non-USD venues are off by default."""
    base, quote = pair.split("/")
    return f"{base}/{venue_cfg.get('quote', quote)}"


def make_venues(acfg: dict):
    """ccxt clients for every enabled venue; created once per process."""
    from bots.data.fetch import make_exchange

    venues = {}
    for name, vcfg in (acfg.get("venues") or {}).items():
        if vcfg.get("enabled", True):
            venues[name] = make_exchange(name)
    return venues


def fetch_books(venues: dict, pairs: list[str], acfg: dict, depth: int = 20) -> tuple[dict, dict]:
    """{(venue, pair): {"bids": [...], "asks": [...]}} plus {venue: error text}."""
    books: dict[tuple[str, str], dict] = {}
    errors: dict[str, str] = {}
    vcfgs = acfg.get("venues") or {}
    for name, api in venues.items():
        vcfg = vcfgs.get(name, {})
        try:
            for pair in pairs:
                book = api.fetch_order_book(venue_symbol(pair, vcfg), depth)
                bids, asks = book.get("bids") or [], book.get("asks") or []
                if not bids or not asks:
                    continue
                if float(asks[0][0]) < float(bids[0][0]):
                    continue  # crossed book: stale or bogus, never trade on it
                books[(name, pair)] = {"bids": bids, "asks": asks}
        except Exception as e:  # noqa: BLE001 - one venue down must not stop the scan
            errors[name] = f"{type(e).__name__}: {e}"[:200]
    return books, errors


def edges_from_books(books: dict, pairs: list[str], acfg: dict) -> list[dict]:
    """Every (buy venue, sell venue) direction for every pair, at the configured size."""
    vcfgs = acfg.get("venues") or {}
    size_usd = float(acfg.get("size_usd", 20))
    slippage = float(acfg.get("slippage_pct", 0.05)) / 100
    out = []
    venues = sorted({v for v, _ in books})
    for pair in pairs:
        for buy_v in venues:
            for sell_v in venues:
                if buy_v == sell_v:
                    continue
                b, s = books.get((buy_v, pair)), books.get((sell_v, pair))
                if b is None or s is None:
                    continue
                mid = (float(b["asks"][0][0]) + float(s["bids"][0][0])) / 2
                if not (math.isfinite(mid) and mid > 0):
                    continue
                qty = size_usd / mid
                buy_px = walk_book(b["asks"], qty)
                sell_px = walk_book(s["bids"], qty)
                if buy_px is None or sell_px is None:
                    continue  # not enough depth at this size: no edge to record
                e = cross_edge(buy_px, sell_px, qty,
                               float(vcfgs[buy_v]["taker_fee_pct"]) / 100,
                               float(vcfgs[sell_v]["taker_fee_pct"]) / 100, slippage)
                out.append({"pair": pair, "buy_venue": buy_v, "sell_venue": sell_v,
                            "qty": qty, "size_usd": size_usd, "buy_price": buy_px,
                            "sell_price": sell_px, "gross_pct": e.gross_pct,
                            "net_pct": e.net_pct, "net_usd": e.net_usd})
    return out


def fetch_funding(futures_api, acfg: dict) -> tuple[list[dict], str | None]:
    """Current funding rates for the configured perpetuals, as carry estimates."""
    fcfg = acfg.get("funding") or {}
    symbols = list(fcfg.get("symbols") or [])
    if not symbols or futures_api is None:
        return [], None
    try:
        rates = futures_api.fetch_funding_rates(symbols)
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"[:200]
    spot_fee = float(fcfg.get("spot_taker_fee_pct", 0.80)) / 100
    fut_fee = float(fcfg.get("futures_taker_fee_pct", 0.05)) / 100
    slippage = float(acfg.get("slippage_pct", 0.05)) / 100
    hold_days = float(fcfg.get("hold_days", 30))
    interval = float(fcfg.get("interval_hours", 8))
    out = []
    for sym in symbols:
        r = (rates or {}).get(sym) or {}
        rate = r.get("fundingRate")
        if rate is None or not math.isfinite(float(rate)):
            continue
        c = funding_carry(float(rate), interval, spot_fee, fut_fee, slippage, hold_days)
        out.append({"venue": fcfg.get("venue", "krakenfutures"), "symbol": sym, "rate": c.rate,
                    "interval_hours": interval, "annual_gross_pct": c.annual_gross_pct,
                    "annual_net_pct": c.annual_net_pct, "breakeven_days": c.breakeven_days})
    return out, None


def scan(conn: sqlite3.Connection, cfg: dict, venues: dict, now: int | None = None,
         futures_api=None) -> dict:
    """One pass: fetch books, compute edges, store observations and funding. Never raises."""
    acfg = cfg.get("arbitrage") or {}
    if not acfg.get("enabled", False):
        return {"enabled": False, "observations": 0}
    now = int(now if now is not None else time.time() * 1000)
    ensure_schema(conn)
    pairs = list(acfg.get("pairs") or cfg["data"]["pairs"])
    books, errors = fetch_books(venues, pairs, acfg)
    edges = edges_from_books(books, pairs, acfg)
    conn.executemany(
        "INSERT INTO arb_observations(ts,pair,buy_venue,sell_venue,qty,size_usd,buy_price,"
        "sell_price,gross_pct,net_pct,net_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(now, e["pair"], e["buy_venue"], e["sell_venue"], e["qty"], e["size_usd"],
          e["buy_price"], e["sell_price"], e["gross_pct"], e["net_pct"], e["net_usd"])
         for e in edges])
    funding, ferr = fetch_funding(futures_api, acfg) if (acfg.get("funding") or {}).get(
        "enabled") else ([], None)
    conn.executemany(
        "INSERT INTO arb_funding(ts,venue,symbol,rate,interval_hours,annual_gross_pct,"
        "annual_net_pct,breakeven_days) VALUES(?,?,?,?,?,?,?,?)",
        [(now, f["venue"], f["symbol"], f["rate"], f["interval_hours"], f["annual_gross_pct"],
          f["annual_net_pct"], f["breakeven_days"]) for f in funding])
    if ferr:
        errors["funding"] = ferr
    for name in venues:
        conn.execute("INSERT OR REPLACE INTO arb_scan_log(ts,venue,ok,detail) VALUES(?,?,?,?)",
                     (now, name, int(name not in errors), errors.get(name, "ok")))
    if ferr:
        conn.execute("INSERT OR REPLACE INTO arb_scan_log(ts,venue,ok,detail) VALUES(?,?,?,?)",
                     (now, "funding", 0, ferr))
    keep_days = float(acfg.get("keep_days", 90))
    conn.execute("DELETE FROM arb_observations WHERE ts < ?", (now - int(keep_days * 86_400_000),))
    conn.execute("DELETE FROM arb_funding WHERE ts < ?", (now - int(keep_days * 86_400_000),))
    conn.execute("DELETE FROM arb_scan_log WHERE ts < ?", (now - int(keep_days * 86_400_000),))
    conn.commit()
    return {"enabled": True, "ts": now, "observations": len(edges), "funding": len(funding),
            "errors": errors, "books": len(books), "edges": edges}


def describe_edges(edges: list[dict]) -> str:
    return json.dumps(edges, indent=1, default=str)
