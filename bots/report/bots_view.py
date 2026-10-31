"""Readable read-only bot directory and individual detail screens.

Research lab is separate from qualified paper and live ledgers. Never combine
them, claim strategy approval, or expose trading/approval mutations here.
"""
from __future__ import annotations

from html import escape
from datetime import datetime, timezone
import json

from bots.paper.forward import evidence as paper_evidence
from bots.paper.forward_history import forward_history
from bots.paper.league import research_genomes, snapshot as lab_snapshot
from bots.report.performance import report, bot_slug, compact_series
from bots.report.forward_page import equity_chart
from bots.report.performance_view import cash, when

EXPLANATIONS = {
    "BTC trend": ("Follows longer price movements",
                  "This bot looks for the shorter moving average to cross above the "
                  "longer average. A signal is not a guarantee of a profitable trade."),
    "ETH pullback": ("Looks for price recovery after a dip",
                     "This bot checks if ETH has fallen enough to look oversold. "
                     "It buys only if its existing rules generate an entry."),
    "SOL breakout": ("Watches for a move above a recent price range",
                     "This bot looks for SOL to break above recent highs. "
                     "False breakouts and losses are possible."),
}


def _card(name, pair, equity, pnl, status, url, category):
    tone = "pos" if pnl > 0 else "neg" if pnl < 0 else ""
    return (
        '<article class="bot-directory-card"><div class="bot-directory-top">'
        '<div><h3>' + escape(name) + '</h3><span>' + escape(pair) + '</span></div>'
        '<span class="status-pill">' + escape(category) + '</span></div>'
        '<div class="bot-directory-value">' + escape(cash(equity)) + '</div>'
        '<div class="' + tone + '">' + escape(cash(pnl, True)) +
        ' since start</div><p>' + escape(status) + '</p>'
        '<a class="button-link" href="' + escape(url, quote=True) +
        '">View details →</a></article>')


def bots_index(conn, cfg: dict) -> str:
    lab = lab_snapshot(conn, cfg)
    present = {b["name"]: b for b in lab.get("bots", [])}
    research = []
    for name, genome in research_genomes().items():
        b = present.get(name)
        url = "/bots/research/" + bot_slug(name)
        if b is None:
            research.append(_card(name, genome.pair, 0, 0,
                                  "Not initialized; waiting for a valid hourly market observation.",
                                  url, "Fake-money experiment"))
        else:
            status = ("Holding an open simulated position" if b["in_position"] else
                      "Waiting for the next rule-based opportunity")
            research.append(_card(name, genome.pair, float(b["equity"]),
                                  float(b["pnl"]), status, url, "Fake-money experiment"))
    # One latest account per bot and mode. Historical accounts stay readable by
    # their ID at the direct route, but do not generate duplicate directory cards.
    seen: set[tuple[str, str]] = set()
    standard = []
    rows = conn.execute(
        "SELECT a.*,b.genome_json,b.status FROM accounts a JOIN deployed_bots b "
        "ON a.bot_id=b.bot_id ORDER BY a.active DESC,a.account_id DESC")
    for a in rows:
        identity = (str(a["bot_id"]), str(a["mode"]))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            pair = json.loads(a["genome_json"])["pair"]
        except (KeyError, ValueError, TypeError):
            pair = "Unknown"
        is_live = a["mode"] == "live"
        label = "REAL MONEY" if is_live else "Standard paper account"
        status = ("Real-money trading account" if is_live else
                  "Standard paper account, separate from unapproved research bots")
        standard.append(_card(str(a["bot_id"]), pair, float(a["last_equity"]),
                              float(a["last_equity"]) - float(a["allocation"]),
                              status, "/bots/account/" + str(a["account_id"]), label))
    return (
        '<div class="home-heading"><span class="home-eyebrow">My trading bots</span>'
        '<h1>Every bot, explained clearly.</h1>'
        '<p>Click a bot to see what it is watching, how it performed, and '
        'what it has actually bought or sold.</p></div>'
        '<section><h2>Research experiments (fake money only)</h2>'
        '<p class="help">These bots are not the same as validated, approved or '
        'real-money bots. They cannot graduate automatically.</p>'
        '<div class="bot-directory">' + "".join(research) + '</div></section>'
        '<section><h2>Standard paper and real-money accounts</h2>'
        '<p class="help">These use the separate strict approval and risk system. '
        'A paper account is not necessarily qualified. LIVE accounts are clearly marked.</p>'
        + ('<div class="bot-directory">' + "".join(standard) + '</div>' if standard else
           '<div class="home-empty"><b>No standard paper or live accounts yet.</b>'
           '<p>Only the three isolated fake-money research experiments are active.</p></div>')
        + '</section>')


