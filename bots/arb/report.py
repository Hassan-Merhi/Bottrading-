"""Read-only summaries of what the arbitrage scanner and paper trader have seen."""
from __future__ import annotations

import os
import sqlite3
import time

DAY_MS = 86_400_000


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def snapshot(conn: sqlite3.Connection, cfg: dict, now: int | None = None,
             env: dict | None = None) -> dict:
    acfg = cfg.get("arbitrage") or {}
    now = int(now if now is not None else time.time() * 1000)
    env = env if env is not None else dict(os.environ)
    base = {"enabled": bool(acfg.get("enabled", False)), "real_money": False, "simulation": True,
            "size_usd": float(acfg.get("size_usd", 20)),
            "min_net_edge_pct": float(acfg.get("min_net_edge_pct", 0.10)),
            "venues": {n: {"taker_fee_pct": v.get("taker_fee_pct"), "quote": v.get("quote", "USD"),
                           "enabled": v.get("enabled", True)}
                       for n, v in (acfg.get("venues") or {}).items()}}
    from bots.arb.live import snapshot as live_snapshot
    base["live"] = live_snapshot(conn, cfg, env, now, clients_ready=False)
    if not base["enabled"] or not _has(conn, "arb_observations"):
        return {**base, "observations": 0, "pairs": [], "paper": None, "funding": [],
                "scan_errors": []}
    total, first, last = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM arb_observations").fetchone()
    pairs = []
    for r in conn.execute(
            "SELECT pair, COUNT(*) n, AVG(gross_pct) avg_gross, MAX(gross_pct) max_gross, "
            "AVG(net_pct) avg_net, MAX(net_pct) max_net, "
            "SUM(CASE WHEN net_usd > 0 THEN 1 ELSE 0 END) positive "
            "FROM arb_observations GROUP BY pair ORDER BY max_net DESC"):
        pairs.append({"pair": r["pair"], "observations": r["n"],
                      "avg_gross_pct": 100 * r["avg_gross"], "max_gross_pct": 100 * r["max_gross"],
                      "avg_net_pct": 100 * r["avg_net"], "max_net_pct": 100 * r["max_net"],
                      "positive_share_pct": 100 * r["positive"] / r["n"] if r["n"] else 0.0})
    best = conn.execute("SELECT * FROM arb_observations ORDER BY net_pct DESC LIMIT 1").fetchone()
    recent = [dict(r) for r in conn.execute(
        "SELECT ts, pair, buy_venue, sell_venue, gross_pct, net_pct, net_usd FROM arb_observations "
        "WHERE ts >= ? ORDER BY net_pct DESC LIMIT 10", (now - DAY_MS,))]
    paper = None
    if _has(conn, "arb_paper_trades"):
        n, pnl, fees = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_usd),0), COALESCE(SUM(fees),0) FROM arb_paper_trades"
        ).fetchone()
        marks: dict[str, float] = {}
        for r in conn.execute(
                "SELECT pair, buy_price, sell_price FROM arb_observations o WHERE id IN "
                "(SELECT MAX(id) FROM arb_observations GROUP BY pair)"):
            marks[r["pair"]] = (r["buy_price"] + r["sell_price"]) / 2
        from bots.arb.paper import equity
        eq = equity(conn, marks) if marks else {"equity": 0.0, "hold_value": 0.0,
                                                "arbitrage_pnl": 0.0}
        inventory = [dict(r) for r in conn.execute(
            "SELECT pair, venue, cash, qty, start_cash, start_qty FROM arb_paper_books "
            "ORDER BY pair, venue")]
        paper = {"trades": n, "realized_net_usd": pnl, "fees_usd": fees, **eq,
                 "inventory": inventory,
                 "last_trades": [dict(r) for r in conn.execute(
                     "SELECT * FROM arb_paper_trades ORDER BY id DESC LIMIT 10")]}
    funding = []
    if _has(conn, "arb_funding"):
        funding = [dict(r) for r in conn.execute(
            "SELECT * FROM arb_funding WHERE id IN (SELECT MAX(id) FROM arb_funding GROUP BY symbol) "
            "ORDER BY symbol")]
    errors = []
    if _has(conn, "arb_scan_log"):
        errors = [dict(r) for r in conn.execute(
            "SELECT * FROM arb_scan_log WHERE ok=0 AND ts >= ? ORDER BY ts DESC LIMIT 10",
            (now - DAY_MS,))]
    return {**base, "observations": total, "first_ms": first, "last_ms": last,
            "observed_days": (last - first) / DAY_MS if first and last else 0.0,
            "pairs": pairs, "best_ever": dict(best) if best else None,
            "best_last_24h": recent, "paper": paper, "funding": funding, "scan_errors": errors}


def text_summary(snap: dict) -> str:
    """Plain lines for the Telegram daily report."""
    if not snap.get("enabled"):
        return "Arbitrage scanner: off."
    lines = [f"Arbitrage scanner (fake money): {snap['observations']} observations over "
             f"{snap.get('observed_days', 0):.1f} days, ${snap['size_usd']:.0f} per leg."]
    if snap.get("pairs"):
        top = snap["pairs"][0]
        lines.append(f"Best pair {top['pair']}: avg gap {top['avg_gross_pct']:+.3f}%, "
                     f"avg net after fees {top['avg_net_pct']:+.3f}%, "
                     f"profitable {top['positive_share_pct']:.1f}% of the time.")
    p = snap.get("paper")
    if p:
        lines.append(f"Paper arbitrage: {p['trades']} trades, realized ${p['realized_net_usd']:+.2f}, "
                     f"versus holding ${p['arbitrage_pnl']:+.2f}.")
    live = snap.get("live") or {}
    if live.get("enabled"):
        lines.append(f"LIVE arbitrage: {'READY' if live.get('ready') else 'locked'}; "
                     f"{live.get('trades', 0)} real trades, net ${live.get('net_usd', 0):+.2f}"
                     + (f"; first blocker: {live['blockers'][0]}" if live.get("blockers") else "")
                     + (f"; HALTED: {live['halt']}" if live.get("halt") else ""))
    for f in snap.get("funding") or []:
        lines.append(f"Funding {f['symbol']}: {100 * f['rate']:+.4f}%/{f['interval_hours']:.0f}h, "
                     f"{100 * f['annual_gross_pct']:+.1f}%/yr gross, "
                     f"{100 * f['annual_net_pct']:+.1f}%/yr net of costs.")
    if not snap.get("pairs"):
        lines.append("No order books observed yet.")
    return "\n".join(lines)
