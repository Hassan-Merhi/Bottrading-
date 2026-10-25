import json

import numpy as np
import pytest

from bots.paper import trader as trader_mod
from bots.paper.exchange import PaperExchange
from bots.paper.trader import DataError, Trader, decide_graduation, validate_candles
from bots.risk.guard import ReadOnlyExchange
from bots.risk.state import ENTRY_HALT, HEARTBEAT
from bots.tests.riskkit import DAY, add_bot, live_cfg, make_guard

H = 3_600_000
Q = 900_000


class FakeMarket:
    """Public market data: closed candles up to now, plus the still-forming one."""

    def __init__(self, clock, price=50_000.0):
        self.clock = clock
        self.price = price
        self.low = None
        self.stale = False
        self.fail = False

    def fetch_ohlcv(self, pair, tf, limit=720):
        if self.fail:
            raise ConnectionError("network down")
        step = {"15m": Q, "1h": H, "4h": 4 * H, "1d": 24 * H}[tf]
        end = self.clock() // step * step - (10 * step if self.stale else 0)
        ts = end - step * np.arange(limit, -1, -1)  # last one is still forming
        p = self.price
        low = self.low if self.low is not None else p * 0.999
        return [[int(t), p, p * 1.001, low, p, 1.0] for t in ts]

    def fetch_ticker(self, pair):
        if self.fail:
            raise ConnectionError("network down")
        return {"last": self.price}


@pytest.fixture
def setup(cfg, conn, tmp_path, monkeypatch):
    cfg = live_cfg(cfg)
    guard, paper, kraken, alerts, clock = make_guard(cfg, conn, tmp_path)
    md = FakeMarket(clock)
    trader = Trader(cfg, conn, guard, ReadOnlyExchange(md), paper, alerts)
    signals = {"entry": False, "exit": False}

    def fake_signals(s, g, ctx=None):
        e = np.zeros(len(s), bool)
        x = np.zeros(len(s), bool)
        e[-1], x[-1] = signals["entry"], signals["exit"]
        return e, x

    monkeypatch.setattr(trader_mod, "compute_signals", fake_signals)
    return cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals


def test_validate_candles():
    now = 100 * H
    good = [[t * H, 1, 2, 0.5, 1.5, 1] for t in range(90, 100)]
    assert len(validate_candles(good + [[100 * H, 1, 1, 1, 1, 1]], "1h", now, 3)) == 10
    with pytest.raises(DataError, match="stale"):
        validate_candles([[t * H, 1, 2, 0.5, 1.5, 1] for t in range(80, 90)], "1h", now, 3)
    with pytest.raises(DataError):
        validate_candles([[90 * H, 1, 2, 0.5, float("nan"), 1], [91 * H, 1, 2, 0.5, 1, 1]],
                         "1h", 92 * H, 3)
    with pytest.raises(DataError, match="high/low"):
        validate_candles([[90 * H, 1, 0.5, 0.4, 1, 1], [91 * H, 1, 2, 0.5, 1, 1]], "1h", 92 * H, 3)
    with pytest.raises(DataError, match="order"):
        validate_candles([[91 * H, 1, 2, 0.5, 1, 1], [90 * H, 1, 2, 0.5, 1, 1]], "1h", 92 * H, 3)


