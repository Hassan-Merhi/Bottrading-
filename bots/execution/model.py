"""Shared, conservative execution assumptions for backtest and paper orders.

OHLC bars do not reveal the sequence of trades within a bar. In particular, a
two-minute resting order CANNOT be assumed filled merely because a 15-minute
candle later reached its price. The historical engine therefore accepts such
a fill only when the next bar opens through the limit (marketable at that
instant), or when a supplied bar is no longer than the order's actual timeout.
This is a deliberately conservative approximation, not Level 2 order replay.
"""
from __future__ import annotations

import math


def market_fill_price(price: float, side: str, slippage: float,
                      spread_bps: float = 0.0) -> float:
    """Approximate executable ask for a buy / bid for a sell, not the mid."""
    if not (math.isfinite(price) and price > 0):
        raise ValueError("price must be positive and finite")
    if side not in ("buy", "sell") or slippage < 0 or spread_bps < 0:
        raise ValueError("invalid execution side or costs")
    half_spread = spread_bps / 20_000.0
    return price * (1 + half_spread + slippage) if side == "buy" else (
        price * (1 - half_spread - slippage))


def limit_fill_kind(open_price: float, low_price: float, limit_price: float,
                    bar_duration_ms: int, timeout_ms: int,
                    trade_through_bps: float = 0.0) -> str | None:
    """Return 'taker', 'maker' or None, without inventing an intrabar timeline.

    Opening through an already-resting buy limit is charged as taker for
    conservatism; execution is bounded to the limit price. For an intrabar
    maker fill, a full bar must be covered by the order's remaining lifetime,
    and the low must trade *strictly through* a safety margin below the bid.
    """
    if min(open_price, low_price, limit_price) <= 0 or timeout_ms < 0 or (
            bar_duration_ms <= 0 or trade_through_bps < 0):
        raise ValueError("invalid limit execution inputs")
    if open_price <= limit_price:
        return "taker"
    if bar_duration_ms <= timeout_ms and low_price < limit_price * (
            1 - trade_through_bps / 10_000):
        return "maker"
    return None
