"""Shared fakes for risk, trader and worker tests."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from bots.paper.exchange import PaperExchange
from bots.risk.guard import RiskGuard
from bots.tests.test_strategy import base_genome

DAY = 86_400_000
T0 = 1_760_000_000_000 // DAY * DAY + 3_600_000  # 01:00 UTC on some day


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeKraken(PaperExchange):
    """A live exchange double: ccxt-shaped, with failure switches."""

    id = "kraken"

    def __init__(self, clock):
        super().__init__(fee=0.004, slippage=0.0005, clock=clock)
        self.free_usd = 1000.0
        self.fail_stop = False
        self.can_withdraw = False
        self.cancel_all_calls: list = []

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        if params and "stopLossPrice" in params and self.fail_stop:
            raise RuntimeError("EOrder:Unknown order")
        return super().create_order(symbol, type, side, amount, price, params)

    def fetch_balance(self):
        return {"free": {"USD": self.free_usd}}

    def market(self, pair):
        return {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 0.5}}}

    def amount_to_precision(self, pair, amount):
        return f"{math.floor(amount * 1e8) / 1e8:.8f}"  # ccxt truncates by default

    def cancel_all_orders(self, symbol=None):
        self.cancel_all_calls.append(symbol)
        return super().cancel_all_orders(symbol)

    def privatePostWithdrawMethods(self, params):
        if self.can_withdraw:
            return {"result": [{"method": "Bank"}]}
        raise Exception("kraken {\"error\":[\"EGeneral:Permission denied\"]}")


class Alerts:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def alert(self, text, kind="info", buttons=None):
        self.sent.append((kind, text))

    def kinds(self):
        return [k for k, _ in self.sent]


def live_cfg(cfg, capital=60):
    cfg["live_enabled"] = True
    cfg["risk"].update(max_total_capital_usd=capital, max_per_bot_usd=20, max_live_bots=3,
                       daily_loss_limit_usd=3, total_drawdown_kill_usd=10)
    return cfg


def add_bot(conn, bot_id, status="paper", **genes):
    g = base_genome(**genes)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO deployed_bots (bot_id, genome_json, genome_hash, status, "
                 "status_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                 (bot_id, g.to_json(), g.hash(), status, "", now, now))
    conn.commit()
    return g


def make_guard(cfg, conn, tmp_path, live=True, mode_env="LIVE", confirm="60", clock=None):
    # Risk-guard lifecycle tests require a filled position to exercise stop,
    # loss and kill-switch behavior. Limit-specific tests explicitly set LIMIT.
    cfg["execution"]["entry_order"] = "market"
    clock = clock or Clock()
    alerts = Alerts()
    paper = PaperExchange(0.004, 0.0005, clock)
    kraken = FakeKraken(clock) if live else None
    confirm_path = tmp_path / "LIVE_CONFIRMED.txt"
    if confirm is not None:
        confirm_path.write_text(confirm)
    guard = RiskGuard(cfg, conn, paper, kraken, alerts, env={"MODE": mode_env},
                      confirm_path=confirm_path, clock=clock, sleep=lambda s: None)
    return guard, paper, kraken, alerts, clock


def set_price(guard, price, pair="BTC/USD", low=None):
    for ex in (guard._paper, guard._live):
        if ex is not None:
            ex.update_price(pair, price, low=low)
    guard.update_marks({pair: price})
