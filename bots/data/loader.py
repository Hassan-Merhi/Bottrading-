"""Turn stored candles into gap-free numpy series and time-based splits."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bots.data.fetch import TIMEFRAME_MS
from bots.data.store import load_candles


@dataclass
class Series:
    """One pair at one timeframe. Every candle is closed."""

    pair: str
    timeframe: str
    ts: np.ndarray       # int64 ms, candle open time
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    day: np.ndarray = field(init=False)  # UTC day number, for per-day trade limits

    def __post_init__(self) -> None:
        self.day = self.ts // 86_400_000

    def __len__(self) -> int:
        return len(self.ts)

    def index_of(self, ts_ms: int) -> int:
        """First candle index whose open time is >= ts_ms."""
        return int(np.searchsorted(self.ts, ts_ms, side="left"))


def fill_gaps(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Insert flat zero-volume candles where no trades happened."""
    if df.empty:
        return df
    step = TIMEFRAME_MS[timeframe]
    full = np.arange(int(df.ts.iloc[0]), int(df.ts.iloc[-1]) + step, step, dtype="int64")
    df = df.set_index("ts").reindex(full)
    df.index.name = "ts"
    missing = df.close.isna()
    df["close"] = df.close.ffill()
    for col in ("open", "high", "low"):
        df.loc[missing, col] = df.loc[missing, "close"]
    df["volume"] = df.volume.fillna(0.0)
    return df.reset_index()


def resample(df: pd.DataFrame, base_tf: str, target_tf: str) -> pd.DataFrame:
    """Aggregate base candles into a larger timeframe, keeping complete buckets only."""
    if base_tf == target_tf:
        return df
    base, target = TIMEFRAME_MS[base_tf], TIMEFRAME_MS[target_tf]
    per_bucket = target // base
    bucket = (df.ts // target) * target
    g = df.groupby(bucket, sort=True)
    out = pd.DataFrame({
        "open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
        "close": g.close.last(), "volume": g.volume.sum(), "n": g.size(),
    })
    out = out[out.n == per_bucket].drop(columns="n")
    out.index.name = "ts"
    return out.reset_index()


def series_from_df(pair: str, timeframe: str, df: pd.DataFrame) -> Series:
    return Series(
        pair=pair, timeframe=timeframe,
        ts=df.ts.to_numpy(dtype="int64"),
        open=df.open.to_numpy(dtype="float64"), high=df.high.to_numpy(dtype="float64"),
        low=df.low.to_numpy(dtype="float64"), close=df.close.to_numpy(dtype="float64"),
        volume=df.volume.to_numpy(dtype="float64"),
    )


def load_market(conn: sqlite3.Connection, pairs: list[str], timeframes: list[str],
                base_tf: str) -> dict[tuple[str, str], Series]:
    market: dict[tuple[str, str], Series] = {}
    for pair in pairs:
        base = fill_gaps(load_candles(conn, pair, base_tf), base_tf)
        if base.empty:
            continue
        for tf in timeframes:
            market[(pair, tf)] = series_from_df(pair, tf, resample(base, base_tf, tf))
    return market


@dataclass(frozen=True)
class Splits:
    """Time boundaries (ms) shared by every series, so no split leaks into another."""

    start: int
    train_end: int
    validation_end: int
    end: int

    def windows(self, n: int) -> list[tuple[int, int]]:
        """Consecutive walk-forward windows over the train period."""
        edges = np.linspace(self.start, self.train_end, n + 1).astype("int64")
        return [(int(edges[i]), int(edges[i + 1])) for i in range(n)]

    @property
    def validation(self) -> tuple[int, int]:
        return self.train_end, self.validation_end

    @property
    def test(self) -> tuple[int, int]:
        return self.validation_end, self.end


def make_splits(market: dict[tuple[str, str], Series], train: float, validation: float) -> Splits:
    """Split by time over the period every series covers."""
    start = max(int(s.ts[0]) for s in market.values())
    end = min(int(s.ts[-1]) for s in market.values()) + 1
    span = end - start
    return Splits(start=start, train_end=start + int(span * train),
                  validation_end=start + int(span * (train + validation)), end=end)
