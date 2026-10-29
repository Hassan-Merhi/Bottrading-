"""Numbers shared by the daily Telegram report and the dashboard."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from bots.risk.ledger import DAY_MS
from bots.paper.forward import evidence
from bots.risk.state import LAST_MODE, RiskState


def account_rows(conn: sqlite3.Connection, mode: str, active_only: bool = True) -> list[dict]:
    q = ("SELECT a.*, b.genome_json, b.status FROM accounts a JOIN deployed_bots b "
         "ON a.bot_id=b.bot_id WHERE a.mode=?")
    if active_only:
        q += " AND a.active=1"
    out = []
    for r in conn.execute(q + " ORDER BY a.account_id", (mode,)):
        d = dict(r)
        d["pair"] = json.loads(d["genome_json"])["pair"]
        d["pnl"] = d["last_equity"] - d["allocation"]
        d["pnl_today"] = d["last_equity"] - d["day_start_equity"]
        out.append(d)
    return out


def summary(conn: sqlite3.Connection, now: int) -> dict:
    day_start = now // DAY_MS * DAY_MS
    paper = account_rows(conn, "paper")
    live = account_rows(conn, "live", active_only=False)
    live_active = [a for a in live if a["active"]]
    retired_today = conn.execute(
        "SELECT bot_id, status_reason FROM deployed_bots WHERE status='retired' AND "
        "updated_at >= datetime(?, 'unixepoch')", (day_start // 1000,)).fetchall()
    grave_today = Counter(r[0] for r in conn.execute(
        "SELECT diagnosis FROM graveyard WHERE created_at >= datetime(?, 'unixepoch')",
        (day_start // 1000,)))
    events = conn.execute("SELECT kind, detail FROM risk_events WHERE ts >= ? ORDER BY ts",
                          (day_start,)).fetchall()
    every = paper + live_active
    best = max(every, key=lambda a: a["pnl"] / a["allocation"], default=None)
    state = RiskState(conn)
    forward = []
    for a in paper:
        ev = evidence(conn, a["account_id"], now)
        forward.append({"bot_id": a["bot_id"],
                        "observed_days": ev["observed_days"],
                        "observed_hours": ev["observed_hours"],
                        "max_observation_gap_hours": ev["max_observation_gap_hours"],
                        "verified_trades": ev["verified_trades"],
                        "profit_factor": ev["profit_factor"],
                        "realized_pnl": ev["realized_pnl"],
                        "fees": ev["fees"],
                        "last_observation_ms": ev["last_observation_ms"]})
    return {
        "mode": state.get(LAST_MODE, "PAPER"),
        "paper_forward": forward,
        "paper_pnl": sum(a["pnl"] for a in paper),
        "paper_pnl_today": sum(a["pnl_today"] for a in paper),
        "paper_bots": len(paper),
        "live_pnl": sum(a["pnl"] for a in live),
        "live_pnl_today": sum(a["pnl_today"] for a in live_active),
        "live_bots": len(live_active),
        "protected": sum(a["protected"] for a in live_active),
        "best": best,
        "retired_today": [dict(r) for r in retired_today],
        "evolution_deaths_today": dict(grave_today.most_common(5)),
        "risk_events": [dict(r) for r in events if r["kind"] != "graduation_decision"],
    }


def daily_report_text(conn: sqlite3.Connection, now: int) -> str:
    s = summary(conn, now)
    lines = [f"Daily report — mode {s['mode']}",
             f"Paper: {s['paper_bots']} bots, P&L {s['paper_pnl']:+.2f} USD "
             f"(today {s['paper_pnl_today']:+.2f})",
             f"Live: {s['live_bots']} bots, P&L {s['live_pnl']:+.2f} USD "
             f"(today {s['live_pnl_today']:+.2f}), protected funds {s['protected']:.2f} USD"]
    for a in s["paper_forward"]:
        lines.append(f"Forward {a['bot_id']}: {a['observed_days']} observed days, "
                     f"{a['verified_trades']} settled trades, "
                     f"profit factor {a['profit_factor']:.2f}, "
                     f"fees ${a['fees']:.2f}, "
                     f"realized P&L ${a['realized_pnl']:+.2f}. "
                     "Simulated, not guaranteed.")
    if s["best"]:
        b = s["best"]
        lines.append(f"Best bot: {b['bot_id']} ({b['mode']}) {b['pnl'] / b['allocation']:+.1%}")
    if s["retired_today"]:
        lines.append("Retired today: " + "; ".join(f"{r['bot_id']} — {r['status_reason']}"
                                                  for r in s["retired_today"]))
    if s["evolution_deaths_today"]:
        lines.append("Evolution deaths today: " + ", ".join(
            f"{k} ({v})" for k, v in s["evolution_deaths_today"].items()))
    if s["risk_events"]:
        lines.append(f"Risk events today ({len(s['risk_events'])}): " + "; ".join(
            e["detail"] for e in s["risk_events"][-5:]))
    else:
        lines.append("Risk events today: none")
    if s["mode"] == "LIVE":
        lines.append("Live trading can lose real money up to your configured caps.")
    return "\n".join(lines)

