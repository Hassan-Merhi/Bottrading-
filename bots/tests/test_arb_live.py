"""Live arbitrage: locked by default, every gate fails closed, legs reconcile or halt."""
from __future__ import annotations

import pytest

from bots.arb import live
from bots.arb.paper import tick as paper_tick
from bots.arb.scanner import scan
from bots.db import connect
from bots.risk.state import KILL_SWITCH, OWNER_PAUSE, RiskState
from bots.tests.test_arb import FakeVenue, book

NOW = 1_800_000_000_000
DAY = 86_400_000


def cfg(**live_over):
    c = {"data": {"pairs": ["BTC/USD"]},
         "backtest": {"min_order": {"BTC/USD": {"min_qty": 0.0001, "min_cost_usd": 5}}},
         "arbitrage": {"enabled": True, "size_usd": 50, "inventory_multiple": 3,
                       "min_net_edge_pct": 0.10, "slippage_pct": 0.05, "scan_seconds": 120,
                       "pairs": ["BTC/USD"],
                       "venues": {"kraken": {"taker_fee_pct": 0.10, "quote": "USD"},
                                  "coinbaseexchange": {"taker_fee_pct": 0.10, "quote": "USD"}},
                       "live": {"enabled": True, "max_total_capital_usd": 300, "per_trade_usd": 50,
                                "min_net_edge_pct": 0.20, "max_trades_per_day": 5,
                                "max_daily_loss_usd": 2, "max_total_loss_usd": 5,
                                "min_paper_days": 1, "min_paper_trades": 2}}}
    c["arbitrage"]["live"].update(live_over)
    return c


class Alerts:
    def __init__(self):
        self.msgs = []

    def alert(self, text, kind="info", buttons=None):
        self.msgs.append((kind, text))


class FakeLiveVenue:
    """ccxt-shaped authenticated venue: IOC limit orders fill fully, partially or not at all."""

    def __init__(self, usd=1000.0, btc=1.0, fill_ratio=1.0, fail=False):
        self.balance = {"free": {"USD": usd, "BTC": btc}}
        self.fill_ratio, self.fail, self.orders, self.n = fill_ratio, fail, {}, 0

    def fetch_balance(self):
        return self.balance

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        assert type == "limit" and params == {"timeInForce": "IOC"}
        if self.fail:
            raise RuntimeError("EOrder:Insufficient funds")
        self.n += 1
        filled = amount * self.fill_ratio
        fee = filled * price * 0.001
        o = {"id": f"o{self.n}", "filled": filled, "average": price if filled else None,
             "fee": {"cost": fee, "currency": "USD"}, "status": "closed"}
        if side == "buy":
            self.balance["free"]["USD"] -= filled * price + fee
            self.balance["free"]["BTC"] += filled
        else:
            self.balance["free"]["USD"] += filled * price - fee
            self.balance["free"]["BTC"] -= filled
        self.orders[o["id"]] = o
        return o

    def fetch_order(self, oid, symbol=None):
        return self.orders[oid]


def armed(tmp_path, monkeypatch, c=None, days=2, paper_trades=3):
    """A DB with enough paper evidence, a confirm file and a LIVE env: every gate open."""
    c = c or cfg()
    conn = connect(":memory:")
    env = {"MODE": "LIVE", "BOTS_STATE_DIR": str(tmp_path)}
    (tmp_path / live.CONFIRM_FILE).write_text(str(c["arbitrage"]["live"]["max_total_capital_usd"]))
    # Paper evidence: a 1% gap at 0.1% fees produces one paper trade per scan.
    books = {"kraken": FakeVenue({"BTC/USD": book(50_000, size=5)}),
             "coinbaseexchange": FakeVenue({"BTC/USD": book(50_500, size=5)})}
    start = NOW - days * DAY
    for k in range(paper_trades):
        t = start + k * 3_600_000
        r = scan(conn, c, books, t)
        paper_tick(conn, c, r["edges"], t)
    r = scan(conn, c, books, NOW)        # the fresh scan live trades on
    return conn, env, r["edges"]


