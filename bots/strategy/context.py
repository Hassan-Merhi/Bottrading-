"""Information beyond a bot's own candles: daily trends and market mood.

Every lookup is causal. A daily candle or a Fear & Greed reading only counts
once its whole day has ended, so a 1h candle closing at 15:00 sees
yesterday's daily close and yesterday's mood, never today's.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bots.data.fetch import TIMEFRAME_MS
from bots.data.loader import Series, resample, series_from_df

DAY_MS = 86_400_000
MAX_MOOD_AGE_MS = 3 * DAY_MS  # an older mood reading is treated as missing


@dataclass
class MarketContext:
    daily: dict[str, Series] = field(default_factory=dict)          # pair -> daily candles
    fear_greed: tuple[np.ndarray, np.ndarray] | None = None          # (day start ms, value)


def daily_from_series(s: Series) -> Series:
    df = pd.DataFrame({"ts": s.ts, "open": s.open, "high": s.high, "low": s.low,
                       "close": s.close, "volume": s.volume})
    return series_from_df(s.pair, "1d", resample(df, s.timeframe, "1d"))


def build_context(market: dict[tuple[str, str], Series],
                  fear_greed: tuple[np.ndarray, np.ndarray] | None) -> MarketContext:
    """Daily candles per pair from the coarsest stored timeframe, plus the mood history."""
    daily = {}
    for pair in sorted({p for p, _ in market}):
        tfs = sorted((tf for p, tf in market if p == pair), key=lambda tf: TIMEFRAME_MS[tf])
        daily[pair] = daily_from_series(market[(pair, tfs[-1])])
    return MarketContext(daily, fear_greed)


def _close_times(s: Series) -> np.ndarray:
    return s.ts + TIMEFRAME_MS[s.timeframe]


def trend_mask(s: Series, ref: Series | None, period: int) -> np.ndarray:
    """True where the reference's last completed daily close is above its moving average."""
    if ref is None or len(ref) < period:
        return np.zeros(len(s), bool)
    closes = pd.Series(ref.close)
    above = (closes > closes.rolling(period).mean()).to_numpy()
    known_from = ref.ts + DAY_MS
    idx = np.searchsorted(known_from, _close_times(s), side="right") - 1
    out = np.zeros(len(s), bool)
    ok = idx >= 0
    out[ok] = above[idx[ok]]
    return out


def mood_values(s: Series, fear_greed: tuple[np.ndarray, np.ndarray] | None) -> np.ndarray:
    """The latest Fear & Greed value known at each candle close; NaN if missing or stale."""
    out = np.full(len(s), np.nan)
    if fear_greed is None:
        return out
    days, values = fear_greed
    known_from = days + DAY_MS
    ct = _close_times(s)
    idx = np.searchsorted(known_from, ct, side="right") - 1
    ok = idx >= 0
    fresh = np.zeros(len(s), bool)
    fresh[ok] = ct[ok] - known_from[idx[ok]] <= MAX_MOOD_AGE_MS
    out[fresh] = values[idx[fresh]]
    return out
