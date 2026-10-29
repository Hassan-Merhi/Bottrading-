"""Phase 8: independent REAL live evidence and read-only capital scale reviews.

No method in this module places exchange orders, modifies allocation, or
turns on LIVE. Backtests and PAPER results never count as real-money P&L.
Scaling remains deliberately disabled until new, verified forward LIVE data
and fresh owner approval exist. Outcomes are suggestions, NOT permissions.
"""
from __future__ import annotations

import math
import sqlite3

from bots.live.pilot import certified_pilot
from bots.paper.forward import net_hold_return
from bots.risk.ledger import DAY_MS, Ledger

DEFAULT_CEILING_DAYS = 60
HARD_MAX_SCALE_CAP_USD = 100.0


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _fresh_close(conn: sqlite3.Connection, pair: str, now: int, cfg: dict):
    tf = cfg["data"]["base_timeframe"]
    row = conn.execute("SELECT ts, close FROM candles WHERE pair=? AND timeframe=? "
                       "ORDER BY ts DESC LIMIT 1", (pair, tf)).fetchone()
    if not row:
        return None
    interval = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}.get(tf, 900_000)
    price = row["close"]
    if (not _finite(price) or price <= 0 or row["ts"] > now
            or now - row["ts"] > interval * cfg["worker"]["max_candle_age_multiple"]):
        return None
    return float(price)


def live_evidence(conn: sqlite3.Connection, account, cfg: dict, now: int) -> dict:
    """Durable observations/fills for ONE real-money account; no paper credits."""
    marks = conn.execute(
        "SELECT day, ts, equity FROM equity_history "
        "WHERE account_id=? AND ts>=? AND ts<=? ORDER BY day",
        (account["account_id"], account["started_at"], now)).fetchall()
    filled = conn.execute(
        "SELECT side, pnl, fee, ts FROM fills WHERE account_id=? AND mode='live' "
        "AND ts>=? AND ts<=? ORDER BY ts, id",
        (account["account_id"], account["started_at"], now)).fetchall()
    buys = sum(f["side"] == "buy" for f in filled)
    sells = [f for f in filled if f["side"] == "sell"]
    wins = sum(max(float(f["pnl"] or 0), 0.0) for f in sells)
    losses = sum(max(-float(f["pnl"] or 0), 0.0) for f in sells)
    pf = min(wins / losses, 100.0) if losses > 0 else (100.0 if wins > 0 else 0.0)
    # Closed round trips cannot exceed either recorded completed ledger cycles,
    # sells or preceding buys. This is evidence, not a fabricated trades count.
    completed = min(buys, len(sells), int(account["trades"]))
    last_mark = marks[-1]["ts"] if marks else None
    days = [int(m["day"]) for m in marks]
    max_gap = max((b - a for a, b in zip(days, days[1:])), default=0)
    start = min(marks[0]["ts"], account["started_at"]) if marks else None
    span = ((now - start) / DAY_MS if start is not None else 0.0)
    # Validity of the data matters more than a profitable-looking last-equity.
    recent_mark = last_mark is not None and 0 <= now - last_mark <= DAY_MS
    bot = conn.execute("SELECT genome_hash, genome_json FROM deployed_bots WHERE bot_id=?",
                       (account["bot_id"],)).fetchone()
    import json
    pair = json.loads(bot["genome_json"])["pair"] if bot else None
    price = _fresh_close(conn, pair, now, cfg) if pair else None
    verified = (bool(bot) and
                certified_pilot(conn, cfg, account["bot_id"], bot["genome_hash"],
                                account["epoch"]))
    equity = Ledger.equity(account, price) if price is not None else None
    ret = (equity / account["allocation"] - 1
           if equity is not None and account["allocation"] > 0 else None)
    hold = net_hold_return(account["hold_start_price"], price, cfg)
    fees = sum(float(f["fee"]) for f in filled)
    return {
        "bot_id": account["bot_id"], "account_id": account["account_id"],
        "paper_verified_pilot_certificate": verified,
        "real_days": max(0.0, (now - account["started_at"]) / DAY_MS),
        "observed_span_days": span,
        "observed_live_days": len(days),
        "max_unobserved_days": max_gap,
        "last_mark_ms": last_mark, "recent_mark": recent_mark,
        "fresh_market_price": price is not None, "closed_roundtrips": completed,
        "realized_wins_usd": wins, "realized_losses_usd": losses,
        "realized_net_usd": wins - losses,
        "fees_usd": fees, "profit_factor": pf,
        "net_return": ret, "net_hold_return": hold,
        "max_drawdown": float(account["max_drawdown"]),
        "allocated_usd": account["allocation"],
    }


