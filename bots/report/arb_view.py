"""Arbitrage page: what the scanner saw, in plain words. Read-only; all strings escaped."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from bots.report.safety_view import _heading, _status_card


def _date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%d %b %Y %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return "never"


def _pct(x, digits=3):
    return f"{float(x):+.{digits}f}%"


def _table(headers, rows):
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def arbitrage_page(snap: dict, now: int) -> str:
    head = _heading("Arbitrage", "Does buying on one exchange and selling on another pay?",
                    "The scanner reads live order books on each venue, walks them for a "
                    f"${snap['size_usd']:.0f} order, and subtracts both taker fees and slippage. "
                    "A paper account with coin and cash already on every venue takes the trade "
                    "when the leftover is at least "
                    f"{snap['min_net_edge_pct']:.2f}%. Fake money. No orders are sent anywhere.")
    if not snap.get("enabled"):
        return head + '<p class="help">The arbitrage scanner is switched off in config.yaml.</p>'
    venues = ", ".join(f"{n} ({v['taker_fee_pct']}% taker, {v['quote']})"
                       for n, v in snap["venues"].items() if v.get("enabled", True))
    pairs = snap.get("pairs") or []
    best = pairs[0] if pairs else None
    paper = snap.get("paper") or {}
    kpis = ('<div class="safety-status-grid">'
            + _status_card("Observations", f"{snap['observations']:,}",
                           f"over {snap.get('observed_days', 0):.1f} days; last {_date(snap.get('last_ms'))}")
            + _status_card("Best net edge ever",
                           _pct(100 * snap["best_ever"]["net_pct"]) if snap.get("best_ever") else "none yet",
                           (f"{snap['best_ever']['pair']} {snap['best_ever']['buy_venue']} to "
                            f"{snap['best_ever']['sell_venue']}") if snap.get("best_ever") else
                           "waiting for the first scan")
            + _status_card("Share of time profitable",
                           f"{best['positive_share_pct']:.1f}%" if best else "n/a",
                           f"{best['pair']}, the best pair" if best else "", "" if best and best["positive_share_pct"] > 0 else "")
            + _status_card("Paper arbitrage result",
                           f"${paper.get('arbitrage_pnl', 0):+.2f}" if paper else "no trades",
                           f"{paper.get('trades', 0)} trades, ${paper.get('fees_usd', 0):.2f} fees"
                           if paper else "nothing cleared the minimum edge")
            + '</div>')
    edges = ('<section id="edges"><h2>Price gaps between venues</h2>'
             f'<p class="help">Venues compared: {escape(venues)}. "Gap" is the raw price difference; '
             '"net" is what is left after both fees and slippage. Break-even for the gap is about '
             f'{_breakeven_text(snap)}.</p>'
             + (_table(["Pair", "Observations", "Avg gap", "Max gap", "Avg net", "Max net", "Profitable"],
                       [[p["pair"], p["observations"], _pct(p["avg_gross_pct"]), _pct(p["max_gross_pct"]),
                         _pct(p["avg_net_pct"]), _pct(p["max_net_pct"]), f"{p['positive_share_pct']:.1f}%"]
                        for p in pairs]) if pairs else '<p class="help">No order books observed yet.</p>')
             + ('<h3>Best moments in the last 24 hours</h3>' + _table(
                 ["When", "Pair", "Buy on", "Sell on", "Gap", "Net"],
                 [[_date(r["ts"]), r["pair"], r["buy_venue"], r["sell_venue"],
                   _pct(100 * r["gross_pct"]), _pct(100 * r["net_pct"])] for r in snap.get("best_last_24h") or []])
                if snap.get("best_last_24h") else "")
             + '</section>')
    inv = paper.get("inventory") or []
    paper_html = ('<section id="paper"><h2>Paper arbitrage</h2>'
                  '<p class="help">Every venue starts with the same cash and the same coin, so a trade '
                  'needs no transfer. The result is the value of all inventory now minus what the '
                  'untouched starting inventory would be worth at the same prices.</p>'
                  + (_table(["Pair", "Venue", "Cash", "Coin", "Start cash", "Start coin"],
                            [[r["pair"], r["venue"], f"${r['cash']:.2f}", f"{r['qty']:.6f}",
                              f"${r['start_cash']:.2f}", f"{r['start_qty']:.6f}"] for r in inv])
                     if inv else '<p class="help">Not seeded yet.</p>')
                  + (('<h3>Last trades</h3>' + _table(
                      ["When", "Pair", "Buy on", "Sell on", "Qty", "Fees", "Net"],
                      [[_date(t["ts"]), t["pair"], t["buy_venue"], t["sell_venue"], f"{t['qty']:.6f}",
                        f"${t['fees']:.3f}", f"${t['net_usd']:+.3f}"] for t in paper.get("last_trades") or []]))
                     if paper.get("last_trades") else "")
                  + '</section>')
    live = snap.get("live") or {}
    if live.get("enabled"):
        state_txt = "HALTED" if live.get("halt") else ("READY: will trade when an edge clears" if live.get("ready") else "Locked")
        live_html = ('<section id="live"><h2>Real-money arbitrage</h2>'
                     '<p class="help">Enabled in config. It trades only while every gate below passes, '
                     f'${float(live.get("per_trade_usd") or 0):.0f} per leg, within '
                     f'${float(live.get("max_total_capital_usd") or 0):.0f} of confirmed capital.</p>'
                     '<div class="safety-status-grid">'
                     + _status_card("State", state_txt, live.get("halt") or "", "warn" if live.get("halt") else "")
                     + _status_card("Real trades", str(live.get("trades", 0)),
                                    f"{live.get('legged', 0)} with unequal fills")
                     + _status_card("Net result", f"${live.get('net_usd', 0):+.2f}",
                                    f"after ${live.get('fees_usd', 0):.2f} fees")
                     + '</div>'
                     + (('<h3>Why it is not trading right now</h3><ul>' + "".join(
                         f"<li>{escape(str(b))}</li>" for b in live.get("blockers") or []) + '</ul>')
                        if live.get("blockers") else "")
                     + (('<h3>Last real trades</h3>' + _table(
                         ["When", "Pair", "Status", "Bought", "Sold", "Fees", "Net", "Detail"],
                         [[_date(t["ts"]), t["pair"], t["status"], f"{t['buy_filled']:.6f}", f"{t['sell_filled']:.6f}",
                           f"${t['fees']:.3f}", f"${t['net_usd']:+.3f}", t["detail"]] for t in live.get("last_trades") or []]))
                        if live.get("last_trades") else "")
                     + '</section>')
    else:
        live_html = ('<section id="live"><h2>Real-money arbitrage</h2><p class="help">Off. '
                     'arbitrage.live.enabled is false; nothing here can place an order.</p></section>')
    funding = snap.get("funding") or []
    funding_html = ('<section id="funding"><h2>Funding-rate carry</h2>'
                    '<p class="help">Long the coin on spot, short the same amount of perpetual futures, '
                    'collect the funding that longs pay shorts. Price moves cancel out; the costs are '
                    'four taker fills. "Net per year" assumes the position is held for the configured '
                    'period and then closed. Negative means the trade would pay to exist.</p>'
                    + (_table(["Perpetual", "Rate per interval", "Gross per year", "Net per year", "Break-even hold", "Seen"],
                              [[f["symbol"], _pct(100 * f["rate"], 4), _pct(100 * f["annual_gross_pct"], 1),
                                _pct(100 * f["annual_net_pct"], 1),
                                f"{f['breakeven_days']:.0f} days" if f.get("breakeven_days") else "never",
                                _date(f["ts"])] for f in funding])
                       if funding else '<p class="help">No funding rates observed yet (needs arbitrage.funding.enabled and a reachable futures venue).</p>')
                    + '</section>')
    errors = snap.get("scan_errors") or []
    err_html = ('<section><h2>Scan problems in the last 24 hours</h2>' + _table(
        ["When", "Venue", "Detail"], [[_date(e["ts"]), e["venue"], e["detail"]] for e in errors]) + '</section>') if errors else ""
    return head + kpis + edges + paper_html + live_html + funding_html + err_html


def _breakeven_text(snap: dict) -> str:
    from bots.arb.model import breakeven_gross_pct

    v = [x for x in snap["venues"].values() if x.get("enabled", True) and x.get("taker_fee_pct") is not None]
    if len(v) < 2:
        return "unknown"
    fees = sorted(float(x["taker_fee_pct"]) / 100 for x in v)
    return f"{100 * breakeven_gross_pct(fees[-1], fees[-2], 0.0005):.2f}%"
