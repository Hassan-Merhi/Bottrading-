import numpy as np
import pandas as pd

from bots.data import fetch
from bots.data.loader import fill_gaps, load_market, make_splits, resample
from bots.data.store import candle_range, get_meta, load_candles, upsert_candles

H = 3_600_000
Q = 900_000
T0 = 1_700_000_000_000 // H * H


def candles(ts, close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({"ts": ts, "open": close, "high": close + 1, "low": close - 1,
                         "close": close, "volume": np.ones(len(ts))})


def test_upsert_and_load_roundtrip(conn):
    df = candles([T0, T0 + Q], [10, 11])
    upsert_candles(conn, "BTC/USD", "15m", df)
    upsert_candles(conn, "BTC/USD", "15m", candles([T0 + Q], [12]))  # replace, not duplicate
    out = load_candles(conn, "BTC/USD", "15m")
    assert list(out.close) == [10, 12]
    assert candle_range(conn, "BTC/USD", "15m") == (T0, T0 + Q, 2)


def test_trades_to_candles():
    trades = pd.DataFrame({"ts": [T0 + 1, T0 + 2, T0 + Q + 5], "price": [10.0, 12.0, 9.0],
                           "volume": [1.0, 2.0, 3.0]})
    c = fetch.trades_to_candles(trades, "15m")
    assert list(c.ts) == [T0, T0 + Q]
    assert c.iloc[0][["open", "high", "low", "close", "volume"]].tolist() == [10, 12, 10, 12, 3]
    assert c.iloc[1].close == 9


class FakeKraken:
    """Mimics ccxt.kraken's public trade endpoint with a nanosecond cursor."""

    def __init__(self, trades, page=3, now_ms=T0 + 10 * H):
        self.trades = trades  # list of (ts_ms, price, volume)
        self.page = page
        self.now = now_ms
        self.calls = 0

    def load_markets(self):
        pass

    def market(self, pair):
        return {"id": "XXBTZUSD"}

    def milliseconds(self):
        return self.now

    def publicGetTrades(self, params):
        self.calls += 1
        since_ns = int(params["since"])
        rows = [t for t in self.trades if t[0] * 1_000_000 > since_ns][: self.page]
        last = rows[-1][0] * 1_000_000 if rows else since_ns
        return {"error": [], "result": {
            "XXBTZUSD": [[str(p), str(v), ts / 1000, "b", "l", "", 1] for ts, p, v in rows],
            "last": str(last)}}

    def fetch_ohlcv(self, pair, timeframe):
        return [[T0 + i * Q, 1, 2, 0.5, 1.5, 10] for i in range(5)]


def test_backfill_from_trades_merges_page_boundaries_and_resumes(conn):
    trades = [(T0 + i * 60_000, 100.0 + i, 1.0) for i in range(20)]  # 20 minutes, 1 trade/min
    ex = FakeKraken(trades, page=3)
    fetch.backfill_from_trades(conn, ex, "BTC/USD", "15m", since_ms=T0 - 1, max_pages=2)
    assert get_meta(conn, "trades_cursor:BTC/USD:15m") is not None
    fetch.backfill_from_trades(conn, ex, "BTC/USD", "15m", since_ms=T0 - 1)  # resume
    out = load_candles(conn, "BTC/USD", "15m")
    assert list(out.ts) == [T0, T0 + Q]
    first = out.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (100, 114, 100, 114, 15)
    assert out.iloc[1].volume == 5 and out.iloc[1].close == 119


def test_update_recent_drops_forming_candle(conn):
    ex = FakeKraken([], now_ms=T0 + 4 * Q + 10)  # candle at T0+4Q is still open
    n = fetch.update_recent(conn, ex, "BTC/USD", "15m")
    assert n == 4


def test_import_kraken_csv(conn, tmp_path):
    p = tmp_path / "XBTUSD_15.csv"
    p.write_text(f"{T0 // 1000},1,2,0.5,1.5,10,3\n{T0 // 1000 + 900},1.5,2,1,1.8,4,2\n")
    assert fetch.import_kraken_csv(conn, p, "BTC/USD", "15m") == 2
    assert list(load_candles(conn, "BTC/USD", "15m").ts) == [T0, T0 + Q]


def test_fill_gaps_inserts_flat_candles():
    df = candles([T0, T0 + 3 * Q], [10, 13])
    out = fill_gaps(df, "15m")
    assert list(out.ts) == [T0 + i * Q for i in range(4)]
    assert out.iloc[1][["open", "high", "low", "close", "volume"]].tolist() == [10, 10, 10, 10, 0]


def test_resample_keeps_complete_buckets_only():
    df = candles([T0 + i * Q for i in range(6)], [1, 2, 3, 4, 5, 6])
    out = resample(df, "15m", "1h")
    assert len(out) == 1
    row = out.iloc[0]
    assert (row.open, row.high, row.low, row.close, row.volume) == (1, 5, 0, 4, 4)


def test_splits_are_time_ordered(conn):
    df = candles([T0 + i * Q for i in range(400)], np.arange(400) + 100)
    upsert_candles(conn, "BTC/USD", "15m", df)
    market = load_market(conn, ["BTC/USD"], ["15m", "1h"], "15m")
    sp = make_splits(market, 0.6, 0.2)
    assert sp.start < sp.train_end < sp.validation_end < sp.end
    w = sp.windows(4)
    assert w[0][0] == sp.start and w[-1][1] == sp.train_end
    assert all(a[1] == b[0] for a, b in zip(w, w[1:]))
