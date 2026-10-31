"""Clear default dashboard, backed entirely by existing verified state.

Phase 1: readable summary and navigation only; no change to orders or risk.
"""
from __future__ import annotations

from html import escape
import sqlite3

from bots.data.health import market_health
from bots.paper.league import snapshot as lab_snapshot
from bots.paper.forward_history import forward_history
from bots.report.daily import summary
from bots.report.forward_page import equity_chart
from bots.risk.state import (CRASH_LOCK, DAILY_HALT_UNTIL, ENTRY_HALT,
                             HEARTBEAT, KILL_SWITCH, OWNER_PAUSE, RiskState)


def _usd(value, signed=False):
    value = float(value)
    return f"${value:+,.2f}" if signed else f"${value:,.2f}"


def _kpi(title, value, meaning, tooltip, tone=""):
    return (
        '<article class="kpi-card"><div class="kpi-head">'
        '<span class="kpi-label">' + escape(title) + '</span>'
        '<span class="info-dot" aria-label="' + escape(tooltip, quote=True) +
        '" title="' + escape(tooltip, quote=True) + '">?</span></div>'
        '<div class="kpi-value ' + escape(tone) + '">' + escape(value) + '</div>'
        '<div class="kpi-foot">' + escape(meaning) + '</div></article>'
    )


def _step(n, title, text):
    return ('<li><span class="step-icon">' + str(n) + '</span>'
            '<div><b>' + escape(title) + '</b><span>' + escape(text) +
            '</span></div></li>')


def _pill(text, cls):
    return '<span class="status-pill ' + escape(cls) + '">' + escape(text) + '</span>'


def _experiment(bot, feed_ok):
    name = str(bot["name"])
    pair = str(bot["pair"])
    pos = bool(bot.get("in_position", False))
    trade_count = int(bot.get("trades", 0))
    if not feed_ok:
        state, css = "Waiting for healthy market data", "warning"
    elif pos:
        state, css = "Holding a simulated position", "holding"
    elif trade_count:
        state, css = "Looking for the next opportunity", ""
    else:
        state, css = "Watching prices — no trades yet", ""
    pnl = float(bot.get("pnl", 0))
    return (
        '<article class="experiment">'
        '<div class="experiment-name">' + escape(name) + '</div>'
        '<div class="experiment-pair">' + escape(pair) + '</div>'
        '<div class="kpi-label">Current fake-money value</div>'
        '<div class="experiment-balance">' + escape(_usd(bot["equity"])) + '</div>'
        '<div class="experiment-pnl ' + ('pos' if pnl > 0 else 'neg' if pnl < 0 else '') +
        '">' + escape(_usd(pnl, True)) + ' after estimated costs</div>'
        '<div class="experiment-state ' + css + '">' + escape(state) + '</div>'
        '<div class="inline-tip">' + str(trade_count) + ' completed simulated trade' +
        ('s' if trade_count != 1 else '') + '</div>'
        '<a class="bot-detail-link" href="/bots/research/' +
        escape(name.casefold().replace(" ", "-"), quote=True) +
        '">See what this bot is doing →</a></article>'
    )


