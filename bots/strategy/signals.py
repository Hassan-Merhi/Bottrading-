"""Entry and exit signals for the four entry types.

A signal at index i is computed from candles 0..i, i.e. at the close of
candle i. The backtester acts on it at the open of candle i + 1.
"""
from __future__ import annotations

from collections import OrderedDict

import numpy as np
import pandas as pd

from bots.data.loader import Series
from bots.strategy import indicators as ind
from bots.strategy.context import MarketContext, mood_values, trend_mask
from bots.strategy.genome import NUMERIC_GENES, Genome

RSI_PERIOD = 14
MACD_SIGNAL = 9
SQUEEZE_QUANTILE = 0.2  # band width in its lowest 20% of the lookback counts as a squeeze


def compute_signals(s: Series, g: Genome, ctx: MarketContext | None = None
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Boolean (entry, exit) arrays, one value per candle.

    Trend and mood filters only gate entries; exits never wait for them. A
    filter whose data is missing blocks entries rather than guessing.
    """
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
    elif g.entry_type == "trend_pullback":
        # Research-only hypothesis until it passes all unchanged historical gates.
        # All indicators use completed candles, with no test-period leakage.
        fast, slow = ind.ema(c, g.fast_period), ind.ema(c, g.slow_period)
        r = ind.rsi(c, RSI_PERIOD)
        entry = ((fast > slow) & (c > fast) &
                 ind.crosses_above(r, np.full_like(r, g.rsi_low)))
        exit_ = (c < slow) | (r >= g.rsi_high)
        warmup = max(g.slow_period, RSI_PERIOD * 3)
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
    elif g.entry_type == "volume_spike":
        avg_vol = pd.Series(s.volume).rolling(g.breakout_window).mean().shift(1).to_numpy()
        hi = ind.prior_rolling_max(s.high, g.fast_period)
        with np.errstate(invalid="ignore"):
            entry = (s.volume > g.vol_mult * avg_vol) & (c > s.open) & (c > hi)
        exit_ = c < ind.ema(c, g.fast_period)
        warmup = max(g.breakout_window, g.fast_period) + 1
    elif g.entry_type == "squeeze_breakout":
        close = pd.Series(c)
        mid = close.rolling(g.breakout_window).mean()
        sd = close.rolling(g.breakout_window).std()
        width = (4 * sd / mid)
        tight = width.rolling(g.slow_period).quantile(SQUEEZE_QUANTILE)
        squeezed = (width.shift(1) <= tight.shift(1)).to_numpy()
        entry = squeezed & (c > (mid + 2 * sd).to_numpy())
        exit_ = c < mid.to_numpy()
        warmup = g.breakout_window + g.slow_period + 1
    else:
        raise ValueError(f"unknown entry_type {g.entry_type!r}")
    entry = np.asarray(entry, dtype=bool).copy()
    exit_ = np.asarray(exit_, dtype=bool).copy()
    entry[:warmup] = False  # indicators are not settled yet
    if g.trend_filter != "none":
        ref_pair = g.pair if g.trend_filter == "own_daily" else "BTC/USD"
        ref = ctx.daily.get(ref_pair) if ctx else None
        entry &= trend_mask(s, ref, g.trend_period)
    if g.mood_filter_on:
        mood = mood_values(s, ctx.fear_greed if ctx else None)
        with np.errstate(invalid="ignore"):
            entry &= (mood >= g.mood_min) & (mood <= g.mood_max)
    return entry, exit_


def stop_fractions(s: Series, g: Genome, atr: np.ndarray | None = None) -> np.ndarray | None:
    """Per-candle stop distance as a fraction of price, or None for a fixed stop.

    In ATR mode the stop sits atr_mult x ATR(atr_period) below the entry, computed
    at the signal candle's close, and clipped into the fixed stop's allowed range
    so one trade can never risk more than the widest fixed stop. Candles where
    the ATR is not settled yet fall back to the genome's fixed stop.
    """
    if g.stop_mode != "atr":
        return None
    if atr is None:
        atr = ind.atr(s.high, s.low, s.close, g.atr_period)
    lo, hi, _ = NUMERIC_GENES["stop_loss_pct"]
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = g.atr_mult * atr / s.close
    frac = np.where(np.isfinite(frac), np.clip(frac, lo / 100, hi / 100), g.stop_loss_pct / 100)
    return frac


def stop_pct_now(s: Series, g: Genome) -> float:
    """The stop distance in percent for an entry signalled on the latest candle."""
    frac = stop_fractions(s, g)
    return g.stop_loss_pct if frac is None else float(frac[-1] * 100)


def signal_key(g: Genome) -> tuple:
    """Only the genes that change the signals."""
    base = (g.pair, g.timeframe, g.entry_type,
            g.trend_filter, g.trend_period if g.trend_filter != "none" else 0,
            g.mood_min, g.mood_max)
    if g.entry_type in ("ema_cross", "macd"):
        return base + (g.fast_period, g.slow_period)
    if g.entry_type == "trend_pullback":
        return base + (g.fast_period, g.slow_period, g.rsi_low, g.rsi_high)
    if g.entry_type == "rsi_reversion":
        return base + (g.rsi_low, g.rsi_high)
    if g.entry_type == "volume_spike":
        return base + (g.breakout_window, g.fast_period, g.vol_mult)
    if g.entry_type == "squeeze_breakout":
        return base + (g.breakout_window, g.slow_period)
    return base + (g.breakout_window,)


class SignalCache:
    """Cache signal positions (sorted index arrays) per distinct signal genes."""

    # Memory matters: a 3-year 15m series is ~105k candles, so one float64 ATR
    # array is ~0.8 MB and a busy signal's index array tens of KB. The caches are
    # small enough that an evolution run fits a 512 MB host next to the worker.
    MAX_ATR = 24

    def __init__(self, market: dict[tuple[str, str], Series], ctx: MarketContext | None = None,
                 max_size: int = 1_000):
        self.market = market
        self.ctx = ctx
        self.max_size = max_size
        self._cache: OrderedDict[tuple, tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self._atr: OrderedDict[tuple, np.ndarray] = OrderedDict()

    def get(self, g: Genome) -> tuple[Series, np.ndarray, np.ndarray]:
        s = self.market[(g.pair, g.timeframe)]
        key = signal_key(g)
        hit = self._cache.get(key)
        if hit is None:
            entry, exit_ = compute_signals(s, g, self.ctx)
            hit = (np.flatnonzero(entry).astype(np.int32), np.flatnonzero(exit_).astype(np.int32))
            self._cache[key] = hit
            if len(self._cache) > self.max_size:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return s, hit[0], hit[1]

    def stops(self, g: Genome) -> np.ndarray | None:
        """Per-candle stop fractions for ATR-mode genomes (None for fixed stops)."""
        if g.stop_mode != "atr":
            return None
        s = self.market[(g.pair, g.timeframe)]
        key = (g.pair, g.timeframe, g.atr_period)
        atr = self._atr.get(key)
        if atr is None:
            atr = ind.atr(s.high, s.low, s.close, g.atr_period).astype(np.float32)
            self._atr[key] = atr
            if len(self._atr) > self.MAX_ATR:
                self._atr.popitem(last=False)
        else:
            self._atr.move_to_end(key)
        return stop_fractions(s, g, atr)
