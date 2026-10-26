"""Explain why a bot failed, in words the owner can read."""
from __future__ import annotations

import numpy as np

from bots.data.loader import Series

# Efficiency ratio below this = price wandered without going anywhere.
SIDEWAYS_EFFICIENCY = 0.15
TREND_MOVE = 0.05
BIG_MOVE = 0.20


def market_regime(s: Series, start: int, end: int) -> tuple[str, float]:
    """Label a stretch of candles as uptrend / downtrend / sideways, plus annualized volatility."""
    c = s.close[start:end]
    if len(c) < 3:
        return "unknown", 0.0
    step = max(int(s.ts[1] - s.ts[0]), 1)
    rets = np.diff(np.log(c))
    vol = float(rets.std() * np.sqrt(365.25 * 86_400_000 / step))
    # Judge the trend on daily closes: over thousands of intraday candles the
    # path length swamps any move and everything would look sideways.
    daily = c[:: max(1, 86_400_000 // step)]
    if len(daily) < 3:
        daily = c
    move = daily[-1] / daily[0] - 1
    path = np.abs(np.diff(daily)).sum()
    efficiency = abs(daily[-1] - daily[0]) / path if path > 0 else 0.0
    big_move = abs(move) >= BIG_MOVE  # a large net move is a trend however choppy the path
    if abs(move) < TREND_MOVE or (efficiency < SIDEWAYS_EFFICIENCY and not big_move):
        regime = "sideways"
    else:
        regime = "uptrend" if move > 0 else "downtrend"
    return regime, vol


def diagnose(window_metrics: list[dict], regime: str, min_trades: int) -> str:
    """Pick the clearest reason for poor performance, judged across all windows."""
    n_trades = sum(m["n_trades"] for m in window_metrics)
    rejected = sum(m["rejected_orders"] for m in window_metrics)
    stops = sum(m["stop_outs"] for m in window_metrics)
    fees = sum(m["fees_paid"] for m in window_metrics)
    gross = sum(m["gross_pnl"] for m in window_metrics)
    total = float(np.prod([1 + m["total_return"] for m in window_metrics]) - 1)
    hold = float(np.prod([1 + m["hold_return"] for m in window_metrics]) - 1)

    if n_trades == 0 and rejected > 0:
        return "orders below exchange minimum (position too small)"
    if n_trades < min_trades:
        return "too few trades to trust"
    if stops / n_trades > 0.6:
        return f"stopped out repeatedly in {regime} market"
    if gross > 0 and fees >= gross:
        return "overtrading: fees ate the profits"
    if total < 0 and regime == "downtrend":
        return "lost money in a downtrend"
    if total < 0:
        return f"lost money in {regime} market"
    if total < hold:
        return f"profitable but trailed simply holding ({regime} market)"
    return "outcompeted by stronger bots"
