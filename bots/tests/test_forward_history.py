"""New genuine-forward paper audit tests; no real exchange or external web needed."""
from __future__ import annotations

from bots.db import connect
from bots.paper.league import tick
from bots.paper.forward_history import forward_history
from bots.tests.test_research_league import config, seed_candles


class PublicTicker:
    def fetch_ticker(self, pair: str):
        return {"last": 103.25}


def _set_up():
    now = (1_800_000_000_000 // 3_600_000) * 3_600_000
    conn = connect(":memory:")
    cfg = config()
    seed_candles(conn, now)
    tick(conn, cfg, now, price_source=PublicTicker())
    return conn, cfg, now


def test_first_forward_mark_has_real_ticker_source_and_provenance():
    conn, cfg, now = _set_up()
    h = forward_history(conn, cfg)
    assert h["real_money"] is False
    assert h["simulation"] is True
    assert h["stats"]["capital"] == 60
    assert h["stats"]["completed_trades"] == 0
    assert h["stats"]["recorded_marks"] == 1
    assert h["stats"]["observed_hours"] == 1
    assert h["observations"][0]["observed_ms"] == now
    assert h["observations"][0]["quote_source"] == "kraken_ticker"
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_new_hour_appends_once_and_never_backfills_missed_hours():
    conn, cfg, now = _set_up()
    seed_candles(conn, now + 3_600_000)
    first = tick(conn, cfg, now + 3_600_000, price_source=PublicTicker())
    assert first["processed"] == 3
    h = forward_history(conn, cfg, name="BTC trend")
    assert len(h["observations"]) == 2
    assert h["stats"]["observed_hours"] == 2
    tick(conn, cfg, now + 3_600_000, price_source=PublicTicker())
    h = forward_history(conn, cfg, name="BTC trend")
    assert len(h["observations"]) == 2
    assert conn.execute("SELECT COUNT(*) FROM lab_fills").fetchone()[0] >= 0


def test_aggregate_does_not_forward_fill_missing_bot_mark():
    conn, cfg, now = _set_up()
    seed_candles(conn, now + 3_600_000)
    tick(conn, cfg, now + 3_600_000, price_source=PublicTicker())
    conn.execute("DELETE FROM lab_marks WHERE name='ETH pullback' AND candle_ts=("
                 "SELECT MAX(candle_ts) FROM lab_marks WHERE name='ETH pullback')")
    conn.commit()
    group = forward_history(conn, cfg)
    assert len(group["observations"]) == 1
    btc = forward_history(conn, cfg, name="BTC trend")
    assert len(btc["observations"]) == 2
    assert group["stats"]["completed_trades"] == 0
    assert group["stats"]["capital"] == 60


def test_settled_trades_and_fees_are_independently_auditable():
    conn, cfg, now = _set_up()
    # A synthetic paper-only closed round trip; never touches genuine live tables.
    conn.execute("INSERT INTO lab_fills(name,ts,side,qty,price,fee,pnl,reason) "
                 "VALUES('BTC trend', ?, 'buy', 0.1,100,0.08,NULL,'test')", (now,))
    conn.execute("INSERT INTO lab_fills(name,ts,side,qty,price,fee,pnl,reason) "
                 "VALUES('BTC trend', ?, 'sell', 0.1,110,0.088,0.832,'test')", (now,))
    conn.execute("UPDATE lab_accounts SET closed_trades=1,wins=1,fees=0.168,"
                 "last_equity=20.832,cash=20.832 WHERE name='BTC trend'")
    conn.commit()
    result = forward_history(conn, cfg, name="BTC trend")
    assert result["stats"]["realized_pnl"] == 0.832
    assert result["stats"]["completed_trades"] == 1
    assert abs(result["stats"]["fees"] - 0.168) < 1e-8
    assert result["stats"]["win_rate_pct"] == 100
    assert result["fills"][0]["side"] == "sell"
    assert result["fills"][0]["pnl"] == 0.832
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_unknown_name_does_not_become_sql_or_any_account():
    conn, cfg, now = _set_up()
    try:
        forward_history(conn, cfg, name="'; DROP TABLE lab_accounts; --")
    except ValueError:
        pass
    else:
        assert False, "Expected unknown research name to be rejected"
    assert conn.execute("SELECT COUNT(*) FROM lab_accounts").fetchone()[0] == 3


def test_readonly_api_and_html_page_are_available():
    from fastapi.testclient import TestClient
    from bots.report.dashboard import create_app
    conn, cfg, now = _set_up()
    # SQLite memory database is connection-private, use a disk database instead.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "forward.db"
        dest = connect(path)
        conn.backup(dest)
        dest.close()
        client = TestClient(create_app(path, cfg, env={"MODE": "PAPER"}))
        response = client.get("/api/research/forward")
        assert response.status_code == 200
        assert response.json()["stats"]["capital"] == 60
        assert response.json()["real_money"] is False
        assert client.get("/api/research/forward?name=invalid").status_code == 404
        exported = client.get("/api/research/forward.csv?kind=equity")
        assert exported.status_code == 200
        assert exported.text.startswith("candle_ts,observed_ms,equity,")
        assert client.get("/api/research/forward.csv?kind=invalid").status_code == 400
