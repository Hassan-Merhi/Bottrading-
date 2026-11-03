"""Hourly Telegram update: every bot with coin, money, the hour's trades, births and deaths."""
from __future__ import annotations

from bots.db import connect
from bots.paper import arena, league
from bots.report.hourly import chunks, hourly_report_text
from bots.tests.test_arena import NOW, HOUR, cfg, fake_run
from bots.tests.test_research_league import seed_candles


def test_hourly_report_lists_bots_trades_deaths_and_events():
    conn = connect(":memory:")
    c = cfg()
    fake_run(conn, n=3)
    arena.refresh_from_run(conn, c, "run-a", NOW - 2 * HOUR)
    seed_candles(conn, NOW - 2 * HOUR)
    arena.tick(conn, c, NOW - 2 * HOUR)
    names = [b["name"] for b in arena.active(conn)]
    conn.execute("UPDATE lab_marks SET equity=20.00")
    conn.execute("UPDATE lab_accounts SET last_equity=20.37, closed_trades=1, wins=1 WHERE name=?", (names[0],))
    conn.execute("INSERT INTO lab_fills(name,ts,side,qty,price,fee,pnl,reason) VALUES(?,?,?,?,?,?,?,?)",
                 (names[0], NOW - 600_000, "sell", 0.0004, 50_000, 0.16, 0.37, "target observed at close"))
    conn.execute("UPDATE lab_accounts SET last_equity=14, max_drawdown=0.3 WHERE name=?", (names[1],))
    conn.execute("UPDATE lab_accounts SET last_equity=20, qty=0.0004 WHERE name=?", (names[2],))
    conn.commit()
    assert arena.retire_losers(conn, c, NOW - 300_000) == 1
    conn.execute("INSERT INTO risk_events(ts,kind,detail) VALUES(?,?,?)",
                 (NOW - 60_000, "entries_resumed", "Connection and data healthy again; new trades allowed"))
    conn.commit()
    text = hourly_report_text(conn, c, NOW)
    assert text.startswith("Hourly update")
    assert "Paper arena (3 bots" in text
    assert "BTC 1h ema cross" in text and "+0.37$" in text and "sold 0.0004 BTC @ 5e+04 +0.37$ (target" in text
    assert "DIED: forward drawdown 30%" in text
    assert "holding SOL" in text or "holding ETH" in text or "holding BTC" in text
    # Graveyard rows carry the wall-clock time, so check that line against it.
    import time
    assert "Evolution buried 1 bot(s)" in hourly_report_text(conn, c, int(time.time() * 1000))
    assert "entries resumed: Connection and data healthy again" in text
    assert "Fake money unless marked REAL" in text


def test_hourly_report_on_empty_database_and_chunking():
    conn = connect(":memory:")
    text = hourly_report_text(conn, cfg(), NOW)
    assert "0 running" in text and "Paper arena: empty" in text
    long = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = chunks(long, limit=1000)
    assert len(parts) > 5 and all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == long


def test_retired_bots_drop_out_after_an_hour():
    conn = connect(":memory:")
    c = cfg()
    fake_run(conn, n=2)
    arena.refresh_from_run(conn, c, "run-a", NOW - 5 * HOUR)
    seed_candles(conn, NOW - 5 * HOUR)
    arena.tick(conn, c, NOW - 5 * HOUR)
    names = [b["name"] for b in arena.active(conn)]
    conn.execute("UPDATE lab_accounts SET last_equity=10, max_drawdown=0.5 WHERE name=?", (names[0],))
    conn.commit()
    arena.retire_losers(conn, c, NOW - 3 * HOUR)
    text = hourly_report_text(conn, c, NOW)
    assert names[0].split("/", 1)[1] not in text and names[1].split("/", 1)[1] in text
    assert "Paper arena (1 bots" in text
