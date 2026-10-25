"""Synthetic candles for tests and offline pipeline checks.

Never use these to judge a strategy: they are random walks, not markets.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bots.data.fetch import TIMEFRAME_MS


def random_walk_candles(n: int, timeframe: str = "15m", start_price: float = 30_000.0,
                        drift: float = 0.0, vol: float = 0.004, seed: int = 0,
                        start_ms: int = 1_640_995_200_000) -> pd.DataFrame:
    """Geometric random walk with regime switches, as OHLCV candles."""
    rng = np.random.default_rng(seed)
    regime_drift = np.repeat(rng.normal(drift, vol / 40, size=n // 500 + 1), 500)[:n]
    rets = rng.normal(regime_drift, vol, size=n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.normal(0, vol / 2, size=(2, n)))
    high = np.maximum(open_, close) * (1 + wick[0])
    low = np.minimum(open_, close) * (1 - wick[1])
    ts = start_ms + np.arange(n, dtype="int64") * TIMEFRAME_MS[timeframe]
    return pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(1, 10, size=n)})
