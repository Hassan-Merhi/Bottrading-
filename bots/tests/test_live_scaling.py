"""Phase 8: real-money-only performance review and zero-auto-scale protections."""
import json

import pytest

from bots.evolution.verification import rules_hash
from bots.live.scale import live_evidence, scale_review
from bots.report.daily import daily_report_text
from bots.tests.riskkit import DAY, add_bot, live_cfg, make_guard, set_price


def _real_sample(cfg, conn, tmp_path):
    """Fabricated exchange data ONLY for isolated unit tests, never production."""
    cfg = live_cfg(cfg, capital=40)
    cfg["pilot"].update(max_bots=1, max_total_capital_usd=40)
    cfg["scaling"].update(enabled=True, owner_signoff=True,
                          max_total_capital_usd=40)
    guard, paper, exchange, alerts, clock = make_guard(
        cfg, conn, tmp_path, confirm="40")
    g = add_bot(conn, "pilot", "live")
    set_price(guard, 50_000)
    account_id = guard.open_live_account("pilot", 50_000)
    assert account_id
    started = clock.t
    clock.t += 65 * DAY
    conn.execute("UPDATE accounts SET cash=30, last_equity=30, peak_equity=30, "
                 "max_drawdown=0.02, trades=35, allocation=20, "
                 "hold_start_price=50000 WHERE account_id=?", (account_id,))
    for i in range(66):
        ts = started + i * DAY
        conn.execute("INSERT INTO equity_history (account_id,day,ts,equity) "
                     "VALUES (?,?,?,?)", (account_id, ts // DAY, ts, 25.0))
    for i in range(35):
        ts = started + (i + 1) * DAY
        for side in ("buy", "sell"):
            conn.execute(
                "INSERT INTO fills (account_id,bot_id,mode,ts,pair,side,qty,price,fee,pnl,reason) "
                "VALUES (?,'pilot','live',?,'BTC/USD',?,0.001,50000,0.01,?,'fake-test')",
                (account_id, ts, side, 0.2 if side == "sell" else None))
    conn.execute(
        "INSERT INTO pilot_certifications "
        "(bot_id,genome_hash,live_epoch,paper_account_id,created_at_ms,evidence_json) "
        "VALUES (?,?,?,?,?,?)",
        ("pilot", g.hash(), guard.live_epoch(), 99, started,
         json.dumps({"historical_rules_sha256": rules_hash(cfg)})))
    conn.execute(
        "INSERT INTO candles (pair,timeframe,ts,open,high,low,close,volume) "
        "VALUES ('BTC/USD','15m',?,50000,50000,50000,50000,1)",
        (clock.t - 900_000,))
    conn.commit()
    return guard, exchange, clock, account_id


def test_scaling_off_and_no_live_data_blocks_any_increase(cfg, conn):
    review = scale_review(conn, cfg, 1_800_000_000_000)
    assert review["status"] == "blocked"
    assert review["safe_to_auto_scale"] is False
    assert review["would_submit_orders"] is False
    assert review["approved_scale_cap_usd"] == 0
    assert review["live_accounts"] == 0


def test_real_settled_live_data_can_qualify_for_manual_review_only(cfg, conn, tmp_path):
    guard, exchange, clock, account_id = _real_sample(cfg, conn, tmp_path)
    ev = live_evidence(conn, guard.ledger.by_id(account_id), cfg, clock())
    assert ev["paper_verified_pilot_certificate"]
    assert ev["observed_live_days"] >= 45
    assert ev["closed_roundtrips"] == 35
    assert ev["net_return"] == pytest.approx(0.5)
    assert ev["realized_net_usd"] == pytest.approx(7.0)
    assert ev["fees_usd"] == pytest.approx(.7)
    review = scale_review(conn, cfg, clock())
    assert review["status"] == "review_eligible", review["review_blockers"]
    assert review["safe_to_auto_scale"] is False
    assert review["would_submit_orders"] is False
    assert review["approved_scale_cap_usd"] == 40
    assert exchange.orders == {}  # read-only review never hits the exchange
    assert guard.ledger.account("pilot", "live")["allocation"] == 20
    assert "Real-money scaling" in daily_report_text(conn, clock(), cfg)


def test_paper_results_never_substitute_for_live_trade_data(cfg, conn, tmp_path):
    guard, exchange, clock, acc_id = _real_sample(cfg, conn, tmp_path)
    conn.execute("DELETE FROM fills WHERE mode='live' AND account_id=?", (acc_id,))
    conn.commit()
    review = scale_review(conn, cfg, clock())
    assert review["status"] == "blocked"
    assert any("real-money round trips" in x for x in
               review["live_evidence"][0]["review_blockers"])


@pytest.mark.parametrize("flaw", ["no_certificate", "stale_quote", "missing_days",
                                  "large_gap", "drawdown", "net_loss", "owner_revoked"])
def test_live_scaling_denied_on_unverifiable_evidence(cfg, conn, tmp_path, flaw):
    guard, exchange, clock, acc_id = _real_sample(cfg, conn, tmp_path)
    if flaw == "no_certificate":
        conn.execute("DELETE FROM pilot_certifications")
    elif flaw == "stale_quote":
        conn.execute("DELETE FROM candles WHERE pair='BTC/USD'")
    elif flaw == "missing_days":
        conn.execute("DELETE FROM equity_history WHERE account_id=? AND ts<?",
                     (acc_id, clock.t - 15 * DAY))
    elif flaw == "large_gap":
        conn.execute("DELETE FROM equity_history WHERE account_id=? AND ts>? AND ts<?",
                     (acc_id, clock.t - 40 * DAY, clock.t - 25 * DAY))
    elif flaw == "drawdown":
        conn.execute("UPDATE accounts SET max_drawdown=.09 WHERE account_id=?", (acc_id,))
    elif flaw == "net_loss":
        conn.execute("UPDATE accounts SET cash=15, last_equity=15 WHERE account_id=?", (acc_id,))
    elif flaw == "owner_revoked":
        cfg["scaling"]["owner_signoff"] = False
    conn.commit()
    review = scale_review(conn, cfg, clock())
    assert review["status"] == "blocked"
    assert review["approved_scale_cap_usd"] == 0
    assert not review["safe_to_auto_scale"]


def test_notional_scaling_ceiling_cannot_exceed_hard_system_limit(cfg, conn, tmp_path):
    guard, _, clock, _ = _real_sample(cfg, conn, tmp_path)
    cfg["scaling"]["max_total_capital_usd"] = 101
    review = scale_review(conn, cfg, clock())
    assert review["status"] == "blocked"
    assert review["approved_scale_cap_usd"] == 0
    assert "ceiling" in " ".join(review["review_blockers"])


def test_unqualified_second_live_bot_disallows_scaling(cfg, conn, tmp_path):
    guard, _, clock, _ = _real_sample(cfg, conn, tmp_path)
    add_bot(conn, "second", "live")
    guard.ledger.open_account("second", "live", 10, guard.live_epoch(), clock(), 50000)
    review = scale_review(conn, cfg, clock())
    assert review["status"] == "blocked"
    assert review["live_accounts"] == 2
    assert review["approved_scale_cap_usd"] == 0
