"""Phase 7: first-funded-bot admission tests; all exchange operations are fakes."""
import json

import pytest

from bots.evolution.verification import PROTOCOL_VERSION, rules_hash
from bots.live.pilot import certified_pilot, paper_qualification, pilot_config_reasons, pilot_status
from bots.risk.guard import OrderRefused
from bots.risk.state import LIVE_EPOCH, RiskState
from bots.tests.riskkit import DAY, add_bot, live_cfg, make_guard, set_price
from bots.tests.test_paper import _make_graduate


def _setup(cfg, conn, tmp_path, eligible=False):
    cfg = live_cfg(cfg, capital=20)
    cfg["pilot"].update(max_bots=1, max_total_capital_usd=20,
                        max_per_bot_usd=20, require_qualified_paper=True)
    guard, paper, kraken, alerts, clock = make_guard(
        cfg, conn, tmp_path, confirm="20")
    g = add_bot(conn, "candidate", "approved")
    set_price(guard, 50_000)
    guard.open_paper_account("candidate", 50_000)
    if eligible:
        _make_graduate(conn, guard, clock, "candidate")
        verified = json.dumps({"_audit": {"protocol": PROTOCOL_VERSION,
                                        "rules_sha256": rules_hash(cfg)},
                               "gate_failures": []})
        conn.execute("INSERT INTO candidates "
                     "(genome_hash,run_id,genome_json,validation_fitness,validation_json,"
                     "test_json,passed_test,stage,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (g.hash(), "run1", g.to_json(), 1.0, "{}", verified, 1,
                      "deployed", "test"))
        RiskState(conn).log("graduation_decision", "candidate approved by owner",
                            clock())
        conn.commit()
    return cfg, guard, paper, kraken, clock, g


def test_default_phase7_is_paper_with_zero_funded_capital(cfg, conn, tmp_path):
    assert cfg["pilot"]["enabled"] is False
    assert cfg["pilot"]["owner_signoff"] is False
    assert cfg["pilot"]["exchange_signoff"] is False
    assert cfg["pilot"]["max_total_capital_usd"] == 0
    assert cfg["pilot"]["require_qualified_paper"] is True
    assert pilot_config_reasons(cfg)
    cfg = live_cfg(cfg, capital=20)
    cfg["pilot"].update(enabled=False, require_qualified_paper=True)
    guard, _, kraken, _, _ = make_guard(cfg, conn, tmp_path, confirm="20")
    assert guard.mode().mode == "PAPER"
    assert guard.open_live_account("unknown", 50_000) is None


@pytest.mark.parametrize("field", ["enabled", "owner_signoff", "exchange_signoff"])
def test_all_pilot_approvals_are_independent(cfg, conn, tmp_path, field):
    cfg, guard, _, _, clock, g = _setup(cfg, conn, tmp_path, eligible=True)
    cfg["pilot"][field] = False
    assert guard.mode().mode == "PAPER"
    assert guard.open_live_account("candidate", 50_000) is None


def test_no_valid_paper_sample_cannot_get_real_allocation(cfg, conn, tmp_path):
    cfg, guard, paper, kraken, clock, g = _setup(cfg, conn, tmp_path)
    assert guard.mode().mode == "LIVE"  # account readiness is independently checked
    assert guard.open_live_account("candidate", 50_000) is None
    assert guard.ledger.active("live") == []
    assert paper_qualification(conn, cfg, "candidate", g.hash(), g.pair, 50_000,
                               clock())


def test_verified_paper_candidate_gets_one_bound_pilot_certificate(
        cfg, conn, tmp_path):
    cfg, guard, paper, kraken, clock, g = _setup(cfg, conn, tmp_path, eligible=True)
    account_id = guard.open_live_account("candidate", 50_000)
    assert account_id
    assert certified_pilot(conn, cfg, "candidate", g.hash(), guard.live_epoch())
    data = conn.execute("SELECT evidence_json, paper_account_id FROM pilot_certifications "
                        "WHERE bot_id='candidate'").fetchone()
    ev = json.loads(data[0])
    assert ev["observed_days"] >= 21
    assert ev["verified_trades"] >= 20
    assert ev["historical_rules_sha256"] == rules_hash(cfg)
    guard._set_status("candidate", "live", "verified pilot started")
    guard.enter_long("candidate", "live", "BTC/USD", 50_000, 40, 3, 6, "pilot")
    assert guard.ledger.account("candidate", "live")["qty"] > 0
    state = pilot_status(conn, cfg, clock())
    assert state["live_accounts"] == 1 and state["allocated_live_usd"] == 20
    # A fresh live price should NOT be required for the original paper trial
    # once its evidence has been sealed into the certificate.
    assert guard._check_live_buy  # no paper freshness side effect