def research_detail(conn, cfg: dict, now: int, name: str, period: str) -> str:
    genome = research_genomes()[name]
    exists = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='lab_accounts' AND type='table'").fetchone())
    account = (conn.execute("SELECT * FROM lab_accounts WHERE name=?", (name,)).fetchone()
               if exists else None)
    if account is None:
        title, description = EXPLANATIONS[name]
        return (
            '<div class="home-heading"><span class="home-eyebrow">Fake-money experiment</span>'
            '<h1>' + escape(name) + '</h1><p>' + escape(title) + '</p></div>'
            '<div class="status-pill">Not started yet · Simulation only</div>'
            '<section><h2>What does this bot do?</h2><p>' + escape(description) +
            '</p><div class="home-empty"><b>No recorded trades or balances yet.</b>'
            '<p>The research account will appear after a valid new market observation. '
            'No historical results will be invented.</p></div>'
            '<details class="rule-details"><summary>Technical strategy rules</summary><p>' +
            escape(genome.describe()) + '</p></details></section>')
    data = report(conn, cfg, now=now, name=name, period=period)
    st = data["all_time"]
    s = data["period_stats"]
    title, description = EXPLANATIONS.get(name, ("Experimental strategy",
                                                  "This strategy is not approved for real orders."))
    state = ("Holding an open simulated position" if account is not None and
             float(account["qty"]) > 0 else
             "Monitoring prices; no open trade" if account is not None else
             "Waiting for its first valid market observation")
    period_links = "".join(
        '<a href="/bots/research/' + bot_slug(name) + '?period=' + p + '"' +
        (' aria-current="page"' if p == period else '') + '>' + label + '</a>'
        for p, label in (("today", "Today"), ("7d", "7 days"),
                         ("30d", "30 days"), ("all", "All time")))
    fills = data["fills_in_period"][:30]
    tx = "".join(
        '<div class="status-row"><span>' + escape(when(f["ts"])) + ' · ' +
        escape(f["side"].upper()) + ' · ' + escape(str(f["reason"])) +
        '</span><strong>' + escape(cash(float(f["pnl"]), True) if
                                  f["pnl"] is not None else
                                  cash(float(f["qty"]) * float(f["price"]))) +
        '</strong></div>' for f in fills)
    return (
        '<div class="home-heading"><span class="home-eyebrow">Fake-money experiment</span>'
        '<h1>' + escape(name) + '</h1><p>' + escape(title) + '</p></div>'
        '<div class="status-pill good">RESEARCH ONLY · Cannot place live orders</div>'
        '<section><h2>What does this bot do?</h2><p>' + escape(description) +
        '</p><p class="help"><strong>Current situation:</strong> ' + escape(state) +
        '</p><details class="rule-details"><summary>Show the technical strategy rules</summary>'
        '<p>' + escape(genome.describe()) + '</p></details></section>'
        '<div class="perf-grid">'
        '<article class="perf-tile"><div class="perf-title">Current fake balance</div>'
        '<strong>' + escape(cash(st.get("equity", st["capital"]))) +
        '</strong><small>Initial experiment: ' + escape(cash(st["capital"])) + '</small></article>'
        '<article class="perf-tile"><div class="perf-title">Profit since starting</div>'
        '<strong>' + escape(cash(st["net_pnl"], True)) +
        '</strong><small>After modeled costs</small></article>'
        '<article class="perf-tile"><div class="perf-title">Completed trades</div>'
        '<strong>' + str(st["completed_trades"]) +
        '</strong><small>Recorded closed positions</small></article>'
        '<article class="perf-tile"><div class="perf-title">Trading fees paid</div>'
        '<strong>' + escape(cash(st["fees"])) +
        '</strong><small>Modeled, not exchange-charged</small></article></div>'
        '<section id="history"><h2>How has this bot performed?</h2>'
        '<nav class="period-tabs" aria-label="Performance period">' + period_links + '</nav>'
        '<p class="help">Performance chart uses only recorded market observations. '
        'Profit for selected dates is measured between the first and last '
        'verified marks inside that window, not made-up prices.</p>'
        + equity_chart(compact_series(data["marks"]))
        + '<p><strong>Change for selected dates: </strong>' +
        escape(cash(s["change"], True)) + '</p>'
        '<p><strong>Simulated net buy-and-hold change: </strong>' +
        escape(cash(s["benchmark_change"], True)) + '</p>'
        '</section><section id="activity"><h2>Recent simulated activity</h2>'
        '<p class="help">The entry amount is shown on buys; profits after all '
        'modeled costs are shown on completed sells.</p>'
        '<div class="status-list">' + (tx or
         '<div class="home-empty"><b>No buys or sells during this period.</b>'
         '<p>Waiting for a valid trading signal is normal.</p></div>') +
        '</div><p class="inline-tip"><a href="/research/forward?name=' +
        escape(name.replace(" ", "+"), quote=True) + '&period=' + period +
        '">Open complete trade ledger →</a></p></section>')


