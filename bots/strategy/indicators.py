"""Causal indicators: the value at index i uses only candles 0..i."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(x: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(x).ewm(span=period, adjust=False).mean().to_numpy()


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI. The first `period` values are NaN."""
    delta = np.diff(close, prepend=np.nan)
    gain = pd.Series(np.where(delta > 0, delta, 0.0))
    loss = pd.Series(np.where(delta < 0, -delta, 0.0))
    gain[0] = loss[0] = np.nan  # no change is known for the first candle
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out[(avg_loss == 0) & avg_gain.notna()] = 100.0
    return out.to_numpy()


def macd(close: np.ndarray, fast: int, slow: int, signal: int = 9) -> tuple[np.ndarray, np.ndarray]:
    line = ema(close, fast) - ema(close, slow)
    return line, ema(line, signal)


def prior_rolling_max(x: np.ndarray, window: int) -> np.ndarray:
    """Max of the `window` candles before i (excluding i)."""
    return pd.Series(x).rolling(window).max().shift(1).to_numpy()


def prior_rolling_min(x: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(x).rolling(window).min().shift(1).to_numpy()


def crosses_above(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    prev = np.roll(a - b, 1)
    cur = a - b
    out = (prev <= 0) & (cur > 0)
    out[0] = False
    return out & ~np.isnan(prev) & ~np.isnan(cur)


def crosses_below(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return crosses_above(b, a)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's Average True Range. The first `period` values are NaN.

    True range at i uses candle i and the close of i - 1, so the value at i is
    known at the close of candle i and never looks ahead.
    """
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    tr[0] = high[0] - low[0]
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().to_numpy()
