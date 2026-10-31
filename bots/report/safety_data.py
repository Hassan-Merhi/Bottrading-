"""Read-only safety, settings, and human approval overview.

These views do not mutate risk_state, budgets, order controls, balances or
configuration. Every approval POST stays at the original token-gated endpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from bots.data.health import market_health
from bots.live.pilot import pilot_status
from bots.live.scale import scale_review
from bots.paper.forward import evidence
from bots.report.daily import summary
from bots.risk.guard import HARD_CAPITAL_CEILING_USD, risk_config_problems
from bots.risk.state import (RiskState, HEARTBEAT, KILL_SWITCH, CRASH_LOCK,
                             ENTRY_HALT, OWNER_PAUSE, DAILY_HALT_UNTIL,
                             LIVE_RECON_HALT)


def _num(v):
    try:
        return int(v)
    except (ValueError, TypeError, OverflowError):
        return None


def _active(state, key):
    return state.is_set(key)


def snapshot(conn: sqlite3.Connection, cfg: dict, now: int,
             approvals_available: bool = False) -> dict:
    """Read current persisted evidence only; never infer an exchange check."""
    st = RiskState(conn)
    mode = summary(conn, now)["mode"]
    worker = cfg["worker"]
    poll_ms = max(1, int(worker.get("poll_seconds", 60))) * 1000
    hb = _num(st.get(HEARTBEAT))
    age = now - hb if hb is not None else None
    # Match existing technical-dashboard freshness rule. A future heartbeat
    # is invalid, not proof of a healthy live loop.
    heartbeat_ok = age is not None and 0 <= age <= 3 * poll_ms + 120_000
    health = market_health(conn, cfg, now)
    market_ok = bool(health["healthy"]) and not bool(health["last_errors"])
    flags = {
        "emergency_stop": _active(st, KILL_SWITCH),
        "restart_lock": _active(st, CRASH_LOCK),
        "reconciliation_lock": _active(st, LIVE_RECON_HALT),
        "entry_halt": _active(st, ENTRY_HALT),
        "owner_pause": _active(st, OWNER_PAUSE),
        "daily_loss_halt": bool(_num(st.get(DAILY_HALT_UNTIL)) and
                                _num(st.get(DAILY_HALT_UNTIL)) > now),
    }
    descriptions = {
        "emergency_stop": ("Emergency stop is active",
                           "Live trading is locked. Investigate the event and use the "
                           "existing owner-controlled recovery procedure; this page cannot reset it."),
        "restart_lock": ("Restart approval is required",
                         "A previous shutdown was not clean. Real-money trading requires a "
                         "fresh owner confirmation through the original workflow."),
        "reconciliation_lock": ("Exchange reconciliation is locked",
                                "Exchange orders or balances need independent verification. "
                                "Never clear this lock just to make the warning disappear."),
        "entry_halt": ("New entries are paused",
                       "The system detected a data or API problem. Its original risk rules "
                       "decide when entries may resume."),
        "owner_pause": ("Owner paused new entries",
                        "New entries stay paused until the existing owner-controlled "
                        "resume action is used. Open positions are still managed."),
        "daily_loss_halt": ("Daily loss guard is active",
                            "Live entries are blocked until the recorded UTC expiry; "
                            "the loss threshold must not be bypassed."),
    }
    notices = [{"key": k, "title": descriptions[k][0], "explanation": descriptions[k][1]}
               for k, active in flags.items() if active]
    if not heartbeat_ok:
        notices.append({"key": "heartbeat", "title": "Trading worker not confirmed healthy",
                        "explanation": "Its heartbeat is missing, stale or invalid. "
                        "Verify the worker and logs before assuming trading is active."})
    if not market_ok:
        notices.append({"key": "data", "title": "Market prices need attention",
                        "explanation": "Missing, stale or gapped candles, or an exchange refresh "
                        "error, prevent a healthy market-data verdict."})
    active_live = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(allocation),0) FROM accounts "
        "WHERE mode='live' AND active=1").fetchone()
    live_accounts = int(active_live[0])
    allocated = float(active_live[1])
    pilot = pilot_status(conn, cfg, now)
    scaling = scale_review(conn, cfg, now)
    risks = cfg["risk"]
    queue = []
    for bot in conn.execute(
        "SELECT bot_id,genome_json,updated_at,status_reason "
        "FROM deployed_bots WHERE status='ready_to_graduate' ORDER BY updated_at,bot_id"):
        pair, strategy = "Unknown coin", "Strategy details unavailable"
        try:
            from bots.strategy.genome import Genome
            g = Genome.from_dict(json.loads(bot["genome_json"]))
            pair, strategy = g.pair, g.describe()
        except (ValueError, TypeError, KeyError):
            pass
        account = conn.execute(
            "SELECT * FROM accounts WHERE bot_id=? AND mode='paper' "
            "ORDER BY account_id DESC LIMIT 1", (bot["bot_id"],)).fetchone()
        observed = evidence(conn, account["account_id"], now) if account else None
        queue.append({
            "bot_id": bot["bot_id"], "pair": pair, "strategy": strategy,
            "updated_at": bot["updated_at"], "status_reason": bot["status_reason"],
            "paper_equity": float(account["last_equity"]) if account else None,
            "paper_allocation": float(account["allocation"]) if account else None,
            "paper_closed_trades": int(observed["verified_trades"]) if observed else 0,
            "observed_days": int(observed["observed_days"]) if observed else 0,
            "paper_days": (max(0, now - int(account["started_at"])) / 86_400_000
                           if account else 0),
        })
    events = [dict(r) for r in conn.execute(
        "SELECT ts,kind,detail FROM risk_events ORDER BY id DESC LIMIT 30")]
    risk_problems = risk_config_problems(cfg)
    if mode == "LIVE":
        headline = "Real-money mode is active"
        level = "danger"
        description = ("Live accounts can lose real funds. Check risk guards, position "
                       "protection and exchange status; a healthy heartbeat alone "
                       "does not prove that orders are safe.")
    elif notices:
        headline = "Trading system needs attention"
        level = "warning"
        description = ("Fake-money research and live trading have separate rules. "
                       "Review the warnings below; no funds have been enabled by this page.")
    else:
        headline = "System is monitoring in paper mode"
        level = "good"
        description = ("The worker and market feeds have recent evidence. "
                       "Research simulations are not real-money trading.")
    return {
        "read_only": True, "mode": mode, "headline": headline, "level": level,
        "description": description, "heartbeat_ms": hb, "heartbeat_age_seconds":
        round(age / 1000, 1) if age is not None and age >= 0 else None,
        "worker_healthy": heartbeat_ok, "market_healthy": market_ok,
        "market": {"fresh_pairs": health["fresh_pairs"],
                   "total_pairs": health["total_pairs"],
                   "unhealthy_pairs": list(health["unhealthy_pairs"]),
                   "last_errors": str(health["last_errors"])[:1200],
                   "last_success_ms": health["last_success_ms"],
                   "pairs": health["pairs"]},
        "flags": flags, "notices": notices, "live_accounts": live_accounts,
        "allocated_live_usd": allocated,
        "hard_ceiling_usd": HARD_CAPITAL_CEILING_USD,
        "risk_config_problems": risk_problems,
        "pilot": pilot, "scaling": scaling, "events": events,
        "approvals": queue, "approval_token_configured": approvals_available,
        "settings": {
            "live_enabled": cfg.get("live_enabled") is True,
            "max_total_live_usd": risks["max_total_capital_usd"],
            "max_per_live_bot_usd": risks["max_per_bot_usd"],
            "max_live_bots": risks["max_live_bots"],
            "daily_loss_limit_usd": risks["daily_loss_limit_usd"],
            "total_drawdown_kill_usd": risks["total_drawdown_kill_usd"],
            "pilot_enabled": cfg.get("pilot", {}).get("enabled") is True,
            "pilot_owner_signoff": cfg.get("pilot", {}).get("owner_signoff") is True,
            "pilot_exchange_signoff": cfg.get("pilot", {}).get("exchange_signoff") is True,
            "pilot_max_total_usd": cfg.get("pilot", {}).get("max_total_capital_usd", 0),
            "scaling_enabled": cfg.get("scaling", {}).get("enabled") is True,
            "paper_allocation_usd": cfg["paper"]["allocation_usd"],
            "paper_max_bots": cfg["paper"]["max_bots"],
            "research_enabled": cfg.get("research", {}).get("enabled", False),
            "research_league_enabled": cfg.get("research_league", {}).get("enabled", False),
            "research_allocation_usd": cfg.get("research_league", {}).get("allocation_usd", 0),
            "fee_taker_pct": cfg["backtest"]["taker_fee_pct"],
            "spread_bps": cfg["backtest"]["spread_bps"],
            "slippage_pct": cfg["backtest"]["slippage_pct"],
            "entry_order": cfg["execution"]["entry_order"],
            "market_refresh_seconds": worker["market_refresh_seconds"],
            "poll_seconds": worker["poll_seconds"],
            "evolution_hour_utc": worker["evolution_hour_utc"],
            "analyst_hour_utc": cfg.get("analyst", {}).get("hour_utc"),
            "graduation_days": cfg["graduation"]["min_paper_days"],
            "graduation_observed_days": cfg["graduation"]["min_observed_days"],
            "graduation_closed_trades": cfg["graduation"]["min_paper_trades"],
        },
        "note": "This page is read-only. It cannot unlock live mode or reset safety locks.",
    }
