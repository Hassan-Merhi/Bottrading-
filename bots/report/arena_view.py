"""Paper arena page: which evolved bots are trading fake money and how they are doing."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from bots.report.safety_view import _heading, _status_card


def _date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%d %b %Y %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return "never"


def _table(headers, rows):
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def arena_page(snap: dict) -> str:
    head = _heading("Paper arena", "Evolved bots practising on real prices with fake money.",
                    "After every evolution run its best bots come here and trade forward, whether "
                    "or not they passed the real-money gate. Losers are buried so evolution avoids "
                    "them; bots that stay ahead seed the next run. Nothing here can reach real money.")
    if not snap.get("enabled"):
        return head + '<p class="help">The paper arena is switched off in config.yaml.</p>'
    kpis = ('<div class="safety-status-grid">'
            + _status_card("Bots trading", f"{snap['active']} / {snap['max_bots']}",
                           f"${snap['allocation_usd']:.0f} fake money each")
            + _status_card("Ahead right now", str(snap.get("profitable", 0)),
                           f"{snap.get('winners', 0)} with enough trades to seed evolution")
            + _status_card("Net result", f"${snap.get('pnl', 0):+.2f}",
                           f"{snap.get('trades', 0)} trades, ${snap.get('fees', 0):.2f} fees")
            + _status_card("Retired so far", str(snap.get("retired_total", 0)),
                           "buried in the graveyard with the cause")
            + '</div>')
    bots = snap.get("bots") or []
    bots_html = ('<section id="bots"><h2>Bots trading</h2>'
                 + (_table(["Bot", "Strategy", "Days", "Trades", "Wins", "Result", "Return", "Drawdown", "Status"],
                           [[b["name"].split("/", 1)[1], b["strategy"], f"{b['days']:.1f}", b["trades"], b["wins"],
                             f"${b['pnl']:+.2f}", f"{b['return_pct']:+.1f}%", f"{b['max_drawdown_pct']:.1f}%",
                             "waiting for data" if b["waiting_for_data"] else ("in a trade" if b["in_position"] else "watching")]
                            for b in bots])
                    if bots else '<p class="help">Empty until the next evolution run finishes.</p>')
                 + '</section>')
    retired = snap.get("retired") or []
    retired_html = ('<section id="retired"><h2>Retired</h2>'
                    + (_table(["When", "Bot", "Why"], [[_date(r["retired_ms"]), r["name"].split("/", 1)[1], r["retire_reason"]]
                                                       for r in retired])
                       if retired else '<p class="help">Nobody has been retired yet.</p>')
                    + '</section>')
    return head + kpis + bots_html + retired_html
