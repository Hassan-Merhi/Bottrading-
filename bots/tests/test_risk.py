"""Section 6.8: the required proofs that the risk rules hold."""
import ast
import re
from pathlib import Path

import pytest

from bots.risk.guard import (HARD_CAPITAL_CEILING_USD, OrderRefused, OrderRequest,
                             ReadOnlyExchange, risk_config_problems)
from bots.risk.state import CRASH_LOCK, DAILY_HALT_UNTIL, ENTRY_HALT, KILL_SWITCH
from bots.tests.riskkit import DAY, add_bot, live_cfg, make_guard, set_price

PACKAGE = Path(__file__).resolve().parents[1]
# ccxt order, cancel, withdrawal and transfer methods (unified and raw Kraken endpoints).
ORDER_METHOD = re.compile(
    r"^(create|cancel|edit)_?\w*orders?$|^withdraw$|^transfer$|"
    r"^private(post|get)(addorder\w*|cancel\w*|editorder|withdraw\w*|wallettransfer)$",
    re.IGNORECASE)
# The only places allowed to make such calls, and why.
ALLOWED = {
    ("risk/guard.py", "*"),                                  # the single gate
    ("live/exchange.py", "privatePostWithdrawMethods"),      # read-only permission probe
}


def _order_calls(tree, rel):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            # The paper exchange simulating itself is not calling an exchange.
            if rel == "paper/exchange.py" and isinstance(f.value, ast.Name) and f.value.id == "self":
                continue
            name = f.attr
        elif isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) > 1 \
                and isinstance(node.args[1], ast.Constant):
            name = str(node.args[1].value)  # getattr(exchange, "create_order")
        else:
            continue
        if ORDER_METHOD.match(name):
            yield name, node.lineno


# -- 6.1 / 6.8: orders bypassing RiskGuard are impossible -------------------

def test_no_module_but_riskguard_calls_order_or_withdraw_methods():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("tests/"):
            continue
        for name, line in _order_calls(ast.parse(path.read_text()), rel):
            if (rel, "*") not in ALLOWED and (rel, name) not in ALLOWED:
                offenders.append(f"{rel}:{line} calls {name}")
    assert not offenders, "order calls outside RiskGuard:\n" + "\n".join(offenders)


def test_scanner_catches_order_calls():
    sneaky = ast.parse("ex.create_order('BTC/USD','market','buy',1)\n"
                       "ex.createMarketBuyOrder('BTC/USD', 1)\n"
                       "getattr(ex, 'create_order')\n"
                       "ex.privatePostAddOrder({})\nex.withdraw('BTC', 1, 'addr')\n"
                       "ex.cancel_all_orders()\nOrderedDict()\nex.fetch_order('1')\n")
    found = [n for n, _ in _order_calls(sneaky, "somewhere.py")]
    assert found == ["create_order", "createMarketBuyOrder", "create_order",
                     "privatePostAddOrder", "withdraw", "cancel_all_orders"]


def test_only_data_and_live_modules_import_ccxt():
    importers = []
    for path in PACKAGE.rglob("*.py"):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("tests/"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] == "ccxt" for n in names):
                importers.append(rel)
    assert set(importers) <= {"data/fetch.py", "live/exchange.py"}, importers


def test_guard_check_runs_before_any_exchange_call(cfg, conn, tmp_path):
    guard, paper, kraken, alerts, _ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1")
    guard.open_paper_account("b1", 50_000)
    set_price(guard, 50_000)
    with pytest.raises(OrderRefused):
        guard.place_order(OrderRequest("b1", "paper", "BTC/USD", "buy", 1.0, ref_price=50_000))
    assert paper.orders == {}  # nothing reached the exchange
    assert conn.execute("SELECT COUNT(*) FROM risk_events WHERE kind='order_refused'").fetchone()[0]
    assert alerts.sent  # refusal alerted


def test_read_only_exchange_cannot_trade():
    class Ex:
        def create_order(self, *a):
            raise AssertionError("must not be reachable")

        def fetch_ohlcv(self, *a, **k):
            return [1]

    ro = ReadOnlyExchange(Ex())
    assert ro.fetch_ohlcv("BTC/USD") == [1]
    for name in ("create_order", "cancel_order", "cancel_all_orders", "edit_order", "withdraw",
                 "privatePostAddOrder", "transfer"):
        with pytest.raises(PermissionError):
            getattr(ro, name)
    with pytest.raises(PermissionError):
        ro._exchange = None  # noqa: SLF001