def _balance_chart(points, allocation):
    """SVG for actual paper-hourly or live-daily observations (never interpolated)."""
    if len(points) < 2:
        return '<div class="home-empty"><b>Not enough recorded balances to draw a chart.</b></div>'
    if len(points) > 240:
        step = (len(points) - 1) / 239
        points = [points[round(i * step)] for i in range(240)]
    width, height = 720, 180
    vals = [float(x["equity"]) for x in points] + [float(allocation)]
    low, high = min(vals), max(vals)
    if high == low:
        low -= 0.01
        high += 0.01
    coords = " ".join(f"{i * width / (len(points) - 1):.1f},"
                      f"{10 + (high-float(p['equity']))/(high-low)*160:.1f}"
                      for i, p in enumerate(points))
    return ('<svg viewBox="0 0 720 180" role="img" '
            'aria-label="Recorded account equity" style="width:100%;height:auto">'
            '<polyline fill="none" stroke="var(--series-1)" stroke-width="3" '
            'points="' + coords + '"/></svg><p class="inline-tip">Actual stored balance '
            'marks only. Missing dates are not backfilled.</p>')


def standard_account_detail(conn, account_id: int) -> str:
    row = conn.execute(
        "SELECT a.*,b.genome_json,b.status FROM accounts a JOIN deployed_bots b "
        "ON a.bot_id=b.bot_id WHERE a.account_id=?", (account_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown account")
    a = dict(row)
    is_live = a["mode"] == "live"
    status = "REAL-MONEY ACCOUNT" if is_live else "STANDARD PAPER ACCOUNT"
    title = str(a["bot_id"])
    try:
        from bots.strategy.genome import Genome
        g = Genome.from_dict(json.loads(a["genome_json"]))
        pair, desc = g.pair, g.describe()
    except (ValueError, KeyError, TypeError):
        pair, desc = "Unknown", "Strategy description not available"
    if is_live:
        marks = [dict(r) for r in conn.execute(
            "SELECT ts,equity FROM equity_history WHERE account_id=? ORDER BY ts",
            (account_id,))]
    else:
        marks = [dict(r) for r in conn.execute(
            "SELECT ts,equity FROM paper_observations WHERE account_id=? ORDER BY ts",
            (account_id,))]
    fills = [dict(r) for r in conn.execute(
        "SELECT id,ts,side,qty,price,fee,pnl,reason FROM fills "
        "WHERE account_id=? AND mode=? ORDER BY id DESC LIMIT 100",
        (account_id, a["mode"]))]
    ev = paper_evidence(conn, account_id, 9_999_999_999_999) if not is_live else None
    losses = float(a["last_equity"]) - float(a["allocation"])
    risk_text = ("These are real Kraken trading account records. Money can be lost." if is_live
                 else "These are standard simulated paper records, separate from the research league.")
    latest = "".join(
        '<div class="status-row"><span>' + escape(when(f["ts"])) + ' · ' +
        escape(f["side"].upper()) + ' · ' + escape(str(f["reason"])) +
        '</span><strong>' + escape(cash(float(f["pnl"]), True)
                                  if f["pnl"] is not None else "Open / buy") +
        '</strong></div>' for f in fills[:30])
    return (
        '<div class="home-heading"><span class="home-eyebrow">' + escape(status) +
        '</span><h1>' + escape(title) + '</h1><p>' + escape(risk_text) + '</p></div>'
        '<div class="status-pill ' + ('bad' if is_live else 'good') + '">' +
        escape(status) + '</div>'
        '<section><h2>What does this bot do?</h2>'
        '<p><b>Coin:</b> ' + escape(pair) + ' · <b>Status:</b> ' +
        escape(str(a["status"])) + ' · <b>Position:</b> ' +
        ("Open" if float(a["qty"]) > 0 else "None") + '</p>'
        '<details class="rule-details"><summary>Technical strategy</summary><p>' +
        escape(desc) + '</p></details></section>'
        '<div class="perf-grid">'
        '<article class="perf-tile"><div class="perf-title">Starting allocation</div>'
        '<strong>' + escape(cash(a["allocation"])) + '</strong></article>'
        '<article class="perf-tile"><div class="perf-title">Account balance</div>'
        '<strong>' + escape(cash(a["last_equity"])) + '</strong></article>'
        '<article class="perf-tile"><div class="perf-title">Profit / loss</div>'
        '<strong>' + escape(cash(losses, True)) + '</strong></article>'
        '<article class="perf-tile"><div class="perf-title">Recorded fees</div>'
        '<strong>' + escape(cash(a["fees"])) + '</strong></article></div>'
        '<section><h2>Account balance history</h2>'
        '<p class="help">' + ("Balance recorded by day; this is a live-money account."
                              if is_live else "Observed paper-hourly account balances.") +
        '</p>' + _balance_chart(marks, a["allocation"]) +
        (('<p class="inline-tip">Paper verification: ' +
          str(ev["observed_days"]) + ' distinct observed days, ' +
          str(ev["verified_trades"]) + ' settled trades.</p>') if ev else '') +
        '</section><section><h2>Recorded trades</h2><div class="status-list">' +
        (latest or '<p class="help">No recorded fills yet.</p>') +
        '</div></section>')