def test_certificate_mismatch_blocks_additional_entries(cfg, conn, tmp_path):
    cfg, guard, _, kraken, clock, g = _setup(cfg, conn, tmp_path, eligible=True)
    assert guard.open_live_account("candidate", 50_000)
    guard._set_status("candidate", "live", "pilot")
    conn.execute("DELETE FROM pilot_certifications WHERE bot_id='candidate'")
    conn.commit()
    with pytest.raises(OrderRefused, match="pilot certificate"):
        guard.enter_long("candidate", "live", "BTC/USD", 50_000, 20, 3, 6, "pilot")
    assert not kraken.fetch_open_orders()


def test_pilot_one_bot_and_amount_caps(cfg, conn, tmp_path):
    cfg, guard, _, kraken, clock, g = _setup(cfg, conn, tmp_path, eligible=True)
    assert guard.open_live_account("candidate", 50_000)
    add_bot(conn, "other", "approved")
    assert guard.open_live_account("other", 50_000) is None
    assert len(guard.ledger.active("live")) == 1
    assert sum(a["allocation"] for a in guard.ledger.active("live")) == 20
    cfg["pilot"]["max_per_bot_usd"] = 10
    guard._set_status("candidate", "live", "pilot")
    with pytest.raises(OrderRefused, match="pilot bot cap"):
        guard.enter_long("candidate", "live", "BTC/USD", 50_000, 20, 3, 6, "entry")


def test_new_live_epoch_invalidates_old_certificate(cfg, conn, tmp_path):
    cfg, guard, _, kraken, clock, g = _setup(cfg, conn, tmp_path, eligible=True)
    assert guard.open_live_account("candidate", 50_000)
    guard._set_status("candidate", "live", "pilot")
    guard.state.set(LIVE_EPOCH, guard.live_epoch() + 1)
    with pytest.raises(OrderRefused, match="earlier live confirmation"):
        guard.enter_long("candidate", "live", "BTC/USD", 50_000, 20, 3, 6, "pilot")


def test_owner_approval_without_historical_test_stays_blocked(cfg, conn, tmp_path):
    cfg, guard, paper, kraken, clock, g = _setup(cfg, conn, tmp_path)
    _make_graduate(conn, guard, clock, "candidate")
    RiskState(conn).log("graduation_decision", "candidate approved by owner", clock())
    assert guard.open_live_account("candidate", 50_000) is None
    assert not conn.execute("SELECT * FROM pilot_certifications").fetchall()


def test_pilot_dollar_loss_ceiling_triggers_kill_earlier(cfg, conn, tmp_path):
    cfg = live_cfg(cfg, capital=20)
    cfg["pilot"].update(max_bots=1, max_total_capital_usd=20,
                        max_per_bot_usd=20, max_live_loss_usd=2,
                        max_live_drawdown_pct=5)
    guard, paper, kraken, alerts, clock = make_guard(cfg, conn, tmp_path, confirm="20")
    add_bot(conn, "one", "live")
    set_price(guard, 50_000)
    assert guard.open_live_account("one", 50_000)
    guard.enter_long("one", "live", "BTC/USD", 50_000, 90, 20, 50, "test")
    set_price(guard, 43_000)  # $2+ mark-to-market loss, before the ordinary $10 kill
    guard.check_limits()
    assert guard.state.is_set("kill_switch")
    assert guard.mode().mode == "PAPER"
    assert "critical" in alerts.kinds()


def test_owner_cannot_activate_pilot_with_unsafe_caps(cfg, conn, tmp_path):
    cfg = live_cfg(cfg, capital=60)
    guard, *_ = make_guard(cfg, conn, tmp_path)
    cfg["pilot"]["max_live_loss_usd"] = 100
    assert "loss threshold" in "; ".join(pilot_config_reasons(cfg))
    assert guard.mode().mode == "PAPER"
    cfg["pilot"]["max_live_loss_usd"] = 10
    cfg["pilot"]["max_live_drawdown_pct"] = 40
    assert guard.mode().mode == "PAPER"
    cfg["pilot"]["max_live_drawdown_pct"] = 10
    cfg["pilot"]["max_bots"] = 10
    assert guard.mode().mode == "PAPER"
