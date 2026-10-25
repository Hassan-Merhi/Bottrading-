"""Entry and exit signals for the four entry types.

A signal at index i is computed from candles 0..i, i.e. at the close of
candle i. The backtester acts on it at the open of candle i + 1.
"""
from __future__ import annotations

from collections import OrderedDict

import numpy as np

from bots.data.loader import Series
from bots.strategy import indicators as ind
from bots.strategy.genome import Genome

RSI_PERIOD = 14
MACD_SIGNAL = 9


def compute_signals(s: Series, g: Genome) -> tuple[np.ndarray, np.ndarray]:
    """Boolean (entry, exit) arrays, one value per candle."""
    c = s.close
    if g.entry_type == "ema_cross":
        fast, slow = ind.ema(c, g.fast_period), ind.ema(c, g.slow_period)
        entry, exit_ = ind.crosses_above(fast, slow), ind.crosses_below(fast, slow)
        warmup = g.slow_period
    elif g.entry_type == "rsi_reversion":
        r = ind.rsi(c, RSI_PERIOD)
        entry = ind.crosses_above(r, np.full_like(r, g.rsi_low))
        exit_ = r > g.rsi_high
        warmup = RSI_PERIOD * 3
    elif g.entry_type == "breakout":
        hi = ind.prior_rolling_max(s.high, g.breakout_window)
        lo = ind.prior_rolling_min(s.low, max(2, g.breakout_window // 2))
        entry = c > hi
        exit_ = c < lo
        warmup = g.breakout_window
    elif g.entry_type == "macd":
        line, sig = ind.macd(c, g.fast_period, g.slow_period, MACD_SIGNAL)
        entry, exit_ = ind.crosses_above(line, sig), ind.crosses_below(line, sig)
        warmup = g.slow_period + MACD_SIGNAL
    else:
        raise ValueError(f"unknown entry_type {g.entry_type!r}")
    entry = np.asarray(entry, dtype=bool).copy()
    exit_ = np.asarray(exit_, dtype=bool).copy()
    entry[:warmup] = False  # indicators are not settled yet
    return entry, exit_


def signal_key(g: Genome) -> tuple:
    """Only the genes that change the signals."""
    base = (g.pair, g.timeframe, g.entry_type)
    if g.entry_type in ("ema_cross", "macd"):
        return base + (g.fast_period, g.slow_period)
    if g.entry_type == "rsi_reversion":
        return base + (g.rsi_low, g.rsi_high)
    return base + (g.breakout_window,)


class SignalCache:
    """Cache signal positions (sorted index arrays) per distinct signal genes."""

    def __init__(self, market: dict[tuple[str, str], Series], max_size: int = 20_000):
        self.market = market
        self.max_size = max_size
        self._cache: OrderedDict[tuple, tuple[np.ndarray, np.ndarray]] = OrderedDict()

    def get(self, g: Genome) -> tuple[Series, np.ndarray, np.ndarray]:
        s = self.market[(g.pair, g.timeframe)]
        key = signal_key(g)
        hit = self._cache.get(key)
        if hit is None:
            entry, exit_ = compute_signals(s, g)
            hit = (np.flatnonzero(entry), np.flatnonzero(exit_))
            self._cache[key] = hit
            if len(self._cache) > self.max_size:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return s, hit[0], hit[1]
