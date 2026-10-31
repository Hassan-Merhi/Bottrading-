"""Beginner-friendly performance and trade history; read-only HTML rendering."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

from bots.report.forward_page import equity_chart
from bots.report.performance import build_link, bot_slug, compact_series

PERIOD_LABELS = (("today", "Today"), ("7d", "7 days"), ("30d", "30 days"), ("all", "All time"))


def cash(value, signed=False):
    if value is None:
        return "Not enough data"
    return f"${value:+,.2f}" if signed else f"${value:,.2f}"


def when(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _tile(title, value, explanation, tone=""):
    return ('<article class="perf-tile"><div class="perf-title">' + escape(title) +
            '</div><strong class="' + escape(tone) + '">' + escape(value) +
            '</strong><small>' + escape(explanation) + '</small></article>')


def _filter_url(name, period, side="all", result="all", search=""):
    return build_link("/research/forward", name=name, period=period, side=side,
                      result=result, search=search)


def _options(items, current):
    return "".join('<option value="' + escape(value, quote=True) + '"' +
                   (' selected' if value == current else '') + '>' +
                   escape(label) + '</option>' for value, label in items)


def _fees(buy, sell):
    total = buy + sell
    if total <= 0:
        return ('<div class="home-empty"><b>No trading fees recorded for this period.</b>'
                '<p>Fees appear after simulated buys or sells happen.</p></div>')
    buy_pct = 100 * buy / total
    return ('<div class="fee-card"><div class="fee-bar" role="img" aria-label="Buy and sell fees">'
            '<span style="width:' + f'{buy_pct:.2f}' + '%"></span>'
            '<span style="width:' + f'{100-buy_pct:.2f}' + '%"></span></div>'
            '<div class="fee-legend"><span>Buy fees <b>' + escape(cash(buy)) +
            '</b></span><span>Sell fees <b>' + escape(cash(sell)) +
            '</b></span><span>Total <b>' + escape(cash(total)) +
            '</b></span></div></div>')


def _trade_rows(data):
    if not data["fills"]:
        return ('<div class="home-empty"><b>No transactions match these filters.</b>'
                '<p>Try changing the dates or clearing the search. Empty results are not losses.</p>'
                '</div>')
    headers = ("Date (UTC)", "Experiment", "Coin", "Action", "Price", "Amount",
               "Fee", "Profit / loss", "Why?")
    rows = []
    for f in data["fills"][:250]:
        pnl = f["pnl"]
        pnl_class = ("pos" if pnl is not None and float(pnl) > 0 else
                     "neg" if pnl is not None and float(pnl) < 0 else "")
        side = "Bought" if f["side"] == "buy" else "Sold"
        detail = ('<details class="trade-detail"><summary>See details</summary>'
                  '<p><b>Reason:</b> ' + escape(str(f["reason"])) + '</p>'
                  '<p><b>Fill reference:</b> ' + escape(str(f["id"])) +
                  ' · <b>Quantity:</b> ' + f'{float(f["qty"]):.8f}' + '</p>'
                  '<p>' + ('A buy has no realized profit yet. The sale will show '
                            'the full modeled net result when the position closes.'
                            if pnl is None else
                            'This realized amount includes the original simulated buy '
                            'cost and the sell fee.') + '</p></details>')
        cells = [when(f["ts"]), f["name"], f["pair"], side, cash(float(f["price"])),
                 f'{float(f["qty"]):.7f}', cash(float(f["fee"])),
                 cash(float(pnl), True) if pnl is not None else "—", detail]
        rows.append('<tr>' + "".join(
            '<td data-label="' + escape(h, quote=True) + '"' +
            (' class="' + pnl_class + '"' if h == "Profit / loss" else '') + '>' +
            (v if h == "Why?" else escape(v)) + '</td>'
            for h, v in zip(headers, cells)) + '</tr>')
    return ('<div class="trade-table-wrap"><table class="stack perf-table"><thead><tr>' +
            "".join('<th>' + escape(h) + '</th>' for h in headers) +
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
            + ('<p class="help">Showing 250 most recent matching events. Export CSV to '
               'see the full available range.</p>' if len(data["fills"]) > 250 else ''))


def performance_page(data: dict) -> str:
    name, period = data["name"], data["period"]
    s, all_time = data["period_stats"], data["all_time"]
    bot_options = [("all", "All experimental bots")] + [(n, n) for n in data["bots"]]
    since = (when(s["first_ms"]) + " to " + when(s["last_ms"])
             if s["first_ms"] is not None else
             "Not enough separately observed prices in this time range")
    if s["change"] is None:
        result_line = ("This period does not yet have two verified observations. "
                       "Showing a profit or loss would be misleading.")
    else:
        result_line = ("These are changes between the first and last real observations "
                       "inside your selected dates. Missing hours are never filled in.")
    headline = (
        '<div class="home-heading"><span class="home-eyebrow">Profit &amp; performance</span>'
        '<h1>Is your fake-money trading profitable?</h1>'
        '<p>View recorded simulated outcomes, compare coins, understand fees '
        'and find every buy or sell. No live-money results are mixed in.</p></div>'
        '<div class="status-pill good">Simulation only · no real-money trading</div>')
    tabs = ('<nav class="period-tabs" aria-label="Time period">' +
            "".join('<a href="' + escape(_filter_url(name, key, data["side"],
                                                     data["result"], data["search"]), quote=True) +
                    '"' + (' aria-current="page"' if key == period else '') +
                    '>' + escape(label) + '</a>' for key, label in PERIOD_LABELS) + '</nav>')
    controls = (
        '<form class="perf-filters" method="get" action="/research/forward">'
        '<label>Trading bot<select name="name">' + _options(bot_options, name) +
        '</select></label><label>Period<select name="period">' +
        _options(PERIOD_LABELS, period) +
        '</select></label><button type="submit">Update chart</button></form>')
    cards = (
        '<div class="perf-grid">'
        + _tile("Profit / loss for this period", cash(s["change"], True),
                "Between verified observed balances only",
                "pos" if (s["change"] or 0) > 0 else "neg" if (s["change"] or 0) < 0 else "")
        + _tile("Realized from closed trades", cash(s["realized_pnl"], True),
                str(s["completed_trades"]) + " completed sale(s) in this period")
        + _tile("Estimated trading fees", cash(s["total_fees"]),
                str(s["buy_orders"]) + " buys · " + str(s["sell_orders"]) + " sells")
        + _tile("Win rate", f'{s["win_rate_pct"]:.1f}%' if s["win_rate_pct"] is not None else "—",
                "Calculated only from closed trades")
        + '</div>')
    chart = (
        '<section id="equity"><div class="section-head"><h2>Your profit compared with holding</h2>'
        '<a class="text-link" href="/bots">View bots →</a></div>'
        '<p class="help">Simulated portfolio profit/loss after modeled fees vs the '
        'same coins bought and held with modeled costs. ' + escape(result_line) + '</p>'
        + equity_chart(compact_series(data["marks"]))
        + '<p class="inline-tip">Verified range: ' + escape(since) + '</p></section>')
    fees = (
        '<section id="fees"><h2>Where did the fees go?</h2>'
        '<p class="help">These are estimated transaction costs already applied to your '
        'simulation. They are shown for information, not deducted twice.</p>'
        + _fees(s["buy_fees"], s["sell_fees"]) + '</section>')
    search = (
        '<form class="perf-filters trade-filters" method="get" '
        'action="/research/forward#trades">'
        '<input type="hidden" name="name" value="' + escape(name, quote=True) + '">'
        '<input type="hidden" name="period" value="' + escape(period, quote=True) + '">'
        '<label>Action<select name="side">' +
        _options((("all", "Buy and sell"), ("buy", "Bought"), ("sell", "Sold")), data["side"]) +
        '</select></label>'
        '<label>Outcome<select name="result">' +
        _options((("all", "All outcomes"), ("profit", "Profitable sells"),
                  ("loss", "Losing sells")), data["result"]) + '</select></label>'
        '<label>Search<input name="search" maxlength="120" placeholder="Coin, bot, reason" value="' +
        escape(data["search"], quote=True) + '"></label>'
        '<button type="submit">Filter transactions</button>'
        '<a class="text-link" href="' + escape(_filter_url(name, period), quote=True) +
        '#trades">Clear filters</a></form>')
    base = dict(name=name, period=period, side=data["side"], result=data["result"],
                search=data["search"])
    csv_trades = build_link("/api/performance.csv", **base, kind="trades")
    csv_equity = build_link("/api/performance.csv", **base, kind="equity")
    audit = build_link("/api/performance", **base)
    trades = ('<section id="trades"><div class="section-head"><h2>Every recorded trade</h2>'
              '<span class="status-pill">' + str(len(data["fills"])) +
              ' matches</span></div>'
              '<p class="help">Each row is an observed simulated fill, not an actual '
              'exchange order. Profit is recorded only when a position is sold.</p>'
              + search + _trade_rows(data)
              + '<div class="export-links"><a href="' +
              escape(csv_trades, quote=True) +
              '">Download filtered trades CSV</a><a href="' +
              escape(csv_equity, quote=True) +
              '">Download verified equity CSV</a><a href="' +
              escape(audit, quote=True) + '">View JSON audit</a></div></section>')
    cap_notice = ('<p class="advanced-note">The stored ledger exceeds this report’s '
                  '25,000-observation safety limit. The visible range is capped; '
                  'do not treat it as a complete all-time history.</p>'
                  if data["history_cap"] else '')
    return (headline + tabs + controls + cards +
            '<section class="perf-summary"><div class="status-list">'
            '<div class="status-row"><span>Total net result since each bot started '
            '(all time)</span><strong>' + escape(cash(all_time["net_pnl"], True)) +
            '</strong></div><div class="status-row"><span>Recorded hours for selected '
            'dates</span><strong>' + str(s["verified_marks"]) + '</strong></div>'
            '<div class="status-row"><span>Benchmark movement for the period</span><strong>' +
            escape(cash(s["benchmark_change"], True)) + '</strong></div></div></section>'
            + cap_notice + chart + fees + trades)