# -- 6.2 / 6.8: LIVE cannot activate with any condition missing ------------

def test_live_when_all_three_conditions_hold(cfg, conn, tmp_path):
    guard, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    d = guard.mode()
    assert d.mode == "LIVE", d.reasons


@pytest.mark.parametrize("breaker", [
    "no_env", "env_paper", "env_garbage", "live_disabled", "live_enabled_string", "no_file",
    "file_mismatch", "file_garbage", "capital_zero", "capital_over_ceiling", "per_bot_over_total",
    "missing_risk_key", "kill_switch", "crash_lock", "no_live_exchange",
])
def test_live_refused_when_any_condition_is_missing(cfg, conn, tmp_path, breaker):
    cfg = live_cfg(cfg)
    kw = {}
    if breaker == "no_env":
        kw["mode_env"] = ""
    elif breaker == "env_paper":
        kw["mode_env"] = "PAPER"
    elif breaker == "env_garbage":
        kw["mode_env"] = "LIVE!!"
    elif breaker == "live_disabled":
        cfg["live_enabled"] = False
    elif breaker == "live_enabled_string":
        cfg["live_enabled"] = "true"
    elif breaker == "no_file":
        kw["confirm"] = None
    elif breaker == "file_mismatch":
        kw["confirm"] = "100"
    elif breaker == "file_garbage":
        kw["confirm"] = "yes please"
    elif breaker == "capital_zero":
        cfg["risk"]["max_total_capital_usd"] = 0
        kw["confirm"] = "0"
    elif breaker == "capital_over_ceiling":
        cfg["risk"]["max_total_capital_usd"] = 500
        kw["confirm"] = "500"
    elif breaker == "per_bot_over_total":
        cfg["risk"]["max_per_bot_usd"] = 80
    elif breaker == "missing_risk_key":
        del cfg["risk"]["daily_loss_limit_usd"]
    elif breaker == "no_live_exchange":
        kw["live"] = False
    guard, *_ = make_guard(cfg, conn, tmp_path, **kw)
    if breaker == "kill_switch":
        guard.state.set(KILL_SWITCH, "test")
    if breaker == "crash_lock":
        guard.state.set(CRASH_LOCK, "test")
    d = guard.mode()
    assert d.mode == "PAPER" and d.reasons


def test_default_config_is_paper_only(cfg):
    assert cfg["live_enabled"] is False
    assert cfg["risk"]["max_total_capital_usd"] == 0
    assert not risk_config_problems(cfg)
    assert HARD_CAPITAL_CEILING_USD == 100


def test_live_buy_refused_in_paper_mode_but_exit_allowed(cfg, conn, tmp_path):
    guard, paper, kraken, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    guard.open_live_account("b1", 50_000)
    guard.enter_long("b1", "live", "BTC/USD", 50_000, 100, 5, 10, "test")
    guard.env["MODE"] = "PAPER"  # owner switches off live
    with pytest.raises(OrderRefused):
        guard.place_order(OrderRequest("b1", "live", "BTC/USD", "buy", 0.0001, ref_price=50_000))
    assert guard.exit_long("b1", "live", "owner") is not None  # reducing risk still works
    assert guard.ledger.account("b1", "live")["qty"] == 0


# -- 6.4 / 6.8: leverage, shorting and other forbidden orders --------------

@pytest.mark.parametrize("change", [
    {"params": {"leverage": 2}}, {"params": {"marginMode": "cross"}},
    {"params": {"reduceOnly": True}}, {"params": {"stopLossPrice": 1}},
    {"side": "short"}, {"order_type": "limit"}, {"pair": "DOGE/USD"}, {"amount": -1},
    {"amount": float("nan")}, {"mode": "futures"},
])
def test_forbidden_orders_are_refused(cfg, conn, tmp_path, change):
    guard, paper, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1")
    guard.open_paper_account("b1", 50_000)
    set_price(guard, 50_000)
    base = dict(bot_id="b1", mode="paper", pair="BTC/USD", side="buy", amount=0.0001,
                ref_price=50_000)
    base.update(change)
    with pytest.raises(OrderRefused):
        guard.place_order(OrderRequest(**base))
    assert paper.orders == {}


