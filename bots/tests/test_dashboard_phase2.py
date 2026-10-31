"""Phase 2 regression: observed-only performance, costs, navigation and bots.

No exchange keys or live order path required.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
import sqlite3

from bots.db import connect
from bots.paper.league import ensure_schema, research_genomes
from bots.report.performance import (report, export_csv, start_of_period,
                                     resolve_bot_slug, compact_series)
from bots.report.bots_view import bots_index, research_detail
from bots.report.dashboard import create_app
from bots.risk.state import now_ms, LAST_MODE, RiskState

DAY = 86_400_000


def seed_lab(conn: sqlite3.Connection, now: int):
    """Observations are invented in this test fixture ONLY, never in production."""
    ensure_schema(conn)
    genome = research_genomes()["BTC trend"]
    conn.execute(
        "INSERT INTO lab_accounts"
        "(name,pair,genome_json,cash,peak_equity,last_equity,hold_start_price,"
        "last_candle_ts,started_ms,closed_trades,wins,fees)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("BTC trend", "BTC/USD", genome.to_json(), 24, 24, 24, 100,
         now, now - 10 * DAY, 1, 1, 0.16))
    series = ((-9, 20), (-5, 22), (-1, 23), (0, 24))
    for back, equity in series:
        observed_ms = now + back * DAY
        conn.execute(
            "INSERT INTO lab_marks"
            "(name,candle_ts,equity,mark_price,observed_ms,quote_source)"
            " VALUES(?,?,?,?,?,?)",
            ("BTC trend", observed_ms - 3_600_000, equity,
             100 + equity, observed_ms, "kraken_ticker"))
    conn.execute(
        "INSERT INTO lab_fills(name,ts,side,qty,price,fee,pnl,reason) "
        "VALUES('BTC trend',?,'buy',0.1,100,0.08,NULL,'signal entry')",
        (now - 5 * DAY,))
    conn.execute(
        "INSERT INTO lab_fills(name,ts,side,qty,price,fee,pnl,reason) "
        "VALUES('BTC trend',?,'sell',0.1,110,0.08,0.4,'signal exit')",
        (now - DAY,))
    conn.commit()


def test_period_profits_are_differences_between_observed_balances(cfg, conn):
    now = (1_800_000_000_000 // DAY) * DAY + 12 * 3_600_000
    seed_lab(conn, now)
    week = report(conn, cfg, now=now, name="BTC trend", period="7d")
    assert week["period_stats"]["change"] == 2
    assert week["all_time"]["net_pnl"] == 4
    assert week["period_stats"]["completed_trades"] == 1
    assert week["period_stats"]["realized_pnl"] == 0.4
    assert abs(week["period_stats"]["total_fees"] - 0.16) < 1e-9
    assert week["period_stats"]["win_rate_pct"] == 100
    today = report(conn, cfg, now=now, name="BTC trend", period="today")
    assert today["period_stats"]["change"] is None
    assert today["period_stats"]["completed_trades"] == 0
    assert today["period_stats"]["total_fees"] == 0
    all_time = report(conn, cfg, now=now, name="BTC trend", period="all")
    assert all_time["period_stats"]["change"] == 4
    assert all_time["period_stats"]["verified_marks"] == 4
    assert len(all_time["marks"]) == 4


def test_sell_filters_cannot_count_a_buy_as_a_win(cfg, conn):
    now = (1_800_000_000_000 // DAY) * DAY + 12 * 3_600_000
    seed_lab(conn, now)
    sold = report(conn, cfg, now, name="BTC trend", period="all",
                  side="sell", result="profit")
    assert [f["side"] for f in sold["fills"]] == ["sell"]
    assert sold["period_stats"]["buy_orders"] == 1
    assert sold["period_stats"]["sell_orders"] == 1
    assert sold["period_stats"]["win_rate_pct"] == 100
    lost = report(conn, cfg, now, name="BTC trend", period="all", result="loss")
    assert lost["fills"] == []
    assert lost["period_stats"]["completed_trades"] == 1  # filter doesn't rewrite totals


def test_export_csv_uses_same_filters_and_neutralizes_formulas(cfg, conn):
    now = (1_800_000_000_000 // DAY) * DAY + 12 * 3_600_000
    seed_lab(conn, now)
    conn.execute("UPDATE lab_fills SET reason='=EVIL()' WHERE side='sell'")
    conn.commit()
    filtered = report(conn, cfg, now, name="BTC trend", period="all", side="sell")
    csv_text = export_csv(filtered)
    assert "sell" in csv_text and "'=EVIL()" in csv_text
    assert "signal entry" not in csv_text
    assert export_csv(filtered, "equity").count("\n") == 5


def test_strict_period_and_slug_validation(cfg, conn):
    now = (1_800_000_000_000 // DAY) * DAY + 12 * 3_600_000
    seed_lab(conn, now)
    import pytest
    with pytest.raises(ValueError):
        report(conn, cfg, now, name="BTC trend", period="yesterday")
    with pytest.raises(ValueError):
        report(conn, cfg, now, name="BTC trend", period="all", side="refund")
    with pytest.raises(ValueError):
        resolve_bot_slug("../../etc/passwd")
    assert resolve_bot_slug("btc-trend") == "BTC trend"
    assert len(compact_series([{"n": i} for i in range(1000)])) <= 320


def test_bot_directory_and_details_read_only(cfg, conn):
    now = (1_800_000_000_000 // DAY) * DAY + 12 * 3_600_000
    seed_lab(conn, now)
    body = bots_index(conn, cfg)
    assert "/bots/research/btc-trend" in body
    assert "Standard paper and real-money accounts" in body
    assert "research" in body.lower()
    profile = research_detail(conn, cfg, now, "BTC trend", "all")
    assert "Follows longer price movements" in profile
    assert "Cannot place live orders" in profile
    assert "Recorded" not in profile or "profit" in profile.lower()
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_no_crash_before_experiment_initializes(cfg, conn):
    detail = research_detail(conn, cfg, now_ms(), "SOL breakout", "7d")
    assert "No recorded trades or balances yet" in detail


def test_routes_and_live_account_separation(cfg, tmp_path):
    from bots.tests.riskkit import add_bot
    path = tmp_path / "phase2.db"
    conn = connect(path)
    now = now_ms()
    seed_lab(conn, now)
    RiskState(conn).set(LAST_MODE, "PAPER")
    add_bot(conn, "regulated-bot", "paper")
    conn.execute(
        "INSERT INTO accounts(bot_id,mode,active,epoch,allocation,cash,"
        "peak_equity,last_equity,day_start_equity,started_at)"
        " VALUES('regulated-bot','paper',1,1,50,50,50,50,50,?)",
        (now-DAY,))
    standard_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    add_bot(conn, "live-bot", "live")
    conn.execute(
        "INSERT INTO accounts(bot_id,mode,active,epoch,allocation,cash,"
        "peak_equity,last_equity,day_start_equity,started_at)"
        " VALUES('live-bot','live',1,1,20,20,20,20,20,?)",
        (now-DAY,))
    live_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    client = TestClient(create_app(path, cfg, env={"MODE": "PAPER"}))
    assert client.get("/bots").status_code == 200
    assert "/bots/account/" + str(live_id) in client.get("/bots").text
    assert client.get("/bots/research/btc-trend?period=all").status_code == 200
    assert "No recorded trades or balances yet" in client.get(
        "/bots/research/eth-pullback").text
    assert client.get("/bots/research/unknown").status_code == 404
    assert client.get("/bots/account/999999").status_code == 404
    assert "STANDARD PAPER ACCOUNT" in client.get(
        "/bots/account/" + str(standard_id)).text
    assert "REAL-MONEY ACCOUNT" in client.get(
        "/bots/account/" + str(live_id)).text
    assert client.get("/research/forward?name=BTC%20trend&period=7d").status_code == 200
    assert "Where did the fees go?" in client.get("/research/forward").text
    assert client.get("/api/performance?name=not-a-bot").status_code == 404
    assert client.get("/api/performance?period=future").status_code == 400
    week = client.get("/api/performance?name=BTC%20trend&period=7d")
    assert week.status_code == 200
    assert week.json()["real_money"] is False
    assert "filled" not in week.json().get("mode", "").lower()
    csv_data = client.get("/api/performance.csv?name=BTC%20trend&kind=trades")
    assert csv_data.status_code == 200 and "text/csv" in csv_data.headers["content-type"]
    assert client.get("/api/performance.csv?kind=invalid").status_code == 400
    # Unmodified endpoints continue to work, including the strict no-trading auth.
    assert client.get("/api/status").status_code == 200
    assert client.post("/bots/regulated-bot/approve",
                       data={"token": "invalid"}).status_code == 403
