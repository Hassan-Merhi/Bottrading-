"""Independent refresh and freshness checks for all whitelisted market pairs.

Market history must stay current even when no strategy has qualified for paper
trading.  Only public OHLC endpoints are used; no order APIs are accessible here.
"""
from __future__ import annotations

import time
from typing import Any

from bots.data.fetch import TIMEFRAME_MS, update_recent
from bots.data.store import candle_range, get_meta, set_meta

LAST_ATTEMPT = "market_refresh:last_attempt_ms"
LAST_SUCCESS = "market_refresh:last_success_ms"
LAST_ERRORS = "market_refresh:last_errors"


def market_health(conn, cfg: dict, clock_ms: int | None = None) -> dict[str, Any]:
    """One freshness result per configured pair, using the last CLOSED stored candle."""
    now = int(clock_ms if clock_ms is not None else time.time() * 1000)
    data = cfg["data"]
    tf = data["base_timeframe"]
    step = TIMEFRAME_MS[tf]
    max_age = float(cfg["worker"].get("max_candle_age_multiple", 3)) * step
    pairs: dict[str, dict] = {}
    for pair in data["pairs"]:
        found = candle_range(conn, pair, tf)
        last = found[1] if found else None
        age = now - (last + step) if last is not None else None
        # Future-dated records and absent candles are never healthy.
        fresh = age is not None and -step <= age <= max_age
        pairs[pair] = {
            "status": "fresh" if fresh else ("missing" if last is None else "stale"),
            "last_candle_ms": last,
            "age_minutes": round(max(age, 0) / 60_000, 1) if age is not None else None,
            "count": found[2] if found else 0,
        }
    unhealthy = [pair for pair, detail in pairs.items() if detail["status"] != "fresh"]
    return {
        "healthy": not unhealthy,
        "fresh_pairs": len(pairs) - len(unhealthy),
        "total_pairs": len(pairs),
        "unhealthy_pairs": unhealthy,
        "pairs": pairs,
        "last_attempt_ms": _int_or_none(get_meta(conn, LAST_ATTEMPT)),
        "last_success_ms": _int_or_none(get_meta(conn, LAST_SUCCESS)),
        "last_errors": get_meta(conn, LAST_ERRORS) or "",
    }


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def refresh_market_pairs(conn, exchange, cfg: dict, clock_ms: int | None = None) -> dict:
    """Refresh every pair independently; report failures and keep successful updates.

    Return a detailed status, without hiding an API failure just because previously
    saved candles happen to remain fresh.  The caller should exit nonzero if the
    resulting status is unhealthy.
    """
    now = int(clock_ms if clock_ms is not None else time.time() * 1000)
    set_meta(conn, LAST_ATTEMPT, str(now))
    errors: list[str] = []
    counts: dict[str, int] = {}
    tf = cfg["data"]["base_timeframe"]
    for pair in cfg["data"]["pairs"]:
        try:
            n = update_recent(conn, exchange, pair, tf)
            if n < 1:
                raise ValueError("exchange returned no closed candles")
            counts[pair] = n
        except Exception as exc:  # one failed pair must not prevent the others refreshing
            errors.append(f"{pair}: {type(exc).__name__}: {exc}")
            counts[pair] = 0
    health = market_health(conn, cfg, now)
    errors += [f"{pair}: {health['pairs'][pair]['status']}"
               for pair in health["unhealthy_pairs"] if not any(
                   line.startswith(f"{pair}:") for line in errors)]
    message = "; ".join(errors)
    set_meta(conn, LAST_ERRORS, message[:2000])
    if not errors:
        set_meta(conn, LAST_SUCCESS, str(now))
    health = market_health(conn, cfg, now)
    health["healthy"] = not errors
    health["updated_candles"] = counts
    health["errors"] = errors
    return health