def home_dashboard(conn: sqlite3.Connection, cfg: dict, now: int) -> str:
    s = summary(conn, now)
    league = lab_snapshot(conn, cfg)
    hist = forward_history(conn, cfg)
    st = RiskState(conn)
    market = market_health(conn, cfg, now)
    count = int(market.get("fresh_pairs", 0))
    total = int(market.get("total_pairs", 0))
    feed_ok = bool(market.get("healthy")) and not bool(market.get("last_errors"))
    heartbeat = st.get(HEARTBEAT)
    alive = False
    try:
        # More conservative than the legacy 3-hour UI flag.
        alive = bool(heartbeat) and 0 <= now - int(heartbeat) <= 3 * 60 * 1000 + 120_000
    except (TypeError, ValueError):
        pass
    paused = bool(st.get(OWNER_PAUSE) or st.get(ENTRY_HALT))
    live = s.get("mode") == "LIVE"
    if live:
        hero_cls = "live"
        hero_tag = "Real-money trading mode"
        hero_title = "Real-money trading is enabled"
        hero_explain = ("This is not a fake-money simulation. Positions can lose real money. "
                        "Review your current limits and orders immediately.")
    elif not alive or not feed_ok:
        hero_cls = "attention"
        hero_tag = "Needs attention"
        hero_title = "The trading system needs a check"
        hero_explain = ("The worker or market feed is not healthy enough to confirm normal "
                        "operation. No paper profits are assumed while observations are missing.")
    elif paused:
        hero_cls = "attention"
        hero_tag = "New entries paused"
        hero_title = "The bots are monitoring, but cannot enter new trades"
        hero_explain = "An owner pause or a risk guard stopped new entries. Existing exits remain handled."
    else:
        hero_cls = ""
        hero_tag = "Running in research mode"
        hero_title = "Your bots are watching the market"
        hero_explain = ("Prices are updating. They only make fake-money trades when "
                        "their strategies find a suitable opportunity.")

    experimental = league.get("bots", [])
    capital = float(league.get("capital", 0))
    profit = float(league.get("pnl", 0))
    number = len(experimental)
    trades = int(league.get("completed_trades", 0))
    simulated_real = conn.execute(
        "SELECT COALESCE(SUM(allocation),0) FROM accounts WHERE mode='live' AND active=1"
    ).fetchone()[0]
    real_capital = float(simulated_real)
    if real_capital < 0:
        real_capital = 0.0
    # Count distinct experiment types without folding qualified paper/real records into
    # the experimental money KPI. Those are separate, protected ledgers.
    qualified_count = int(s.get("paper_bots", 0))
    live_count = int(s.get("live_bots", 0))
    waiting = conn.execute("SELECT COUNT(*) FROM deployed_bots WHERE "
                           "status='ready_to_graduate'").fetchone()[0]
    if not experimental:
        bot_cards = ('<div class="home-empty"><b>No experimental bots are running yet</b>'
                     '<p>Experiments initialize after enough fresh market data is available.</p>'
                     '</div>')
    else:
        bot_cards = '<div class="experiment-grid">' + "".join(
            _experiment(bot, feed_ok and alive) for bot in experimental) + '</div>'
    last = hist["observations"][-1] if hist["observations"] else None
    verified = int(hist.get("stats", {}).get("observed_hours", 0))
    if last and last.get("observed_ms") is not None and verified:
        evidence_line = f"{verified} hour(s) of time-stamped forward observations recorded."
    elif hist["observations"]:
        evidence_line = ("Starting forward tracking. Earlier balance snapshots have no verified "
                         "observation timestamp and are not counted as fresh evidence.")
    else:
        evidence_line = "Waiting for the first observed hourly mark; no historical data is invented."
    progress_note = (
        "A result appears only after a simulated position is closed."
        if trades == 0 else
        f"{trades} simulated round trip(s) have finished. Profits include modeled costs.")
    updates = [
        ("Market data", f"{count}/{total} market pairs ready" if feed_ok
         else "Market feed needs attention", "good" if feed_ok else "warn"),
        ("Paper trading", "Running" if alive and not paused else
         "Paused" if paused else "Worker not confirmed", "good" if alive and not paused else "warn"),
        ("Real-money orders", f"{live_count} active live bot(s)" if live_count
         else "Disabled — no active live bots", "bad" if live_count else "good"),
    ]
    alert_items = []
    if st.get(CRASH_LOCK) and not live:
        alert_items.append(("Real trading restart lock", "Real trading needs owner re-approval after a restart. "
                            "This does not stop fake-money experiments."))
    if st.get(KILL_SWITCH):
        alert_items.append(("Real trading emergency stop", "The live kill switch is active. "
                            "Check the risk log before changing anything."))
    if st.get(DAILY_HALT_UNTIL):
        alert_items.append(("Trading loss limit", "A risk limit is recorded. "
                            "Check the safety page for the exact timing."))
    notice = ""
    if alert_items:
        notice = ('<div class="advanced-note"><b>Safety notices:</b> ' +
                  " ".join(escape(a + ": " + b) for a, b in alert_items) +
                  ' <a href="/advanced#status">View details</a></div>')
    return (
        '<div class="home-page">'
        '<div class="home-heading"><span class="home-eyebrow">Your trading at a glance</span>'
        '<h1>Know what your bots are doing.</h1>'
        '<p>No complicated figures or confusing strategy terms. See whether the system '
        'is healthy, how the fake-money experiments perform, and what comes next.</p></div>'
        '<section id="status" class="home-hero ' + hero_cls + '">'
        '<div><span class="hero-status"><span class="dot" aria-hidden="true"></span>' +
        escape(hero_tag) + '</span><h2>' + escape(hero_title) + '</h2><p>' +
        escape(hero_explain) + '</p></div>'
        '<div class="hero-ministats">'
        '<div><span>Live data feeds</span><strong>' + str(count) + '/' + str(total) +
        '</strong></div><div><span>Experimental accounts</span><strong>' +
        str(number) + '</strong></div>'
        '<div><span>Completed paper trades</span><strong>' + str(trades) +
        '</strong></div><div><span>Live money allocated</span><strong>' +
        escape(_usd(real_capital)) + '</strong></div></div></section>'
        '<div class="kpi-grid" aria-label="Important performance numbers">'
        + _kpi("Fake-money starting balance", _usd(capital),
               f"{number} independent experimental account(s)",
               "Total starting funds for research-only paper experiments. No bank money is deposited.")
        + _kpi("Experimental net profit / loss", _usd(profit, True),
               "After modeled spread, slippage and fees",
               "Current simulated value minus original fake balance. This is not money you can withdraw.",
               "pos" if profit > 0 else "neg" if profit < 0 else "")
        + _kpi("Trades completed", str(trades),
               "Full buy-and-sell round trips only",
               "A trade counts only when the paper bot has bought and later sold its simulated position.")
        + _kpi("Real money at risk", _usd(real_capital),
               "Allocated to active real-money bots",
               "Reported active live allocations, NOT your bank balance. Real money is at risk only in LIVE mode.",
               "neg" if real_capital else "pos")
        + '</div>'
        + notice
        + '<div class="home-two-col">'
        '<section id="profit-chart"><div class="section-head"><h2>Is it making money?</h2>'
        '<a class="text-link" href="/research/forward">View detailed performance →</a></div>'
        '<p class="help">Experimental profit after costs versus simply buying and holding '
        'the coins. Neither line represents real-money earnings.</p>'
        '<div class="home-chart">' + equity_chart(hist["observations"]) + '</div>'
        '<p class="inline-tip">' + escape(evidence_line) + '</p></section>'
        '<section id="next"><div class="section-head"><h2>What happens next?</h2></div>'
        '<ul class="next-steps">'
        + _step(1, "Watch live market prices",
                "The bot checks real Kraken prices and waits for complete candles.")
        + _step(2, "Wait for a strategy signal",
                "The bot trades only when a predefined rule allows it.")
        + _step(3, "Make a fake-money trade",
                "Any simulated buy and sell is recorded with modeled costs.")
        + _step(4, "Review the actual outcome",
                "Completed simulated trades appear in performance history.")
        + '</ul><div class="advanced-note">' + escape(progress_note) + '</div></section></div>'
        '<section id="my-bots"><div class="section-head"><h2>Your research bots</h2>'
        '<a class="text-link" href="/bots">View all bot details →</a></div>'
        '<p class="help">These experiments do not qualify automatically for real-money '
        'trading. Profit can be positive or negative.</p>' + bot_cards
        + (f'<p class="inline-tip">Separately, {qualified_count} standard-pipeline '
           f'paper account(s), {waiting} approval request(s) and {live_count} live bot(s) '
           'are tracked in the advanced pipeline.</p>' if (qualified_count or waiting or live_count)
           else '<p class="inline-tip">There are no approved or live bots. '
                'Research bots never bypass the strict qualification process.</p>')
        + '</section>'
        '<section id="health"><div class="section-head"><h2>Is everything working?</h2>'
        '<a class="text-link" href="/advanced#status">Full system details →</a></div>'
        '<div class="status-list">'
        + "".join('<div class="status-row"><span>' + escape(k) +
                  '</span><strong>' + _pill(v, tone) + '</strong></div>'
                  for k, v, tone in updates)
        + '</div></section>'
        '</div>'
    )
