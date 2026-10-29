"""Phase 1 regression tests: closed candles, independent refresh, and fail-closed health."""
import pytest

from bots.data.fetch import TIMEFRAME_MS, update_recent
from bots.data.health import market_health, refresh_market_pairs
from bots.data.store import get_meta, upsert_candles
from bots.db import connect

STEP = TIMEFRAME_MS["15m"]
NOW = 100 * STEP


class PublicOHLC:
    def __init__(self, now=NOW, broken=()):
        self.now = now
        self.broken = set(broken)
        self.calls = []

    def milliseconds(self):
        return self.now

    def fetch_ohlcv(self, pair, tf):
        self.calls.append(pair)
        if pair in self.broken:
            raise ConnectionError("public API unavailable")
        return [[i * STEP, 100, 101, 99, 100, 5] for i in range(92, 101)]


def test_refresh_every_whitelisted_pair_with_no_bots(conn, cfg):
    cfg["data"]["pairs"] = ["BTC/USD", "ETH/USD"]
    ex = PublicOHLC()
    result = refresh_market_pairs(conn, ex, cfg, NOW)
    assert ex.calls == ["BTC/USD", "ETH/USD"]
    assert result["healthy"]
    assert result["fresh_pairs"] == result["total_pairs"] == 2
    assert all(v == 8 for v in result["updated_candles"].values())
    assert result["last_success_ms"] == NOW
    assert result["last_errors"] == ""


def test_one_pair_failure_keeps_other_pairs_updated_and_marks_not_healthy(conn, cfg):
    cfg["data"]["pairs"] = ["BTC/USD", "ETH/USD"]
    ex = PublicOHLC(broken=("ETH/USD",))
    result = refresh_market_pairs(conn, ex, cfg, NOW)
    assert ex.calls == ["BTC/USD", "ETH/USD"]
    assert result["pairs"]["BTC/USD"]["status"] == "fresh"
    assert result["pairs"]["ETH/USD"]["status"] == "missing"
    assert not result["healthy"]
    assert "ETH/USD" in result["last_errors"]
    assert result["last_success_ms"] is None


def test_stale_and_missing_candles_are_detected_even_when_one_pair_is_fresh(conn, cfg):
    cfg["data"]["pairs"] = ["BTC/USD", "ETH/USD"]
    refresh_market_pairs(conn, PublicOHLC(), cfg, NOW)
    status = market_health(conn, cfg, NOW + 5 * STEP)
    assert not status["healthy"]
    assert status["unhealthy_pairs"] == ["BTC/USD", "ETH/USD"]
    assert all(item["status"] == "stale" for item in status["pairs"].values())


def test_invalid_candle_data_is_rejected_before_database_write(conn):
    class Malformed(PublicOHLC):
        def fetch_ohlcv(self, pair, tf):
            rows = super().fetch_ohlcv(pair, tf)
            rows[1][2] = 1  # high cannot be below open
            return rows

    with pytest.raises(ValueError, match="inconsistent OHLC"):
        update_recent(conn, Malformed(), "BTC/USD", "15m")
    assert conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0


def test_refresh_failure_recovers_on_next_success(conn, cfg):
    cfg["data"]["pairs"] = ["BTC/USD"]
    failed = refresh_market_pairs(conn, PublicOHLC(broken=("BTC/USD",)), cfg, NOW)
    assert not failed["healthy"]
    restored = refresh_market_pairs(conn, PublicOHLC(), cfg, NOW)
    assert restored["healthy"]
    assert restored["last_errors"] == ""
    assert get_meta(conn, "market_refresh:last_success_ms") == str(NOW)


def test_worker_schedules_single_refresh_and_no_overlap(cfg, tmp_path):
    from bots.data.fetch import BACKFILL_DONE
    from bots.data.store import set_meta
    from bots.tests.test_worker import make_worker

    cfg["data"]["pairs"] = ["BTC/USD"]
    worker, _ = make_worker(cfg, tmp_path)
    worker.startup()
    # Simulate completed history so no full backfill is triggered.
    set_meta(worker.conn, BACKFILL_DONE, "1")
    worker.conn.execute(
        "INSERT INTO candles VALUES (?, '15m', ?, 100, 101, 99, 100, 5)",
        ("BTC/USD", NOW - STEP))
    worker.conn.commit()
    worker._due = lambda *_: False
    worker._evolution_retry_due = lambda: False
    spawned = []

    class Busy:
        def poll(self):
            return None

    worker._spawn = lambda command: spawned.append(command) or Busy()
    worker._scheduled()
    worker._scheduled()
    assert spawned == ["update"]
    worker.shutdown()


def test_recent_history_gap_is_not_called_healthy(conn, cfg):
    cfg["data"]["pairs"] = ["BTC/USD"]
    refresh_market_pairs(conn, PublicOHLC(), cfg, NOW)
    conn.execute("DELETE FROM candles WHERE pair='BTC/USD' AND ts=?", (96 * STEP,))
    conn.commit()
    health = market_health(conn, cfg, NOW)
    assert health["pairs"]["BTC/USD"]["status"] == "gapped"
    assert health["pairs"]["BTC/USD"]["recent_missing_candles"] == 1
    assert not health["healthy"]


def test_worker_detects_failed_refresh_process(cfg, tmp_path):
    from bots.tests.test_worker import make_worker

    worker, _ = make_worker(cfg, tmp_path)
    worker.startup()

    class Failed:
        def poll(self):
            return 1

    worker._market_refresh = Failed()
    worker._check_market_refresh_process()
    assert worker._market_refresh is None
    assert worker.state.get("market_refresh_process_error") == "refresh exited with code 1"
    worker.shutdown()