def scale_review(conn: sqlite3.Connection, cfg: dict, now: int) -> dict:
    """Review criteria independent of configured approval flags.

    A positive verdict means *eligible for a NEW owner discussion*, not
    authority for increasing the amount or modifying an exchange position.
    """
    s = cfg.get("scaling") or {}
    active = conn.execute(
        "SELECT * FROM accounts WHERE mode='live' AND active=1 ORDER BY account_id"
    ).fetchall()
    blocks = []
    if s.get("enabled") is not True:
        blocks.append("capital scaling disabled")
    if s.get("owner_signoff") is not True:
        blocks.append("separate capital-scaling owner approval missing")
    if not active:
        blocks.append("no completed and active real-money pilot")
    if len(active) != 1:
        blocks.append("scaling requires one independent active pilot bot")
    rows = []
    for account in active:
        ev = live_evidence(conn, account, cfg, now)
        why = []
        if not ev["paper_verified_pilot_certificate"]:
            why.append("no valid historical/forward-paper certification")
        if min(ev["real_days"], ev["observed_span_days"]) < s.get("min_live_days", DEFAULT_CEILING_DAYS):
            why.append("real-money pilot has insufficient elapsed evidence")
        if ev["observed_live_days"] < s.get("min_observed_days", 45):
            why.append("too few observed real-money market days")
        if ev["max_unobserved_days"] > s.get("max_unobserved_days", 3):
            why.append("extended live equity monitoring outage")
        if not ev["recent_mark"] or not ev["fresh_market_price"]:
            why.append("no fresh live valuation/market quote")
        if ev["closed_roundtrips"] < s.get("min_live_trades", 30):
            why.append("insufficient settled real-money round trips")
        if ev["profit_factor"] < s.get("min_profit_factor", 1.2):
            why.append("insufficient realized live profit factor")
        if ev["net_return"] is None or ev["net_return"] <= s.get("min_net_return_pct", 5) / 100:
            why.append("insufficient after-fee live net return")
        if ev["max_drawdown"] >= s.get("max_drawdown_pct", 5) / 100:
            why.append("live drawdown exceeds scale-review limit")
        if s.get("must_beat_hold", True) and (
                ev["net_hold_return"] is None or ev["net_return"] is None or
                ev["net_return"] <= ev["net_hold_return"]):
            why.append("live strategy failed net buy-and-hold")
        ev["review_blockers"] = why
        rows.append(ev)
    max_usd = s.get("max_total_capital_usd", 0)
    if not _finite(max_usd) or max_usd <= 0 or max_usd > HARD_MAX_SCALE_CAP_USD:
        blocks.append("scaling capital ceiling invalid or zero")
    if (cfg.get("pilot", {}).get("enabled") is not True or
            cfg.get("pilot", {}).get("owner_signoff") is not True or
            cfg.get("pilot", {}).get("exchange_signoff") is not True):
        blocks.append("original approved pilot controls missing")
    if any(row["review_blockers"] for row in rows):
        blocks.append("real-money performance qualification not satisfied")
    return {
        "enabled": s.get("enabled") is True,
        "status": "review_eligible" if not blocks else "blocked",
        "safe_to_auto_scale": False,
        "would_submit_orders": False,
        "live_accounts": len(active),
        "approved_scale_cap_usd": max_usd if not blocks else 0,
        "review_blockers": blocks,
        "live_evidence": rows,
        "note": ("Even a passed performance review never increases allocations; "
                 "separate manual funding, policy and exchange checks required. "
                 "Past REAL results do not guarantee profits."),
    }