def test_selling_more_than_held_is_shorting_and_refused(cfg, conn, tmp_path):
    guard, paper, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1")
    guard.open_paper_account("b1", 50_000)
    set_price(guard, 50_000)
    with pytest.raises(OrderRefused, match="shorting"):
        guard.place_order(OrderRequest("b1", "paper", "BTC/USD", "sell", 0.001))
    guard.enter_long("b1", "paper", "BTC/USD", 50_000, 50, 5, 10, "t")
    held = guard.ledger.account("b1", "paper")["qty"]
    with pytest.raises(OrderRefused, match="shorting"):
        guard.place_order(OrderRequest("b1", "paper", "BTC/USD", "sell", held * 2))


def test_one_position_per_bot(cfg, conn, tmp_path):
    guard, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1")
    guard.open_paper_account("b1", 50_000)
    set_price(guard, 50_000)
    guard.enter_long("b1", "paper", "BTC/USD", 50_000, 50, 5, 10, "t")
    with pytest.raises(OrderRefused, match="one per bot"):
        guard.enter_long("b1", "paper", "BTC/USD", 50_000, 50, 5, 10, "t")


def test_stop_loss_is_placed_right_after_entry(cfg, conn, tmp_path):
    guard, paper, kraken, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    guard.open_live_account("b1", 50_000)
    guard.enter_long("b1", "live", "BTC/USD", 50_000, 100, 2, 10, "t")
    acc = guard.ledger.account("b1", "live")
    stop = kraken.orders[acc["stop_order_id"]]
    assert stop["status"] == "open" and stop["amount"] == pytest.approx(acc["qty"])
    assert stop["stopLossPrice"] == pytest.approx(acc["entry_price"] * 0.98)
    # The stop fills on the exchange; the guard records it.
    set_price(guard, 48_000, low=48_000)
    assert guard.sync_stop("b1", "live") < 0
    assert guard.ledger.account("b1", "live")["qty"] == 0


def test_position_closed_at_once_if_stop_cannot_be_placed(cfg, conn, tmp_path):
    guard, paper, kraken, alerts, _ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    guard.open_live_account("b1", 50_000)
    kraken.fail_stop = True
    guard.enter_long("b1", "live", "BTC/USD", 50_000, 100, 2, 10, "t")
    assert guard.ledger.account("b1", "live")["qty"] == 0
    sides = [o["side"] for o in kraken.orders.values() if o["status"] == "closed"]
    assert sides == ["buy", "sell"]
    assert "critical" in alerts.kinds()


def test_stop_cancelled_on_exchange_closes_position(cfg, conn, tmp_path):
    guard, paper, kraken, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    guard.open_live_account("b1", 50_000)
    guard.enter_long("b1", "live", "BTC/USD", 50_000, 100, 2, 10, "t")
    kraken.cancel_order(guard.ledger.account("b1", "live")["stop_order_id"])  # someone cancelled it
    guard.sync_stop("b1", "live")
    assert guard.ledger.account("b1", "live")["qty"] == 0


# -- 6.3 / 6.8: each money limit stops trading -----------------------------

def _three_live_bots(guard, conn, stop_pct=10):
    set_price(guard, 50_000)
    for i in range(3):
        add_bot(conn, f"b{i}", "live")
        assert guard.open_live_account(f"b{i}", 50_000) is not None
        guard.enter_long(f"b{i}", "live", "BTC/USD", 50_000, 100, stop_pct, 50, "t")


def test_max_live_bots_and_total_capital(cfg, conn, tmp_path):
    guard, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    _three_live_bots(guard, conn)
    add_bot(conn, "b9", "live")
    assert guard.open_live_account("b9", 50_000) is None  # max_live_bots = 3
    assert sum(a["allocation"] for a in guard.ledger.active("live")) <= 60


