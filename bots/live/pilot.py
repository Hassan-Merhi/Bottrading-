"""Phase 7: independently gated first-funded-bot pilot.

No historical test, config toggle or Telegram status by itself qualifies for
real-money orders. Live account allocation and every subsequent live entry
must satisfy pilot limits. The production default is irrevocably OFF until the
owner separately chooses to enable the pilot and confirms exchange readiness.
"""
from __future__ import annotations

import json
import math
import sqlite3

from bots.evolution.verification import PROTOCOL_VERSION, rules_hash
from bots.paper.forward import evidence, net_hold_return
from bots.risk.ledger import DAY_MS, Ledger

HARD_PILOT_CAP_USD = 100.0  # never exceed the existing application hard cap


def pilot_config_reasons(cfg: dict) -> list[str]:
    p = cfg.get("pilot") or {}
    reasons = []
    if p.get("enabled") is not True:
        reasons.append("pilot explicitly disabled")
    if p.get("owner_signoff") is not True:
        reasons.append("separate pilot owner signoff missing")
    if p.get("exchange_signoff") is not True:
        reasons.append("real Kraken reconciliation and stop testing not signed off")
    for name, ceiling in (("max_total_capital_usd", HARD_PILOT_CAP_USD),
                          ("max_per_bot_usd", HARD_PILOT_CAP_USD)):
        v = p.get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            reasons.append(f"pilot.{name} must be a finite positive USD amount")
        elif v > ceiling:
            reasons.append(f"pilot.{name} exceeds hard ${ceiling:g} ceiling")
    bots = p.get("max_bots")
    if type(bots) is not int or bots < 1 or bots > 3:
        reasons.append("pilot.max_bots must be 1 to 3")
    if (isinstance(p.get("max_total_capital_usd"), (int, float))
            and isinstance(p.get("max_per_bot_usd"), (int, float))
            and p.get("max_total_capital_usd", 0) > 0
            and p.get("max_per_bot_usd", 0) > p.get("max_total_capital_usd")):
        reasons.append("pilot bot cap exceeds the pilot total cap")
    return reasons


def paper_qualification(conn: sqlite3.Connection, cfg: dict, bot_id: str,
                        genome_hash: str, pair: str, price: float | None,
                        now: int) -> list[str]:
    """Prove a bot's actual Phase 4 + 5 evidence before allocating real funds.

    Purely read-only; uses the original account, persisted forward quotes and
    exchange-style paper fills, never an editable 'approved' status alone.
    """
    reasons: list[str] = []
    row = conn.execute(
        "SELECT passed_test, test_json, stage FROM candidates WHERE genome_hash=?",
        (genome_hash,)).fetchone()
    if row is None or row["passed_test"] != 1 or row["stage"] != "deployed":
        reasons.append("missing successful historical test / deployed candidate")
    else:
        try:
            t = json.loads(row["test_json"])
            audit = t.get("_audit") or {}
            if (audit.get("protocol") != PROTOCOL_VERSION
                    or audit.get("rules_sha256") != rules_hash(cfg)
                    or t.get("gate_failures")):
                reasons.append("historical test audit does not match current verified rules")
        except (ValueError, TypeError):
            reasons.append("historical audit unreadable")
    paper = conn.execute(
        "SELECT * FROM accounts WHERE bot_id=? AND mode='paper' "
        "ORDER BY account_id DESC LIMIT 1", (bot_id,)).fetchone()
    if paper is None:
        return reasons + ["no actual forward-paper account"]
    ev = evidence(conn, paper["account_id"], now)
    g = cfg["graduation"]
    elapsed = (now - paper["started_at"]) / DAY_MS
    observed = ((now - ev["first_observation_ms"]) / DAY_MS
                if ev["first_observation_ms"] is not None else 0)
    if elapsed < g["min_paper_days"] or observed < g["min_paper_days"]:
        reasons.append("30-day forward-paper observation not yet complete")
    if ev["observed_days"] < g.get("min_observed_days", 21):
        reasons.append("insufficient independent forward market days")
    if ev["max_observation_gap_hours"] > g.get("max_observation_gap_hours", 72):
        reasons.append("unresolved long paper monitoring gap")
    max_quote_age = max(180_000, int(cfg["worker"]["poll_seconds"]) * 3000)
    if (ev["last_observation_ms"] is None
            or not 0 <= now - ev["last_observation_ms"] <= max_quote_age):
        reasons.append("stale forward-paper market observation")
    if (min(ev["verified_trades"], paper["trades"]) < g["min_paper_trades"]):
        reasons.append("insufficient completed paper round trips")
    if ev["profit_factor"] < g.get("min_paper_profit_factor", 1.1):
        reasons.append("insufficient realized paper profit factor")
    if paper["max_drawdown"] >= g["max_paper_drawdown_pct"] / 100:
        reasons.append("paper drawdown exceeded graduation limit")
    if (price is None or not isinstance(price, (int, float))
            or not math.isfinite(price) or price <= 0):
        reasons.append("no safe recent live price")
    else:
        equity = Ledger.equity(paper, price)
        ret = equity / paper["allocation"] - 1 if paper["allocation"] > 0 else -1
        hold = net_hold_return(paper["hold_start_price"], price, cfg)
        if not math.isfinite(ret) or ret <= 0:
            reasons.append("paper strategy not profitable after costs")
        if g["must_beat_hold"] and (hold is None or ret <= hold):
            reasons.append("paper strategy failed net buy-and-hold")
    decision = conn.execute(
        "SELECT 1 FROM risk_events WHERE kind='graduation_decision' "
        "AND detail LIKE ? LIMIT 1", (f"{bot_id} approved by %",)).fetchone()
    if not decision:
        reasons.append("no recorded manual owner approval of this bot")
    return reasons


