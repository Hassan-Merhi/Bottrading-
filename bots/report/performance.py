"""Read-only period and trade analytics for isolated simulated research bots.

Always derive data from persisted marks and fills; never replay old signals.
For shorter windows, period gain is the difference between the earliest and
latest *actually observed* balance inside the window (not a hypothetical price
at midnight). Original all-time balance remains visible separately.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from urllib.parse import urlencode

from bots.paper.forward_history import forward_history
from bots.paper.league import research_genomes

DAY_MS = 86_400_000
PERIODS = ("today", "7d", "30d", "all")
SIDES = ("all", "buy", "sell")
RESULTS = ("all", "profit", "loss")
# Almost three years of real hourly observations. Display a clear cap warning
# if more history exists; never label a clipped history as complete.
MAX_MARKS = 25000
MAX_FILLS = 25000


def validate(period: str, side: str = "all", result: str = "all") -> None:
    if period not in PERIODS or side not in SIDES or result not in RESULTS:
        raise ValueError("Invalid period or trading filter")


def start_of_period(now: int, period: str) -> int | None:
    if period == "all":
        return None
    if period == "today":
        return now // DAY_MS * DAY_MS
    return now - (7 if period == "7d" else 30) * DAY_MS


def build_link(path: str, **filters: str) -> str:
    return path + "?" + urlencode({k: v for k, v in filters.items() if v is not None})


def report(conn, cfg: dict, now: int, name: str = "all", period: str = "7d",
           side: str = "all", result: str = "all", search: str = "") -> dict:
    validate(period, side, result)
    # Never load or report live fills through an experimental view.
    raw = forward_history(conn, cfg, name=name, limit=MAX_MARKS, fills_limit=MAX_FILLS)
    lower = start_of_period(now, period)
    marks = [x for x in raw["observations"] if x["observed_ms"] is not None
             and x["quote_source"] == "kraken_ticker"
             and int(x["observed_ms"]) <= now
             and (lower is None or int(x["observed_ms"]) >= lower)]
    # A period change is provable only when there are two distinct,
    # timestamp-verified observed marks. The graph cannot fabricate baseline.
    start = marks[0] if len(marks) > 1 else None
    finish = marks[-1] if len(marks) > 1 else None
    change = round(float(finish["equity"]) - float(start["equity"]), 10) if start else None
    hold_change = (round(float(finish["benchmark_equity"]) -
                         float(start["benchmark_equity"]), 10) if start else None)
    pairs = {a["name"]: a["pair"] for a in
             conn.execute("SELECT name,pair FROM lab_accounts").fetchall()
             } if _has_lab_accounts(conn) else {}
    # Filter all fills before truncating so counters are not distorted.
    # SQL query intentionally lives in forward_history; this is a bounded UI read.
    fills_in_window = [dict(f, pair=pairs.get(f["name"], "—")) for f in raw["fills"]
                       if int(f["ts"]) <= now and (lower is None or int(f["ts"]) >= lower)]
    buy_fees = sum(float(f["fee"]) for f in fills_in_window if f["side"] == "buy")
    sell_fees = sum(float(f["fee"]) for f in fills_in_window if f["side"] == "sell")
    settled = [f for f in fills_in_window if f["side"] == "sell" and f["pnl"] is not None]
    realized = sum(float(f["pnl"]) for f in settled)
    wins = sum(float(f["pnl"]) > 0 for f in settled)
    filtered = fills_in_window
    if side != "all":
        filtered = [f for f in filtered if f["side"] == side]
    if result != "all":
        filtered = [f for f in filtered if f["pnl"] is not None and
                    ((float(f["pnl"]) > 0) if result == "profit" else
                     (float(f["pnl"]) < 0))]
    query = search.strip().casefold()[:120]
    if query:
        filtered = [f for f in filtered if query in
                    (" ".join([f["name"], f["pair"], f["side"], f["reason"]]).casefold())]
    return {
        "mode": "PAPER", "simulation": True, "real_money": False,
        "name": name, "period": period, "side": side, "result": result,
        "search": search.strip()[:120], "bots": raw["bots"],
        "marks": marks, "fills": filtered, "fills_in_period": fills_in_window,
        "all_time": raw["stats"],
        "period_stats": {
            "change": change, "benchmark_change": hold_change,
            "first_ms": start["observed_ms"] if start else None,
            "last_ms": finish["observed_ms"] if finish else None,
            "verified_marks": len(marks),
            "completed_trades": len(settled),
            "wins": wins,
            "win_rate_pct": 100 * wins / len(settled) if settled else None,
            "realized_pnl": realized,
            "buy_fees": buy_fees, "sell_fees": sell_fees,
            "total_fees": buy_fees + sell_fees,
            "buy_orders": sum(f["side"] == "buy" for f in fills_in_window),
            "sell_orders": sum(f["side"] == "sell" for f in fills_in_window),
        },
        "history_cap": len(raw["observations"]) >= MAX_MARKS or len(raw["fills"]) >= MAX_FILLS,
    }


def _has_lab_accounts(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='lab_accounts' AND type='table'").fetchone())


def export_csv(data: dict, kind: str = "trades") -> str:
    if kind not in ("trades", "equity"):
        raise ValueError("Invalid export type")
    out = io.StringIO()
    writer = csv.writer(out)
    if kind == "trades":
        cols = ("id", "name", "pair", "ts", "side", "qty", "price", "fee", "pnl", "reason")
        rows = data["fills"]
    else:
        cols = ("candle_ts", "observed_ms", "equity", "net_pnl",
                "benchmark_equity", "benchmark_net_pnl", "quote_source")
        rows = data["marks"]
    writer.writerow(cols)
    for row in rows:
        # Spreadsheet formula injection must not be possible through imported
        # labels or reasons. Numeric columns are kept numeric for analysis.
        def safe(value):
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                return "'" + value
            return value
        writer.writerow([safe(row.get(key)) for key in cols])
    return out.getvalue()


def compact_series(marks: list[dict], cap: int = 320) -> list[dict]:
    """Bound the SVG, retaining actual marks only—no averaging or interpolating."""
    if len(marks) <= cap:
        return marks
    indices = sorted(set(round(i * (len(marks) - 1) / (cap - 1))
                         for i in range(cap)))
    return [marks[i] for i in indices]


def verified_bot_names() -> tuple[str, ...]:
    return tuple(research_genomes())


def bot_slug(name: str) -> str:
    return name.casefold().replace(" ", "-")


def resolve_bot_slug(slug: str) -> str:
    for name in verified_bot_names():
        if bot_slug(name) == slug:
            return name
    raise ValueError("Unknown research bot")