def test_capital_cap_limits_allocations(cfg, conn, tmp_path):
    cfg = live_cfg(cfg, capital=30)
    guard, *_ = make_guard(cfg, conn, tmp_path, confirm="30")
    set_price(guard, 50_000)
    for i in range(3):
        add_bot(conn, f"b{i}", "live")
    allocs = [guard.open_live_account(f"b{i}", 50_000) for i in range(3)]
    live = guard.ledger.active("live")
    assert sum(a["allocation"] for a in live) <= 30
    assert [a["allocation"] for a in live] == [20, 10]
    assert allocs[2] is None


def test_per_bot_cap_and_cash_check(cfg, conn, tmp_path):
    guard, paper, kraken, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    guard.open_live_account("b1", 50_000)
    assert guard.ledger.account("b1", "live")["allocation"] == 20
    with pytest.raises(OrderRefused, match="tradable cash"):
        guard.place_order(OrderRequest("b1", "live", "BTC/USD", "buy", 0.001, ref_price=50_000))
    kraken.free_usd = 3.0  # the real exchange balance is the last word
    with pytest.raises(OrderRefused, match="free USD"):
        guard.enter_long("b1", "live", "BTC/USD", 50_000, 100, 5, 10, "t")


def test_orders_below_exchange_minimum_refused(cfg, conn, tmp_path):
    guard, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1")
    guard.open_paper_account("b1", 50_000)
    set_price(guard, 50_000)
    with pytest.raises(OrderRefused, match="minimum"):
        guard.enter_long("b1", "paper", "BTC/USD", 50_000, 10, 5, 10, "t")  # $2 order


