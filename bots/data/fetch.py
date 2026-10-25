"""Fetch Kraken historical candles through the public API (no keys needed).

Kraken's OHLC endpoint only returns the most recent 720 candles per timeframe,
so years of history cannot come from it. Three sources are supported:

* `backfill_from_trades` — rebuilds base-timeframe candles from Kraken's full
  public trade history. Slow (hours for BTC) but complete and resumable.
* `import_kraken_csv` — loads Kraken's downloadable OHLCVT CSV files
  (support.kraken.com, "Downloadable historical OHLCVT data"). Fastest.
* `update_recent` — tops up the latest candles from the OHLC endpoint. Run it
  regularly to keep the store current after a backfill or import.

This module only uses public market-data endpoints. It never places orders.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from bots.data.store import get_meta, set_meta, upsert_candles

BACKFILL_DONE = "backfill_done"
TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
                "1d": 86_400_000}


def make_exchange(name: str = "kraken"):
    import ccxt

    return getattr(ccxt, name)({"enableRateLimit": True})


def update_recent(conn: sqlite3.Connection, exchange, pair: str, timeframe: str) -> int:
    """Store the most recent closed candles from the OHLC endpoint."""
    rows = exchange.fetch_ohlcv(pair, timeframe)
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    step = TIMEFRAME_MS[timeframe]
    now_ms = exchange.milliseconds()
    df = df[df.ts + step <= now_ms]  # drop the still-forming candle
    return upsert_candles(conn, pair, timeframe, df)


def trades_to_candles(trades: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate trades (columns ts [ms], price, volume; time-ordered) into candles."""
    step = TIMEFRAME_MS[timeframe]
    bucket = (trades.ts // step) * step
    g = trades.groupby(bucket, sort=True)
    out = pd.DataFrame({
        "open": g.price.first(),
        "high": g.price.max(),
        "low": g.price.min(),
        "close": g.price.last(),
        "volume": g.volume.sum(),
    })
    out.index.name = "ts"
    return out.reset_index()


def _merge_boundary(conn: sqlite3.Connection, pair: str, timeframe: str,
                    candles: pd.DataFrame) -> pd.DataFrame:
    """Merge the first new candle with a stored candle for the same bucket.

    Pages of trades split buckets arbitrarily, so the first bucket of a page
    may already hold trades from the previous page.
    """
    if candles.empty:
        return candles
    first_ts = int(candles.ts.iloc[0])
    row = conn.execute(
        "SELECT open, high, low, volume FROM candles WHERE pair=? AND timeframe=? AND ts=?",
        (pair, timeframe, first_ts),
    ).fetchone()
    if row is None:
        return candles
    candles = candles.copy()
    candles.loc[candles.index[0], "open"] = row[0]
    candles.loc[candles.index[0], "high"] = max(row[1], candles.high.iloc[0])
    candles.loc[candles.index[0], "low"] = min(row[2], candles.low.iloc[0])
    candles.loc[candles.index[0], "volume"] = row[3] + candles.volume.iloc[0]
    return candles


def backfill_from_trades(conn: sqlite3.Connection, exchange, pair: str, timeframe: str,
                         since_ms: int, until_ms: int | None = None,
                         progress: Callable[[int, int], None] | None = None,
                         max_pages: int | None = None) -> int:
    """Rebuild candles from Kraken's public trade history. Resumable.

    The paging cursor is saved in the `meta` table in the same transaction as
    the candles, so an interrupted backfill picks up exactly where it stopped.
    """
    exchange.load_markets()
    market_id = exchange.market(pair)["id"]
    cursor_key = f"trades_cursor:{pair}:{timeframe}"
    saved = get_meta(conn, cursor_key)
    cursor = int(saved) if saved else since_ms * 1_000_000  # Kraken cursors are nanoseconds
    until_ms = until_ms or exchange.milliseconds()
    pages = 0
    written = 0
    while True:
        resp = exchange.publicGetTrades({"pair": market_id, "since": str(cursor)})
        result = resp["result"]
        last = int(result["last"])
        key = next(k for k in result if k != "last")
        raw = result[key]
        if not raw:
            break
        trades = pd.DataFrame({
            "ts": [int(float(t[2]) * 1000) for t in raw],
            "price": [float(t[0]) for t in raw],
            "volume": [float(t[1]) for t in raw],
        })
        trades = trades[trades.ts < until_ms]
        candles = _merge_boundary(conn, pair, timeframe, trades_to_candles(trades, timeframe))
        with conn:
            written += upsert_candles(conn, pair, timeframe, candles, commit=False)
            set_meta(conn, cursor_key, str(last), commit=False)
        pages += 1
        if progress:
            progress(pages, int(trades.ts.iloc[-1]) if len(trades) else until_ms)
        if last <= cursor or last // 1_000_000 >= until_ms or len(trades) < len(raw):
            break
        if max_pages is not None and pages >= max_pages:
            break
        cursor = last
    return written


def import_kraken_csv(conn: sqlite3.Connection, path: str | Path, pair: str, timeframe: str) -> int:
    """Import a Kraken OHLCVT CSV (columns: unix seconds, o, h, l, c, volume, trades)."""
    df = pd.read_csv(path, header=None,
                     names=["ts", "open", "high", "low", "close", "volume", "trades"])
    df["ts"] = df.ts.astype("int64") * 1000
    return upsert_candles(conn, pair, timeframe, df)


def backfill_all(conn: sqlite3.Connection, cfg: dict, log: Callable[[str], None] = print) -> None:
    """Backfill every whitelisted pair at the base timeframe, then top up recent candles."""
    exchange = make_exchange(cfg["data"]["exchange"])
    tf = cfg["data"]["base_timeframe"]
    years = cfg["data"]["history_years"]
    since_ms = int((time.time() - years * 365.25 * 86400) * 1000)
    for pair in cfg["data"]["pairs"]:
        log(f"Backfilling {pair} {tf} from trades since {pd.to_datetime(since_ms, unit='ms')}")

        def progress(pages: int, ts: int, pair=pair) -> None:
            if pages % 100 == 0:
                log(f"  {pair}: {pages} pages, reached {pd.to_datetime(ts, unit='ms')}")

        backfill_from_trades(conn, exchange, pair, tf, since_ms, progress=progress)
        n = update_recent(conn, exchange, pair, tf)
        log(f"  {pair}: done ({n} recent candles topped up)")
    set_meta(conn, BACKFILL_DONE, str(int(time.time() * 1000)))
