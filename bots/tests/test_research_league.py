"""Regression tests: experiments cannot be promoted and cannot fabricate fills."""
from __future__ import annotations

import sqlite3

from bots.db import connect
from bots.paper import league
from bots.research import agent
from bots.strategy.genome import ENTRY_TYPES, Genome
from bots.strategy.signals import compute_signals


def config():
    return {"research_league": {"enabled": True, "allocation_usd": 20},
            "research": {"enabled": True},
            "backtest": {"taker_fee_pct": 0.80, "slippage_pct": 0.05,
                         "spread_bps": 10,
                         "min_order": {"BTC/USD": {"min_cost_usd": 5, "min_qty": 0.0001},
                                       "ETH/USD": {"min_cost_usd": 5, "min_qty": 0.002},
                                       "SOL/USD": {"min_cost_usd": 5, "min_qty": 0.02}}}}


def seed_candles(conn, end_ms, hours=95):
    start = end_ms - hours * 3_600_000
    for pair in ("BTC/USD", "ETH/USD", "SOL/USD"):
        for i in range(hours * 4):
            candle = start + i * 900_000
            price = 100.0 + (i // 4) * 0.3
            conn.execute(
                "INSERT OR REPLACE INTO candles(pair,timeframe,ts,open,high,low,close,volume)"
                "VALUES(?,?,?,?,?,?,?,?)",
                (pair, "15m", candle, price, price * 1.01, price * 0.99, price, 10.0))
    conn.commit()


def test_forward_only_seeds_without_replaying_old_signals():
    conn = connect(":memory:")
    now = 1_800_000_000_000
    now = (now // 3_600_000) * 3_600_000
    seed_candles(conn, now)
    first = league.tick(conn, config(), now)
    assert first["processed"] == 0
    rows = conn.execute("SELECT COUNT(*) FROM lab_accounts").fetchone()[0]
    assert rows == 3
    assert conn.execute("SELECT COUNT(*) FROM lab_fills").fetchone()[0] == 0
    repeat = league.tick(conn, config(), now)
    assert repeat["processed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM lab_marks").fetchone()[0] == 3
    seed_candles(conn, now + 3_600_000)
    next_tick = league.tick(conn, config(), now + 3_600_000)
    assert next_tick["processed"] == 3
    assert conn.execute("SELECT COUNT(*) FROM lab_marks").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM deployed_bots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_paper_league_exits_charge_realistic_fees():
    conn = connect(":memory:")
    now = (1_800_000_000_000 // 3_600_000) * 3_600_000
    seed_candles(conn, now)
    league.tick(conn, config(), now)
    # Inject a paper position only; ensure next closed-candle stop pays taker costs.
    conn.execute("UPDATE lab_accounts SET cash=0, qty=0.1, entry_cost=10,"
                 "entry_price=100, entry_ts=?, stop_price=1000,"
                 "target_price=5000 WHERE name='BTC trend'", (now-3_600_000,))
    conn.commit()
    seed_candles(conn, now + 3_600_000)
    league.tick(conn, config(), now + 3_600_000)
    acc = conn.execute("SELECT * FROM lab_accounts WHERE name='BTC trend'").fetchone()
    assert acc["qty"] == 0
    assert acc["closed_trades"] == 1
    assert acc["fees"] > 0
    assert conn.execute("SELECT COUNT(*) FROM lab_fills WHERE side='sell'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_research_is_read_only_and_deduplicates(monkeypatch):
    conn = connect(":memory:")
    source = {"query": "test", "source": "Google News", "url": "https://news.google.com/rss/articles/a",
              "title": "Public market news", "snippet": ""}
    monkeypatch.setattr(agent, "_google_news", lambda q: [source])
    one = agent.run_research(conn, config(), env={}, now_ms=1_800_000_000_000)
    assert one["articles_added"] == 1
    assert one["can_trade"] is False
    assert conn.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    two = agent.run_research(conn, config(), env={}, now_ms=1_800_000_000_000 + 3_600_000)
    assert two["articles_added"] == 1


def test_new_trend_pullback_is_valid_signal_family():
    assert "trend_pullback" in ENTRY_TYPES
    old = league.research_genomes()["BTC trend"]
    genome = Genome.from_dict({**old.to_dict(), "entry_type": "trend_pullback"})
    assert genome.entry_type == "trend_pullback"
    assert "pullback" in genome.describe().lower() or "rebound" in genome.describe().lower()
