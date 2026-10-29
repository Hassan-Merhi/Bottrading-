"""Phase 6: fail-closed exchange reconciliation and emergency execution tests."""
import pytest

from bots.live.reconcile import ReconciliationError, reconcile_live_orders
from bots.risk.guard import OrderFailed, OrderRefused
from bots.risk.state import KILL_SWITCH, LIVE_RECON_HALT
from bots.tests.riskkit import add_bot, live_cfg, make_guard, set_price


def _open_position(cfg, conn, tmp_path):
    guard, paper, kraken, alerts, clock = make_guard(live_cfg(cfg), conn, tmp_path)
    add_bot(conn, "b1", "live")
    set_price(guard, 50_000)
    assert guard.open_live_account("b1", 50_000)
    assert guard.enter_long("b1", "live", "BTC/USD", 50_000, 40, 2, 5, "test")
    return guard, paper, kraken, alerts


def test_reconcile_protected_live_position(cfg, conn, tmp_path):
    guard, _, kraken, _ = _open_position(cfg, conn, tmp_path)
    result = guard.verify_live_orders()
    assert result["active_live_accounts"] == 1
    assert result["open_orders"] == 1
    assert not guard.state.is_set(LIVE_RECON_HALT)


def test_unrecognized_exchange_order_halts_new_live_exposure(cfg, conn, tmp_path):
    guard, _, kraken, alerts = _open_position(cfg, conn, tmp_path)
    kraken.create_order("BTC/USD", "limit", "buy", 0.0002, 49_000)  # manual/unknown
    with pytest.raises(OrderRefused, match="reconciliation"):
        guard.verify_live_orders()
    assert guard.state.is_set(LIVE_RECON_HALT)
    assert guard.mode().mode == "PAPER"
    assert "critical" in alerts.kinds()
    # After the remote orphan is cancelled, reconciliation may succeed, but
    # the sticky lock is NOT reset automatically.
    for order in list(kraken.fetch_open_orders()):
        if order["type"] == "limit":
            kraken.cancel_order(order["id"])
    guard.verify_live_orders()
    assert guard.state.is_set(LIVE_RECON_HALT)


def test_missing_protective_stop_halts_live_entries(cfg, conn, tmp_path):
    guard, _, kraken, _ = _open_position(cfg, conn, tmp_path)
    acc = guard.ledger.account("b1", "live")
    kraken.cancel_order(acc["stop_order_id"])
    with pytest.raises(OrderRefused, match="reconciliation"):
        guard.verify_live_orders()
    assert guard.state.is_set(LIVE_RECON_HALT)


def test_stop_cancel_timeout_cannot_send_a_second_live_sell(cfg, conn, tmp_path):
    guard, _, kraken, _ = _open_position(cfg, conn, tmp_path)
    kraken.fail_cancel = True
    with pytest.raises(OrderFailed, match="cancel"):
        guard.exit_long("b1", "live", "signal")
    acc = guard.ledger.account("b1", "live")
    assert acc["qty"] > 0 and acc["stop_order_id"]
    assert guard.state.is_set(LIVE_RECON_HALT)
    assert [o["side"] for o in kraken.orders.values() if o["status"] == "closed"] == ["buy"]
    assert guard.mode().mode == "PAPER"


def test_kill_preserves_unknown_live_exposure_for_recovery(cfg, conn, tmp_path):
    guard, _, kraken, alerts = _open_position(cfg, conn, tmp_path)
    kraken.fail_cancel = True
    guard.kill("test emergency")
    acc = guard.ledger.account("b1", "live")
    assert acc is not None and acc["qty"] > 0  # never fake a successful liquidation
    assert guard.state.is_set(KILL_SWITCH)
    assert guard.state.is_set(LIVE_RECON_HALT)
    assert not guard.confirm_path.exists()
    assert "critical" in alerts.kinds()


def test_invalid_exchange_response_does_not_create_live_account(cfg, conn, tmp_path):
    guard, _, kraken, _ = _open_position(cfg, conn, tmp_path)
    add_bot(conn, "b2", "live")
    kraken.fetch_open_orders = lambda: {"bad": "format"}
    assert guard.open_live_account("b2", 50_000) is None
    assert guard.ledger.account("b2", "live") is None
    assert guard.state.is_set(LIVE_RECON_HALT)


def test_owner_audit_requires_authenticated_read_only_pass(cfg, conn, tmp_path, monkeypatch):
    from bots import main
    from bots.risk.state import RiskState

    cfg = live_cfg(cfg)
    guard, _, kraken, _ = _open_position(cfg, conn, tmp_path)
    state = RiskState(conn)
    state.set(LIVE_RECON_HALT, "prior mismatch")
    monkeypatch.setenv("KRAKEN_API_KEY", "synthetic")
    monkeypatch.setenv("KRAKEN_API_SECRET", "synthetic")
    monkeypatch.setattr("bots.live.exchange.make_live_exchange", lambda env: kraken)
    args = type("Args", (), {"acknowledge": False})()
    main.cmd_audit_live(cfg, conn, args)
    assert state.is_set(LIVE_RECON_HALT)  # read-only audit does not unlock
    args.acknowledge = True
    main.cmd_audit_live(cfg, conn, args)
    assert not state.is_set(LIVE_RECON_HALT)
    assert guard.mode().mode == "LIVE"
    kraken.can_withdraw = True
    state.set(LIVE_RECON_HALT, "again")
    with pytest.raises(SystemExit, match="audit FAILED"):
        main.cmd_audit_live(cfg, conn, args)
    assert state.is_set(LIVE_RECON_HALT)


def test_missing_exchange_permission_or_timeout_cannot_be_assumed_safe(cfg, conn):
    from bots.risk.ledger import Ledger
    class Unqueryable:
        def fetch_open_orders(self):
            raise TimeoutError("API unavailable")
    with pytest.raises(ReconciliationError):
        reconcile_live_orders(Unqueryable(), Ledger(conn), ["BTC/USD"])