def test_default_config_is_locked_everywhere():
    from bots.config import load_config
    c = load_config()
    conn = connect(":memory:")
    reasons = live.readiness(conn, c, {}, RiskState(conn), NOW, clients_ready=False)
    joined = " ".join(reasons)
    for needle in ("live.enabled is not true", "MODE env is not LIVE", "max_total_capital_usd",
                   live.CONFIRM_FILE, "no authenticated venue connections", "no paper arbitrage evidence"):
        assert needle in joined, needle
    assert c["arbitrage"]["size_usd"] == 50 == c["arbitrage"]["live"]["per_trade_usd"]


def test_each_gate_blocks_on_its_own(tmp_path, monkeypatch):
    conn, env, edges = armed(tmp_path, monkeypatch)
    c = cfg()
    state = RiskState(conn)
    assert live.readiness(conn, c, env, state, NOW, True) == []
    assert live.readiness(conn, c, {**env, "MODE": "PAPER"}, state, NOW, True) == ["MODE env is not LIVE"]
    (tmp_path / live.CONFIRM_FILE).write_text("299")
    assert any("says 299" in r for r in live.readiness(conn, c, env, state, NOW, True))
    (tmp_path / live.CONFIRM_FILE).write_text("300")
    state.set(KILL_SWITCH, "test")
    assert any("kill switch" in r for r in live.readiness(conn, c, env, state, NOW, True))
    state.clear(KILL_SWITCH)
    state.set(OWNER_PAUSE, "1")
    assert any("owner pause" in r for r in live.readiness(conn, c, env, state, NOW, True))
    state.clear(OWNER_PAUSE)
    assert live.readiness(conn, c, env, state, NOW, False) == ["no authenticated venue connections"]
    assert any("needs 10" in r for r in live.readiness(conn, cfg(min_paper_trades=10), env, state, NOW, True))
    assert any("observed days" in r for r in live.readiness(conn, cfg(min_paper_days=30), env, state, NOW, True))
    stale = live.readiness(conn, c, env, state, NOW + DAY, True)
    assert any("too old" in r for r in stale)
    bad = cfg(per_trade_usd=20)
    assert any("size_usd must equal" in r for r in live.readiness(conn, bad, env, state, NOW, True))
    too_big = cfg(max_total_capital_usd=10_000)
    assert any("code ceiling" in r for r in live.readiness(conn, too_big, env, state, NOW, True))


def test_full_fill_is_recorded_and_sized_at_fifty(tmp_path, monkeypatch):
    conn, env, edges = armed(tmp_path, monkeypatch)
    clients = {"kraken": FakeLiveVenue(), "coinbaseexchange": FakeLiveVenue()}
    alerts = Alerts()
    out = live.tick(conn, cfg(), env, RiskState(conn), clients, edges, NOW, alerter=alerts)
    assert out["attempted"] == 1 and out["status"] == "filled", out
    t = conn.execute("SELECT * FROM arb_live_trades").fetchone()
    assert t["status"] == "filled" and t["buy_venue"] == "kraken" and t["sell_venue"] == "coinbaseexchange"
    assert t["buy_filled"] == pytest.approx(t["sell_filled"]) and t["net_usd"] > 0
    assert 49 < t["intended_qty"] * t["buy_avg"] < 51.5          # $50 per leg
    assert clients["kraken"].n == 1 and clients["coinbaseexchange"].n == 1
    assert alerts.msgs[-1][0] == "info" and "net $" in alerts.msgs[-1][1]
    assert not RiskState(conn).is_set(live.ARB_LIVE_HALT)
    snap = live.snapshot(conn, cfg(), env, NOW, True)
    assert snap["trades"] == 1 and snap["ready"] and snap["net_usd"] > 0


def test_unequal_fill_halts_and_alerts(tmp_path, monkeypatch):
    conn, env, edges = armed(tmp_path, monkeypatch)
    clients = {"kraken": FakeLiveVenue(fill_ratio=0.5), "coinbaseexchange": FakeLiveVenue()}
    alerts = Alerts()
    state = RiskState(conn)
    out = live.tick(conn, cfg(), env, state, clients, edges, NOW, alerter=alerts)
    assert out["status"] == "legged"
    assert state.is_set(live.ARB_LIVE_HALT) and "unequal fills" in state.get(live.ARB_LIVE_HALT)
    assert alerts.msgs[-1][0] == "critical"
    # Halted: the next scan does nothing, even with a perfect edge.
    again = live.tick(conn, cfg(), env, state, clients, edges, NOW + 60_000, alerter=alerts)
    assert again["attempted"] == 0 and any("arbitrage halt" in b for b in again["blocked"])
    assert conn.execute("SELECT COUNT(*) FROM arb_live_trades").fetchone()[0] == 1


