import sqlite3

import pytest

from bots.risk.state import CRASH_LOCK, LAST_MODE, RUNNING, RiskState
from bots.tests.riskkit import Alerts, Clock, FakeKraken, live_cfg
from bots.tests.test_paper import FakeMarket
from bots.worker import Worker, backup_database


def make_worker(cfg, tmp_path, env=None, kraken=None):
    alerts = Alerts()
    md = FakeMarket(Clock())
    w = Worker(cfg, env or {}, tmp_path / "w.db", alerter=alerts, market_data=md,
               live_exchange_factory=lambda env: kraken)
    w._spawn = lambda cmd: None  # no subprocesses in tests
    return w, alerts


def test_clean_restart_has_no_crash_lock(cfg, tmp_path):
    w, _ = make_worker(cfg, tmp_path)
    w.startup()
    assert RiskState(w.conn).get(LAST_MODE) == "PAPER"
    w.shutdown()
    w2, _ = make_worker(cfg, tmp_path)
    w2.startup()
    assert not w2.state.is_set(CRASH_LOCK)


def test_crash_restarts_in_paper_until_owner_confirms(cfg, tmp_path):
    cfg = live_cfg(cfg)
    (tmp_path / "LIVE_CONFIRMED.txt").write_text("60")
    env = {"MODE": "LIVE", "KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "s",
           "BOTS_STATE_DIR": str(tmp_path)}
    kraken = FakeKraken(Clock())
    w, _ = make_worker(cfg, tmp_path, env, kraken)
    w.startup()
    assert w.guard.mode().mode == "LIVE"
    w.conn.close()  # crash: no shutdown()
    w2, alerts = make_worker(cfg, tmp_path, env, kraken)
    w2.startup()
    assert w2.state.is_set(CRASH_LOCK)
    assert w2.guard.mode().mode == "PAPER"
    assert "critical" in alerts.kinds()


def test_refuses_to_run_if_key_can_withdraw(cfg, tmp_path):
    kraken = FakeKraken(Clock())
    kraken.can_withdraw = True
    env = {"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "s"}
    w, alerts = make_worker(cfg, tmp_path, env, kraken)
    with pytest.raises(SystemExit):
        w.startup()
    assert "critical" in alerts.kinds()
    assert not RiskState(sqlite3.connect(tmp_path / "w.db")).is_set(RUNNING)


def test_loop_once_survives_errors_and_halts_entries(cfg, tmp_path):
    w, _ = make_worker(cfg, tmp_path)
    w.startup()

    def boom():
        raise RuntimeError("unexpected")

    w.trader.tick = boom
    w.loop_once()
    assert w.state.is_set("entry_halt")
    w.shutdown()


def test_backup_keeps_newest(tmp_path):
    db = tmp_path / "x.db"
    sqlite3.connect(db).execute("CREATE TABLE t (a)")
    for _ in range(3):
        out = backup_database(db, tmp_path / "b", keep=2)
        assert out.exists()
        import time
        time.sleep(1.05)
    assert len(list((tmp_path / "b").glob("bots-*.db"))) == 2
