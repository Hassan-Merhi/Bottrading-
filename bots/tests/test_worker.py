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


def test_export_state_skips_candles_and_sends_offsite(tmp_path):
    import gzip
    from bots.db import connect
    from bots.worker import export_state, send_offsite_backup
    db = tmp_path / "x.db"
    conn = connect(db)
    conn.execute("INSERT INTO candles VALUES ('BTC/USD','15m',1,1,1,1,1,1)")
    conn.execute("INSERT INTO meta VALUES ('k','v')")
    conn.commit()
    conn.close()
    out = export_state(db, tmp_path / "state.sqlite.gz")
    raw = gzip.open(out, "rb").read()
    tmp = tmp_path / "restored.db"
    tmp.write_bytes(raw)
    restored = sqlite3.connect(tmp)
    assert restored.execute("SELECT value FROM meta WHERE key='k'").fetchone()[0] == "v"
    assert "candles" not in {r[0] for r in restored.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    class FileAlerts(Alerts):
        def __init__(self):
            super().__init__()
            self.files = []

        def send_file(self, path, caption=""):
            self.files.append((path.name, caption))
            return True

    a = FileAlerts()
    assert send_offsite_backup(db, a).startswith("sent")
    assert a.files and a.files[0][0].endswith(".sqlite.gz") and "restore" in a.files[0][1].lower()
    assert send_offsite_backup(db, Alerts()) is None  # no file support: skipped quietly


def test_telegram_commands_pause_resume_bots_and_kill(cfg, tmp_path):
    from bots.risk.state import KILL_SWITCH, OWNER_PAUSE
    from bots.tests.riskkit import add_bot
    w, alerts = make_worker(cfg, tmp_path)
    w.startup()
    assert w._command("/bots") == "No bots running yet."
    add_bot(w.conn, "b1", "paper")
    w.loop_once()  # opens the paper account
    text = w._command("/bots")
    assert "b1 [paper] BTC/USD" in text and "flat" in text
    assert "Paused" in w._command("/pause") and w.state.is_set(OWNER_PAUSE)
    assert "Resumed" in w._command("/resume") and not w.state.is_set(OWNER_PAUSE)
    assert "/kill confirm" in w._command("/kill") and not w.state.is_set(KILL_SWITCH)
    assert "Kill switch" in w._command("/KILL confirm") and w.state.is_set(KILL_SWITCH)
    assert "/pause" in w._command("/whatever")
    w.shutdown()


def test_dead_evolution_is_reported_and_retried_once(cfg, tmp_path):
    from datetime import datetime, timezone
    w, alerts = make_worker(cfg, tmp_path)
    w.startup()

    class Dead:
        def __init__(self, code):
            self.code = code

        def poll(self):
            return self.code

    spawned = []
    w._spawn = lambda cmd: spawned.append(cmd) or Dead(None)
    assert w._command("/evolve") == "Evolution started. You get a summary here when it finishes."
    assert spawned == ["evolve"] and "already in progress" in w._command("/evolve")
    # The process dies of a memory kill: an alert, no second run until the retry rule.
    w._evolution = Dead(-9)
    w._check_evolution_process()
    assert w._evolution is None and any("out of memory" in t for _, t in alerts.sent)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    w.state.set("last_evolution", today)
    assert not w._evolution_retry_due()  # no unfinished run recorded
    w.conn.execute("INSERT INTO runs (run_id, started_at, config_json, data_label) VALUES "
                   "('r1', ?, '{}', 'x')", (datetime.now(timezone.utc).isoformat(),))
    w.conn.commit()
    assert w._evolution_retry_due() and not w._evolution_retry_due()  # once per day
    w.shutdown()
