import copy

import numpy as np
import pandas as pd
import pytest

from bots.config import load_config
from bots.data.loader import Series, resample, series_from_df
from bots.data.synthetic import random_walk_candles
from bots.db import connect


def make_series(open_, high, low, close, timeframe="1h", start_ms=1_700_000_000_000 // 3_600_000 * 3_600_000,
                pair="BTC/USD", volume=None) -> Series:
    n = len(close)
    step = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe]
    df = pd.DataFrame({"ts": start_ms + np.arange(n, dtype="int64") * step, "open": open_,
                       "high": high, "low": low, "close": close,
                       "volume": np.ones(n) if volume is None else volume})
    return series_from_df(pair, timeframe, df)


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture(scope="session")
def synthetic_market():
    market = {}
    for k, pair in enumerate(["BTC/USD", "ETH/USD"]):
        base = random_walk_candles(4 * 24 * 200, "15m", start_price=[30_000, 2_000][k], seed=k)
        for tf in ["1h", "4h"]:
            market[(pair, tf)] = series_from_df(pair, tf, resample(base, "15m", tf))
    return market