def test_sell_leg_failure_means_no_buy(tmp_path, monkeypatch):
    conn, env, edges = armed(tmp_path, monkeypatch)
    clients = {"kraken": FakeLiveVenue(), "coinbaseexchange": FakeLiveVenue(fail=True)}
    out = live.tick(conn, cfg(), env, RiskState(conn), clients, edges, NOW, alerter=Alerts())
    assert out["status"] == "error"
    assert clients["kraken"].n == 0                      # buy never attempted
    assert RiskState(conn).is_set(live.ARB_LIVE_HALT)


def test_insufficient_balance_or_small_edge_skips_without_orders(tmp_path, monkeypatch):
    conn, env, edges = armed(tmp_path, monkeypatch)
    poor = {"kraken": FakeLiveVenue(usd=10.0), "coinbaseexchange": FakeLiveVenue()}
    out = live.tick(conn, cfg(), env, RiskState(conn), poor, edges, NOW)
    assert out["attempted"] == 0 and poor["kraken"].n == 0
    no_coin = {"kraken": FakeLiveVenue(), "coinbaseexchange": FakeLiveVenue(btc=0.0)}
    assert live.tick(conn, cfg(), env, RiskState(conn), no_coin, edges, NOW)["attempted"] == 0
    strict = cfg(min_net_edge_pct=5.0)
    rich = {"kraken": FakeLiveVenue(), "coinbaseexchange": FakeLiveVenue()}
    assert live.tick(conn, strict, env, RiskState(conn), rich, edges, NOW)["attempted"] == 0
    assert rich["kraken"].n == 0
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='arb_live_trades'").fetchone() \
        or conn.execute("SELECT COUNT(*) FROM arb_live_trades").fetchone()[0] == 0


def test_daily_trade_and_loss_limits(tmp_path, monkeypatch):
    conn, env, edges = armed(tmp_path, monkeypatch, cfg(max_trades_per_day=2))
    clients = {"kraken": FakeLiveVenue(), "coinbaseexchange": FakeLiveVenue()}
    for k in range(3):
        live.tick(conn, cfg(max_trades_per_day=2), env, RiskState(conn), clients, edges, NOW + k)
    assert conn.execute("SELECT COUNT(*) FROM arb_live_trades").fetchone()[0] == 2
    # A recorded loss beyond the limit halts before any order is placed.
    live.ensure_schema(conn)
    conn.execute("INSERT INTO arb_live_trades(ts,pair,buy_venue,sell_venue,intended_qty,buy_filled,"
                 "sell_filled,fees,net_usd,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (NOW, "BTC/USD", "kraken", "coinbaseexchange", 0.001, 0.001, 0.001, 0.1, -9.0, "filled"))
    conn.commit()
    state = RiskState(conn)
    out = live.tick(conn, cfg(), env, state, clients, edges, NOW + 10)
    assert out["attempted"] == 0 or out.get("status") == "skipped"
    assert state.is_set(live.ARB_LIVE_HALT) and "loss limit" in state.get(live.ARB_LIVE_HALT)


def test_disabled_live_is_a_noop_even_when_armed(tmp_path, monkeypatch):
    conn, env, edges = armed(tmp_path, monkeypatch)
    clients = {"kraken": FakeLiveVenue(), "coinbaseexchange": FakeLiveVenue()}
    out = live.tick(conn, cfg(enabled=False), env, RiskState(conn), clients, edges, NOW)
    assert out == {"enabled": False, "attempted": 0} and clients["kraken"].n == 0


def test_make_live_venues_requires_credentials():
    with pytest.raises(RuntimeError, match="no API credentials for kraken"):
        live.make_live_venues(cfg()["arbitrage"], {})