def certify_pilot(conn: sqlite3.Connection, cfg: dict, bot_id: str,
                  genome_hash: str, epoch: int, now: int) -> None:
    """Freeze verified paper evidence on initial allocation, not on every trade.

    The original paper price becomes stale after graduation; a certificate
    binds that genuinely checked forward trial to the selected genome/epoch.
    """
    paper = conn.execute(
        "SELECT account_id FROM accounts WHERE bot_id=? AND mode='paper' "
        "ORDER BY account_id DESC LIMIT 1", (bot_id,)).fetchone()
    if paper is None:
        raise ValueError("no paper account to certify")
    snapshot = evidence(conn, paper["account_id"], now)
    snapshot["historical_rules_sha256"] = rules_hash(cfg)
    conn.execute(
        "INSERT INTO pilot_certifications "
        "(bot_id, genome_hash, live_epoch, paper_account_id, created_at_ms, evidence_json) "
        "VALUES (?,?,?,?,?,?)",
        (bot_id, genome_hash, epoch, paper["account_id"], now,
         json.dumps(snapshot, sort_keys=True)))
    conn.commit()


def certified_pilot(conn: sqlite3.Connection, cfg: dict, bot_id: str,
                    genome_hash: str, epoch: int) -> bool:
    row = conn.execute(
        "SELECT evidence_json FROM pilot_certifications "
        "WHERE bot_id=? AND genome_hash=? AND live_epoch=?",
        (bot_id, genome_hash, epoch)).fetchone()
    if not row:
        return False
    try:
        return (json.loads(row["evidence_json"]).get("historical_rules_sha256")
                == rules_hash(cfg))
    except (ValueError, TypeError):
        return False


def pilot_status(conn: sqlite3.Connection, cfg: dict, now: int) -> dict:
    """Unprivileged dashboard/API readiness: never prints secrets or writes."""
    active = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(allocation),0) capital "
        "FROM accounts WHERE mode='live' AND active=1").fetchone()
    p = cfg.get("pilot") or {}
    waiting = conn.execute(
        "SELECT COUNT(*) FROM deployed_bots WHERE status IN "
        "('paper','ready_to_graduate','approved')").fetchone()[0]
    return {
        "enabled": p.get("enabled") is True,
        "pilot_configuration_ready": not pilot_config_reasons(cfg),
        "configuration_blockers": pilot_config_reasons(cfg),
        "live_accounts": active["n"],
        "allocated_live_usd": active["capital"],
        "paper_or_waiting_bots": waiting,
        "owner_funded_launch": False if not active["n"] else True,
        "profitability_proven": False,
        "note": "Code readiness is not exchange verification or a funded launch.",
    }
