"""Regressions for strictly forward, restart-persistent paper evidence."""
import math

from bots.db import connect
from bots.paper.forward import evidence, history, net_hold_return, observe
from bots.tests.riskkit import add_bot, make_guard, Clock


def test_hourly_quote_snapshots_cannot_fake_missing_calendar_days(cfg, conn, tmp_path):
    add_bot(conn, "b1")
    clock = Clock()
    guard, paper, *_ = make_guard(cfg, conn, tmp_path, clock=clock)
    guard.open_paper_account("b1", 50_000)
    acc = guard.ledger.account("b1", "paper")
    assert observe(conn, acc, 50_000, clock(), cfg)
    assert observe(conn, acc, 50_100, clock() + 1000, cfg)
    ev = evidence(conn, acc["account_id"], clock() + 1000)
    assert ev["observed_days"] == 1
    assert ev["observed_hours"] == 1  # same hour upsert, not two sessions
    assert len(history(conn, acc["account_id"])) == 1
    assert history(conn, acc["account_id"])[0]["price"] == 50_100
    assert not observe(conn, acc, float("nan"), clock(), cfg)
    assert not observe(conn, acc, -1, clock(), cfg)
    assert ev["verified_trades"] == 0 and ev["profit_factor"] == 0


def test_paper_snapshots_survive_database_reconnect(cfg, tmp_path):
    db_path = tmp_path / "forward.db"
    conn = connect(db_path)
    add_bot(conn, "b1")
    guard, paper, *_ = make_guard(cfg, conn, tmp_path)
    guard.open_paper_account("b1", 50_000)
    a = guard.ledger.account("b1", "paper")
    observe(conn, a, 49_000, a["started_at"], cfg)
    observe(conn, a, 49_200, a["started_at"] + 24 * 3600_000, cfg)
    ident = a["account_id"]
    conn.close()
    restored = connect(db_path)
    ev = evidence(restored, ident, a["started_at"] + 2 * 24 * 3600_000)
    assert ev["observed_days"] == 2
    assert ev["max_observation_gap_hours"] == 24
    assert len(history(restored, ident)) == 2
    restored.close()


def test_net_hold_baseline_includes_spread_slippage_and_both_taker_fees(cfg):
    assert net_hold_return(None, 100, cfg) is None
    # A flat-priced coin loses money after the round-trip costs.
    same = net_hold_return(100, 100, cfg)
    assert same < 0
    boosted = net_hold_return(100, 110, cfg)
    assert boosted > same
    assert math.isfinite(boosted)