def test_daily_loss_limit_closes_and_pauses_until_next_day(cfg, conn, tmp_path):
    guard, paper, kraken, alerts, clock = make_guard(live_cfg(cfg), conn, tmp_path)
    _three_live_bots(guard, conn)
    set_price(guard, 47_000)  # -6% on $60 = -$3.6, stops at -10% not hit
    guard.check_limits()
    assert all(a["qty"] == 0 for a in guard.ledger.active("live"))
    assert int(guard.state.get(DAILY_HALT_UNTIL)) == (clock.t // DAY + 1) * DAY
    assert not guard.state.is_set(KILL_SWITCH)
    with pytest.raises(OrderRefused, match="daily loss"):
        guard.enter_long("b0", "live", "BTC/USD", 47_000, 100, 10, 50, "t")
    clock.t += DAY  # next UTC day
    for a in guard.ledger.active("live"):
        guard.ledger.mark(a["account_id"], 47_000, clock.t)
    guard.enter_long("b0", "live", "BTC/USD", 47_000, 100, 10, 50, "t")
    assert guard.ledger.account("b0", "live")["qty"] > 0


def test_kill_switch_closes_everything_and_stays_off(cfg, conn, tmp_path):
    guard, paper, kraken, alerts, clock = make_guard(live_cfg(cfg), conn, tmp_path)
    _three_live_bots(guard, conn, stop_pct=50)
    set_price(guard, 40_000)  # -20% on $60 = -$12, beyond the $10 kill level
    guard.check_limits()
    assert guard.state.is_set(KILL_SWITCH)
    assert guard.ledger.active("live") == []  # all live accounts closed
    assert all(o["status"] != "open" for o in kraken.orders.values())  # no resting orders
    assert kraken.cancel_all_calls  # cancel-all sent for every pair
    assert not guard.confirm_path.exists()  # confirmation revoked
    assert guard.mode().mode == "PAPER"
    statuses = {r[0] for r in conn.execute("SELECT status FROM deployed_bots")}
    assert statuses == {"paper"}
    assert "critical" in alerts.kinds()
    # Time passing and prices recovering do not restart live trading.
    clock.t += 10 * DAY
    set_price(guard, 60_000)
    guard.check_limits()
    assert guard.mode().mode == "PAPER"
    # Even with a fresh confirmation file the kill switch holds until the owner clears it.
    guard.confirm_path.write_text("60")
    assert guard.mode().mode == "PAPER"
    add_bot(conn, "new", "live")
    assert guard.open_live_account("new", 60_000) is None


def test_confirm_live_command_clears_kill_only_with_fresh_file(cfg, conn, tmp_path, monkeypatch):
    from bots import main as main_mod
    from bots.risk.state import LIVE_EPOCH, RiskState

    cfg = live_cfg(cfg)
    monkeypatch.setenv("BOTS_STATE_DIR", str(tmp_path))
    state = RiskState(conn)
    state.set(KILL_SWITCH, "test")
    with pytest.raises(SystemExit):
        main_mod.cmd_confirm_live(cfg, conn, None)
    assert state.is_set(KILL_SWITCH)
    (tmp_path / "LIVE_CONFIRMED.txt").write_text("60")
    main_mod.cmd_confirm_live(cfg, conn, None)
    assert not state.is_set(KILL_SWITCH)
    assert state.get(LIVE_EPOCH) == "2"  # losses counted afresh only after owner confirmation


# -- 6.5 house money -------------------------------------------------------

def test_house_money_protects_original_stake(cfg, conn, tmp_path):
    guard, paper, kraken, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    guard.open_live_account("b1", 50_000)
    guard.enter_long("b1", "live", "BTC/USD", 50_000, 100, 5, 500, "t")
    set_price(guard, 110_000)
    guard.exit_long("b1", "live", "take profit")
    acc = guard.ledger.account("b1", "live")
    assert acc["cash"] >= 40 and acc["protected"] == 20
    assert guard.ledger.tradable_cash(acc) == pytest.approx(acc["cash"] - 20)
    guard.enter_long("b1", "live", "BTC/USD", 110_000, 100, 5, 500, "t")
    acc = guard.ledger.account("b1", "live")
    assert acc["cash"] >= 20 - 1e-9  # the protected stake was not spent


def test_paper_accounts_have_no_house_money_lock(cfg, conn, tmp_path):
    guard, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1")
    set_price(guard, 50_000)
    guard.open_paper_account("b1", 50_000)
    guard.enter_long("b1", "paper", "BTC/USD", 50_000, 100, 5, 500, "t")
    set_price(guard, 110_000)
    guard.exit_long("b1", "paper", "tp")
    assert guard.ledger.account("b1", "paper")["protected"] == 0


# -- 6.7 fail-safes --------------------------------------------------------

def test_exchange_error_halts_entries_until_healthy(cfg, conn, tmp_path):
    guard, paper, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1")
    guard.open_paper_account("b1", 50_000)
    # No price on the paper exchange yet: the order errors at the exchange.
    from bots.risk.guard import OrderFailed

    with pytest.raises(OrderFailed):
        guard.enter_long("b1", "paper", "BTC/USD", 50_000, 50, 5, 10, "t")
    assert guard.state.is_set(ENTRY_HALT)
    set_price(guard, 50_000)
    with pytest.raises(OrderRefused, match="halted"):
        guard.enter_long("b1", "paper", "BTC/USD", 50_000, 50, 5, 10, "t")
    for _ in range(3):
        guard.note_healthy(3)
    assert not guard.state.is_set(ENTRY_HALT)
    guard.enter_long("b1", "paper", "BTC/USD", 50_000, 50, 5, 10, "t")


def test_withdrawal_permission_check():
    from bots.live.exchange import WithdrawalPermissionError, check_cannot_withdraw
    from bots.tests.riskkit import Clock, FakeKraken

    ex = FakeKraken(Clock())
    check_cannot_withdraw(ex)  # permission denied = safe
    ex.can_withdraw = True
    with pytest.raises(WithdrawalPermissionError):
        check_cannot_withdraw(ex)

    class Broken:
        def privatePostWithdrawMethods(self, p):
            raise TimeoutError("network down")

    with pytest.raises(WithdrawalPermissionError):  # inconclusive also refuses
        check_cannot_withdraw(Broken())


def test_demoted_live_bot_returns_to_paper_without_more_money(cfg, conn, tmp_path):
    guard, *_ = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    guard.open_live_account("b1", 50_000)
    guard.enter_long("b1", "live", "BTC/USD", 50_000, 100, 20, 50, "t")
    guard.demote("b1", "broke its limits")
    assert guard.ledger.account("b1", "live") is None
    assert conn.execute("SELECT status FROM deployed_bots").fetchone()[0] == "paper"
