"""Simple responsive charts and trade audit for the isolated paper league."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote


def _utc(ms):
    if ms is None:
        return "legacy mark (recorded time unavailable)"
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%d %b %Y %H:%M UTC")


def _usd(number, with_sign=False):
    if number is None:
        return "—"
    return f"${number:+,.2f}" if with_sign else f"${number:,.2f}"


def _num(number, digits=5):
    return f"{number:,.{digits}f}"


def equity_chart(points: list[dict]) -> str:
    """Observed values only. The price comparison uses matched simulated costs."""
    if len(points) < 2:
        return ('<div class="notice">The experiment is active. The equity graph needs at '
                'least two separately observed, closed-hour marks; no historical '
                'or artificial results will be inserted.</div>')
    W, H = 790, 288
    L, R, T, B = 80, 20, 25, 48
    values = [float(r[k]) for r in points for k in ("net_pnl", "benchmark_net_pnl")]
    low = min(0.0, *values)
    high = max(0.0, *values)
    pad = max(0.03, (high - low) * 0.12)
    lo, hi = low - pad, high + pad
    span = hi - lo
    def y(v):
        return T + (hi - v) / span * (H - T - B)
    def x(i):
        return L + i * (W - L - R) / (len(points) - 1)
    grid = []
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        yy = y(v)
        grid.append(
            f'<line x1="{L}" y1="{yy:.1f}" x2="{W-R}" y2="{yy:.1f}" '
            'stroke="var(--line)" stroke-width="1"/>'
            f'<text x="{L-8}" y="{yy+4:.1f}" text-anchor="end" '
            f'font-size="12" fill="var(--ink-2)">{escape(_usd(v, True))}</text>')
    series = []
    for key, clr in (("net_pnl", "--series-1"), ("benchmark_net_pnl", "--series-3")):
        coords = " ".join(f"{x(i):.2f},{y(float(p[key])):.2f}"
                          for i, p in enumerate(points))
        series.append(f'<polyline points="{coords}" fill="none" '
                      f'stroke="var({clr})" stroke-width="3" '
                      'stroke-linecap="round" stroke-linejoin="round"/>')
    return (
        '<div class="legend"><span><i style="background:var(--series-1)"></i>'
        'Research league: net P&amp;L</span><span><i style="background:var(--series-3)"></i>'
        'Buy-and-hold: modeled net P&amp;L</span></div>'
        '<div style="width:100%;overflow-x:auto">'
        f'<svg role="img" aria-label="Observed simulated profits compared to buy and hold" '
        f'viewBox="0 0 {W} {H}" style="width:100%;min-width:310px;max-height:310px">'
        + "".join(grid)
        + f'<line x1="{L}" y1="{y(0):.1f}" x2="{W-R}" y2="{y(0):.1f}" '
          'stroke="var(--ink-3)" stroke-dasharray="3 4"/>'
        + "".join(series)
        + f'<text x="{L}" y="{H-10}" font-size="12" fill="var(--ink-2)">'
          f'{escape(_utc(points[0]["observed_ms"])[:17])}</text>'
        + f'<text x="{W-R}" y="{H-10}" font-size="12" text-anchor="end" '
          f'fill="var(--ink-2)">{escape(_utc(points[-1]["observed_ms"])[:17])}</text>'
        + '</svg></div>'
        '<p class="help">Only observed hourly marks are plotted. Missing hours are not '
        'filled in; lines simply connect available measurements. All returns are '
        'simulated, not real Kraken orders.</p>')


def _stats_panel(stats: dict) -> str:
    items = [
        ("Fake starting capital", _usd(stats["capital"])),
        ("Current virtual equity", _usd(stats.get("equity", stats["capital"]))),
        ("Net P&L after modeled costs", _usd(stats["net_pnl"], True)),
        ("Realized P&L (closed trades)", _usd(stats["realized_pnl"], True)),
        ("Completed round trips", str(stats["completed_trades"])),
        ("Win rate", f'{stats["win_rate_pct"]:.1f}%' if stats["win_rate_pct"] is not None else "—"),
        ("Total simulated fees paid", _usd(stats["fees"])),
        ("Maximum drawdown", f'{stats["max_drawdown_pct"]:.2f}%'),
        ("Net buy-and-hold P&L", _usd(stats["benchmark_net_pnl"], True)),
        ("Verified observed hours / UTC days", f'{stats["observed_hours"]} / {stats["observed_days"]}'),
        ("Open simulated positions", str(stats["open_positions"])),
    ]
    return ('<div class="grid">' + "".join(
        '<div class="tile"><span class="lbl">' + escape(k) +
        '</span><b style="font-size:20px">' + escape(v) + '</b></div>'
        for k, v in items) + '</div>')


def _trades_table(trades: list[dict]) -> str:
    if not trades:
        return ('<div class="notice">No simulated orders have been filled yet. '
                'Trading signals are not forced, and the bot never invents returns.</div>')
    header = ["Observed fill time (UTC)", "Bot", "Side", "Quantity", "Fill price",
              "Fee", "Realized net P&L", "Reason"]
    rows = []
    for r in trades:
        fields = [_utc(r["ts"]), r["name"], r["side"].upper(),
                  _num(r["qty"], 7), _usd(r["price"]), _usd(r["fee"]),
                  _usd(r["pnl"], True), r["reason"]]
        rows.append('<tr>' + "".join(
            f'<td data-label="{escape(h, quote=True)}">{escape(v)}</td>'
            for h, v in zip(header, fields)) + '</tr>')
    return ('<div style="overflow-x:auto"><table class="stack">'
            '<thead><tr>' + "".join(f'<th>{escape(v)}</th>' for v in header) +
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')


def history_page(data: dict) -> str:
    name = data["name"]
    selected = data["bots"]
    opts = ['<option value="all"' + (' selected' if name == "all" else '') +
            '>All research bots</option>']
    for bot in selected:
        opts.append(f'<option value="{escape(bot, quote=True)}"' +
                    (' selected' if name == bot else '') + f'>{escape(bot)}</option>')
    st = data["stats"]
    latest = data["observations"][-1] if data["observations"] else None
    observed = _utc(latest["observed_ms"]) if latest else "No marks yet"
    return (
        '<section id="equity"><h2>Forward trading performance — simulated only</h2>'
        '<p class="help">The research league uses paper money, observed Kraken ticker '
        'prices and strict after-fee accounting. A forward paper experiment is '
        'not a qualified strategy and does not enable live trading.</p>'
        '<form action="/research/forward" method="get" style="display:flex;gap:10px;'
        'align-items:center;flex-wrap:wrap;margin:16px 0">'
        '<label for="bot-select">Research bot</label>'
        '<select id="bot-select" name="name" style="padding:10px;max-width:260px;'
        'color:var(--ink);background:var(--panel);border:1px solid var(--line);'
        'border-radius:8px">' + "".join(opts) + '</select>'
        '<button type="submit">View results</button>'
        '</form>'
        '<p class="help"><b>Last witnessed mark:</b> ' + escape(observed) +
        ' · A mark is recorded after a new closed hourly candle, using '
        'the current Kraken quote at that time.</p>'
        + _stats_panel(st)
        + '<div style="margin-top:20px"><h3>Forward equity vs passive holding</h3>'
        + equity_chart(data["observations"]) + '</div>'
        + '<p class="help">The passive benchmark models a single buy-and-hold '
        'round trip using the same taker fees, spreads and slippage. Past paper '
        'performance cannot guarantee real profitability.</p>'
        '</section><section id="trades"><h2>Observed simulated buys and sells</h2>'
        '<p class="help">Immutable-by-design append-only trade events. Net realized '
        'P&amp;L on SELL includes both entry and exit costs; BUY P&amp;L is blank '
        'until the position closes. Rows show the newest trades first.</p>'
        + _trades_table(data["fills"])
        + '<p class="help"><a href="/api/research/forward?name=' + quote(name) +
        '">JSON audit</a> · <a href="/api/research/forward.csv?name=' + quote(name) +
        '&kind=equity">Download equity CSV</a> · '
        '<a href="/api/research/forward.csv?name=' + quote(name) +
        '&kind=trades">Download trade CSV</a></p></section>')


def overview_panel(data: dict) -> str:
    stats = data["stats"]
    latest = data["observations"][-1] if data["observations"] else None
    text = escape(_utc(latest["observed_ms"])) if latest else "No observed marks yet"
    return (
        '<div style="margin:16px 0"><h3>Actual forward paper evidence</h3>'
        '<p class="help">Recorded forward marks: ' + str(stats["observed_hours"]) +
        ' · Completed trades: ' + str(stats["completed_trades"]) +
        ' · Last observed: ' + text + '</p>'
        + equity_chart(data["observations"])
        + '<p class="help"><a href="/research/forward">View full forward-performance '
        'history, transaction ledger and CSV exports</a></p></div>')