def test_deploys_candidates_and_trades_through_the_guard(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    g = add_bot(conn, "x", "retired")  # just to get a genome json
    conn.execute("INSERT INTO candidates VALUES ('h1','r',?,1.0,'{}','{}',1,'awaiting_paper','t')",
                 (g.to_json(),))
    conn.commit()
    assert trader.deploy_candidates() == 1
    bot_id = conn.execute("SELECT bot_id FROM deployed_bots WHERE status='paper'").fetchone()[0]
    signals["entry"] = True
    report = trader.tick()
    assert report.entries == 1, report.errors
    acc = guard.ledger.account(bot_id, "paper")
    assert acc["qty"] > 0 and paper.orders[acc["stop_order_id"]]["status"] == "open"
    assert guard.state.is_set(HEARTBEAT)
    # Same candle again: no double entry.
    assert trader.tick().entries == 0
    # Next candle trades through the stop.
    clock.t += H
    signals["entry"] = False
    md.price = 48_000
    md.low = 47_000
    trader.tick()
    acc = guard.ledger.account(bot_id, "paper")
    assert acc["qty"] == 0 and acc["trades"] == 1
    fills = conn.execute("SELECT side, reason FROM fills ORDER BY id").fetchall()
    assert [tuple(f) for f in fills] == [("buy", "ema_cross entry"), ("sell", "stop")]


def test_exit_signal_and_target(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    add_bot(conn, "b1", "paper", take_profit_pct=4.0)
    signals["entry"] = True
    trader.tick()
    clock.t += H
    signals.update(entry=False, exit=True)
    trader.tick()
    assert conn.execute("SELECT reason FROM fills WHERE side='sell'").fetchone()[0] == "signal"
    clock.t += H
    signals.update(entry=True, exit=False)
    trader.tick()
    md.price = 53_000  # above the 4% target
    trader.tick()
    reasons = [r[0] for r in conn.execute("SELECT reason FROM fills WHERE side='sell'")]
    assert reasons == ["signal", "target"]


def test_bad_data_stops_new_entries(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    add_bot(conn, "b1", "paper")
    md.stale = True
    signals["entry"] = True
    report = trader.tick()
    assert not report.healthy and guard.state.is_set(ENTRY_HALT)
    assert guard.ledger.account("b1", "paper")["qty"] == 0
    md.stale = False
    md.fail = True
    trader.tick()
    assert guard.state.is_set(ENTRY_HALT)
    md.fail = False
    for _ in range(cfg["worker"]["healthy_ticks_to_resume"]):
        clock.t += H
        trader.tick()
    assert not guard.state.is_set(ENTRY_HALT)
    clock.t += H
    trader.tick()
    assert guard.ledger.account("b1", "paper")["qty"] > 0


def _make_graduate(conn, guard, clock, bot_id, equity=26.0, trades=25, dd=0.05, hold_price=None):
    acc = guard.ledger.account(bot_id, "paper")
    conn.execute("UPDATE accounts SET started_at=?, trades=?, cash=?, last_equity=?, "
                 "max_drawdown=?, peak_equity=?, hold_start_price=? WHERE account_id=?",
                 (clock.t - 31 * DAY, trades, equity, equity, dd, equity,
                  hold_price or 50_000, acc["account_id"]))
    conn.commit()


def test_graduation_gate_needs_every_condition(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    add_bot(conn, "b1", "paper")
    trader.tick()
    acc = guard.ledger.account("b1", "paper")
    ok, missing = trader.graduation_check(acc, 50_000)
    assert not ok and any("paper days" in m for m in missing)
    for kw, word in ((dict(trades=5), "trades"), (dict(dd=0.12), "drawdown"),
                     (dict(equity=19.0), "profitable"), (dict(hold_price=30_000), "holding")):
        _make_graduate(conn, guard, clock, "b1", **kw)
        ok, missing = trader.graduation_check(guard.ledger.account("b1", "paper"), 50_000)
        assert not ok and any(word in m for m in missing), (kw, missing)
    _make_graduate(conn, guard, clock, "b1")
    assert trader.graduation_check(guard.ledger.account("b1", "paper"), 50_000)[0]


def test_graduation_needs_owner_approval_then_goes_live(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    add_bot(conn, "b1", "paper")
    trader.tick()
    _make_graduate(conn, guard, clock, "b1")
    trader.tick()
    assert conn.execute("SELECT status FROM deployed_bots").fetchone()[0] == "ready_to_graduate"
    assert "graduation" in alerts.kinds()
    trader.tick()  # not live without approval
    assert guard.ledger.account("b1", "live") is None
    assert "approved" in decide_graduation(conn, "b1", True, "test")
    trader.tick()
    assert conn.execute("SELECT status FROM deployed_bots").fetchone()[0] == "live"
    assert guard.ledger.account("b1", "live")["allocation"] == 20
    assert guard.ledger.account("b1", "paper") is None
    assert "not waiting" in decide_graduation(conn, "b1", True, "test")


def test_approved_bot_stays_paper_while_live_is_locked(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    guard.env["MODE"] = "PAPER"
    add_bot(conn, "b1", "approved")
    trader.tick()
    assert guard.ledger.account("b1", "live") is None
    assert conn.execute("SELECT status FROM deployed_bots").fetchone()[0] == "approved"


def test_live_bot_breaking_limits_returns_to_paper(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    add_bot(conn, "b1", "live")
    md.price = 50_000
    trader.tick()  # sets prices
    guard.open_live_account("b1", 50_000)
    conn.execute("UPDATE accounts SET max_drawdown=0.15 WHERE mode='live'")
    conn.commit()
    trader.tick()
    assert conn.execute("SELECT status FROM deployed_bots").fetchone()[0] == "paper"
    trader.tick()
    assert guard.ledger.account("b1", "paper") is not None  # back to fake money


def test_paper_bot_retires_after_big_drawdown(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    add_bot(conn, "b1", "paper")
    trader.tick()
    conn.execute("UPDATE accounts SET max_drawdown=0.3")
    conn.commit()
    trader.tick()
    assert conn.execute("SELECT status FROM deployed_bots").fetchone()[0] == "retired"
    assert conn.execute("SELECT COUNT(*) FROM graveyard WHERE run_id='paper'").fetchone()[0] == 1


def test_paper_stops_restored_after_restart(setup):
    cfg, conn, guard, paper, kraken, alerts, clock, md, trader, signals = setup
    add_bot(conn, "b1", "paper")
    signals["entry"] = True
    trader.tick()
    old_stop = guard.ledger.account("b1", "paper")["stop_order_id"]
    new_paper = PaperExchange(0.004, 0.0005, clock)  # memory lost on restart
    guard._paper = new_paper
    trader.paper = new_paper
    trader.restore_paper_stops()
    trader.tick()
    acc = guard.ledger.account("b1", "paper")
    assert acc["qty"] > 0 and acc["stop_order_id"] != old_stop
    assert new_paper.orders[acc["stop_order_id"]]["stopLossPrice"] == pytest.approx(acc["stop_price"])
