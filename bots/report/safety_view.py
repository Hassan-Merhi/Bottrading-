"""Phase 4 novice-first Safety, Settings and owner-approval screens.

Pure HTML presentation; high-risk controls remain the existing protected
/ bots / {id} / {action} POST endpoint. All dynamic content is escaped.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote


def _money(v):
    return f"${float(v):,.2f}"


def _date(ms):
    try:
        return datetime.fromtimestamp(int(ms)/1000, timezone.utc).strftime("%d %b %Y %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return "Not recorded"


def _row(name, value, note=""):
    return ('<div class="safety-info-row"><div><b>' + escape(str(name)) + '</b>' +
            ('<small>' + escape(str(note)) + '</small>' if note else '') +
            '</div><strong>' + escape(str(value)) + '</strong></div>')


def _badge(value, tone):
    return ('<span class="status-pill ' + tone + '">' + escape(value) + '</span>')


def _heading(eyebrow, title, text):
    return ('<div class="home-heading"><span class="home-eyebrow">' +
            escape(eyebrow) + '</span><h1>' + escape(title) + '</h1><p>' +
            escape(text) + '</p></div>')


def _status_card(title, value, note, tone=""):
    return ('<article class="safety-status-card"><span class="kpi-label">' +
            escape(title) + '</span><strong>' + escape(value) +
            '</strong><small>' + escape(note) + '</small>' +
            ('<div>' + _badge("Needs attention", tone) + '</div>' if tone else '') +
            '</article>')


def _notice(item):
    return ('<div class="safety-alert"><div class="safety-alert-symbol" '
            'aria-hidden="true">!</div><div><b>' +
            escape(str(item["title"])) + '</b><p>' +
            escape(str(item["explanation"])) + '</p></div></div>')


def safety_page(data: dict) -> str:
    mode = data["mode"]
    live = mode == "LIVE"
    health = data["market"]
    warnings = data["notices"]
    count = len(warnings)
    headline = _heading(
        "Safety center", "Know exactly what is protected.",
        "See real-time worker evidence, market-data quality, active trading locks "
        "and recent safety events. This is a read-only monitor.")
    hero = (
        '<section class="safety-hero ' + escape(data["level"]) + '">'
        '<div><span class="status-pill">' +
        escape("REAL MONEY MODE" if live else "FAKE MONEY MODE") + '</span>'
        '<h2>' + escape(data["headline"]) + '</h2><p>' +
        escape(data["description"]) + '</p></div>'
        '<div class="safety-hero-count"><strong>' + str(count) +
        '</strong><span>items to review</span></div></section>')
    kpis = (
        '<div class="safety-status-grid">'
        + _status_card("Worker heartbeat",
                       "Healthy" if data["worker_healthy"] else "Not confirmed",
                       ("Last observed " + _date(data["heartbeat_ms"]) if
                        data["heartbeat_ms"] else "No completed loop heartbeat"),
                       "" if data["worker_healthy"] else "warn")
        + _status_card("Market prices",
                       str(health["fresh_pairs"]) + "/" + str(health["total_pairs"]),
                       "Market pairs with fresh, gap-free closed candles",
                       "" if data["market_healthy"] else "warn")
        + _status_card("Active live-money accounts", str(data["live_accounts"]),
                       "Accounts held by the separate original risk engine",
                       "bad" if live else "")
        + _status_card("Actual live capital allocated",
                       _money(data["allocated_live_usd"]),
                       "Risk-account allocation only; not your bank balance",
                       "bad" if data["allocated_live_usd"] > 0 else "")
        + '</div>')
    alerts = (
        '<section id="warnings"><div class="section-head"><h2>Warnings and protections</h2>'
        '<span class="status-pill ' + ("warn" if count else "good") + '">' +
        str(count) + ' active</span></div>'
        '<p class="help">Warnings never clear themselves because you visit '
        'the dashboard. The existing engine decides whether any trading can proceed.</p>'
        + ("".join(_notice(w) for w in warnings) if warnings else
           '<div class="safety-empty">No active flags and the observed worker/market '
           'checks are healthy. This does not guarantee future trades or exchange fills.</div>')
        + '<p class="inline-tip">For investigation or owner-controlled recovery, '
        '<a href="/advanced#events">open the original risk log</a>. '
        'No emergency reset is available on this page.</p></section>')
    pair_list = "".join(
        '<div class="safety-pair"><b>' + escape(pair) + '</b>' +
        _badge(str(values["status"]).capitalize(),
               "good" if values["status"] == "fresh" else "warn") +
        '<span>Last closed candle: ' + _date(values.get("last_candle_ms")) +
        '</span><small>' + str(values["recent_missing_candles"]) +
        ' missing 15-minute slots in the last day</small></div>'
        for pair, values in health["pairs"].items())
    feed = (
        '<section id="feeds"><div class="section-head"><h2>Market data quality</h2>'
        '<span class="status-pill">' + str(health["fresh_pairs"]) +
        ' fresh pairs</span></div>'
        '<p class="help">Trades must be based on actual fresh market observations. '
        'Missing candles and API failures require attention; the app never fills them '
        'in just to display a healthy status.</p>'
        + ('<div class="safety-alert"><div class="safety-alert-symbol">!</div><div>'
           '<b>Latest exchange refresh error</b><p>' +
           escape(health["last_errors"]) + '</p></div></div>'
           if health["last_errors"] else '')
        + '<details class="safety-detail"><summary>See all market pairs and freshness</summary>'
        '<div class="safety-pair-grid">' + pair_list + '</div></details></section>')
    events = data["events"]
    event_html = "".join(
        '<details class="safety-event"><summary><b>' +
        escape(str(e["kind"]).replace("_", " ").capitalize()) +
        '</b><time>' + _date(e["ts"]) + '</time></summary>'
        '<p>' + escape(str(e["detail"])) + '</p></details>' for e in events)
    timeline = (
        '<section id="events"><div class="section-head"><h2>Recent risk activity</h2>'
        '<span class="status-pill">Last 30 entries</span></div>'
        '<p class="help">Original audit records, in newest-first order. '
        'A blank log does not prove that exchange reconciliation is complete.</p>'
        + (event_html if events else
           '<div class="home-empty"><b>No safety events recorded yet.</b></div>')
        + '</section>')
    return (headline + hero + kpis + alerts + feed + timeline +
            '<p class="inline-tip">Safety remains enforced by the original '
            'RiskGuard, exchange checks and owner confirmation rules.</p>')


def _setting_category(title, description, rows):
    return ('<section class="settings-category"><h2>' + escape(title) + '</h2>'
            '<p class="help">' + escape(description) + '</p>'
            '<div class="safety-info-list">' + "".join(
                _row(name, value, helptext) for name, value, helptext in rows
            ) + '</div></section>')


def settings_page(data: dict) -> str:
    c, pilot, scaling = data["settings"], data["pilot"], data["scaling"]
    headline = _heading(
        "System settings", "Your settings, in plain language.",
        "Review what the bot is configured to do. These values are read-only "
        "here, and editing the dashboard cannot enable real-money trading.")
    highlight = (
        '<section class="settings-banner"><div class="section-head">'
        '<h2>Real-money trading is ' +
        escape("configured as allowed" if c["live_enabled"] else "disabled in config") +
        '</h2>' + _badge("LIVE engine" if data["mode"] == "LIVE" else "PAPER engine",
                        "bad" if data["mode"] == "LIVE" else "good") +
        '</div><p class="help">Even when a configuration flag is enabled, real '
        'trades require the independent risk-guard mode confirmation, a qualified '
        'paper strategy, separate owner authorization and exchange checks.</p>'
        '<div class="status-list">' +
        _row("Active live-money bots", data["live_accounts"],
             "Independent live account records, not research experiments") +
        _row("Currently allocated live capital", _money(data["allocated_live_usd"]),
             "Not an estimate of your exchange bank balance") +
        _row("Hard application ceiling", _money(data["hard_ceiling_usd"]),
             "Risk engine enforces this fixed maximum") + '</div></section>')
    risk = _setting_category(
        "Real-money limits", "Existing caps. No settings on this screen can raise them.", [
            ("Maximum total live capital", _money(c["max_total_live_usd"]),
             "0 means no budget is assigned for live orders"),
            ("Maximum live capital per bot", _money(c["max_per_live_bot_usd"]),
             "Also subject to pilot and global caps"),
            ("Maximum simultaneous live bots", c["max_live_bots"],
             "Original guard refuses excess active live bots"),
            ("Daily live loss threshold", _money(c["daily_loss_limit_usd"]),
             "A daily halt stops new live entries"),
            ("Total loss emergency stop", _money(c["total_drawdown_kill_usd"]),
             "Requires a fresh owner confirmation after activation")])
    pilot_text = ("Off" if not c["pilot_enabled"] else
                  "Configured on — independent qualification still required")
    pilot_card = _setting_category(
        "Live pilot and scaling", "Both require separate evidence and owner decisions.", [
            ("Funded pilot", pilot_text, "No experimental lab bot qualifies automatically"),
            ("Owner pilot signoff", "Recorded" if c["pilot_owner_signoff"] else "Missing",
             "Must be explicitly authorized outside this page"),
            ("Exchange pilot verification", "Recorded" if c["pilot_exchange_signoff"] else "Missing",
             "Kraken permissions, stops and reconciliation need separate checks"),
            ("Pilot maximum capital", _money(c["pilot_max_total_usd"]),
             "A zero cap blocks any funded pilot"),
            ("Automatic scaling", "Disabled / not permitted",
             "Even if scaling review is configured, reallocations need a new owner action")])
    pilot_blockers = list(pilot.get("configuration_blockers") or [])
    pilot_card += (
        '<section><h2>Why the pilot cannot start automatically</h2>' +
        ('<ul class="settings-blockers">' + "".join('<li>' + escape(str(b)) +
         '</li>' for b in pilot_blockers[:12]) + '</ul>' if pilot_blockers else
         '<p class="help">Configuration checks passed, but historical/forward proof, '
         'separate owner action, risk confirmation and the exchange still determine '
         'whether a real position may open.</p>') +
        '<p class="inline-tip">Scaling review: ' +
        escape(str(scaling.get("status", "blocked")).replace("_", " ")) +
        '. Review eligibility is not permission to allocate money.</p></section>')
    sim = _setting_category(
        "Fake-money research and simulation", "These settings never directly fund a live bot.", [
            ("Research league", "Enabled" if c["research_league_enabled"] else "Disabled",
             "Independent research experiments, no qualification shortcut"),
            ("Fake capital per research bot", _money(c["research_allocation_usd"]),
             "Simulated dollars; not a bank deposit"),
            ("Standard paper capital per bot", _money(c["paper_allocation_usd"]),
             "Independent protected paper pipeline"),
            ("Maximum standard paper bots", c["paper_max_bots"],
             "Separate from the 3 research experiments"),
            ("Public research collection", "Enabled" if c["research_enabled"] else "Disabled",
             "Read-only public information, not trade instructions")])
    prices = _setting_category(
        "Realistic execution costs", "Already included in simulated calculations.", [
            ("Entry execution", str(c["entry_order"]).upper(),
             "Market entries pay taker fees; limit orders may never fill"),
            ("Modeled taker fee", f'{float(c["fee_taker_pct"]):.2f}%',
             "Per market fill, separate for buying and selling"),
            ("Modeled full spread", f'{float(c["spread_bps"]):g} basis points',
             "Full bid-ask spread in the simulator"),
            ("Modeled slippage", f'{float(c["slippage_pct"]):.2f}%',
             "Estimated additional cost of a fill")])
    gates = _setting_category(
        "Paper qualification", "All conditions remain enforced by the existing engine.", [
            ("Minimum elapsed and witnessed span", str(c["graduation_days"]) + " days",
             "A simulated backtest cannot replace observed paper trading"),
            ("Minimum independent observed days", str(c["graduation_observed_days"]),
             "Distinct UTC days with witnessed market quotes"),
            ("Minimum verified completed trades", str(c["graduation_closed_trades"]),
             "Actual recorded paper buy/sell round trips"),
            ("Owner approval", "Required",
             "A review click does not itself unlock live mode")])
    jobs = _setting_category(
        "Schedules and worker", "Times are in UTC; no schedule is edited here.", [
            ("Trading worker polling", str(c["poll_seconds"]) + " seconds",
             "Worker heartbeat still must be recent and valid"),
            ("Public market refresh", str(c["market_refresh_seconds"]) + " seconds",
             "Missing candles are never counted as healthy"),
            ("Historical evolution", str(c["evolution_hour_utc"]).zfill(2) + ":00 UTC",
             "Historical strategy optimization"),
            ("Daily analyst briefing",
             str(c["analyst_hour_utc"]).zfill(2) + ":00 UTC"
             if c["analyst_hour_utc"] is not None else "Not configured",
             "Read-only informational research")])
    ending = ('<section><h2>How do I change a setting?</h2>'
              '<p>Changes to risk, pilot, research or trading configuration are made '
              'through the existing reviewed deployment workflow, not by clicking a '
              'toggle here. Real-money funding requires a separate authorization '
              'and the existing guard must agree.</p>'
              '<p><a href="/advanced#limits">View the exact technical limits →</a>'
              ' · <a href="/approvals">Review pending approvals →</a></p></section>')
    return headline + highlight + risk + pilot_card + sim + prices + gates + jobs + ending


def approval_page(data: dict) -> str:
    pending = data["approvals"]
    enabled = data["approval_token_configured"]
    headline = _heading(
        "Owner approval center", "Review before approving anything.",
        "A historically qualified standard-paper strategy can request a decision. "
        "This does NOT apply to the three unqualified research experiments.")
    lead = ('<section class="approval-intro"><div class="section-head">'
            '<h2>Approvals waiting for you</h2><span class="status-pill">' +
            str(len(pending)) + ' pending</span></div>'
            '<p>Approving is a serious decision: it may allow that standard bot '
            'to trade real money later, but ONLY if every other owner, pilot, '
            'exchange and capital risk gate is independently satisfied. '
            'It never turns live mode on by itself.</p>'
            '<p class="help">' +
            ('Dashboard approvals require your existing DASHBOARD_TOKEN.'
             if enabled else
             'Dashboard token is not configured. Review here, then use your '
             'existing authenticated Telegram owner approval workflow.') +
            '</p></section>')
    cards = []
    for b in pending:
        bid = str(b["bot_id"])
        safe_id = quote(bid, safe="")
        if enabled:
            buttons = (
                '<form class="approval-form" method="post" '
                'action="/bots/' + escape(safe_id, quote=True) + '/approve">'
                '<label>Owner dashboard password'
                '<input type="password" name="token" required autocomplete="current-password" '
                'placeholder="Enter dashboard approval token"></label>'
                '<div class="approval-actions">'
                '<button class="primary" type="submit" data-confirm="approve">Approve this bot</button>'
                '<button class="danger" type="submit" formaction="/bots/' +
                escape(safe_id, quote=True) + '/decline" data-confirm="decline">'
                'Decline this bot</button></div></form>')
        else:
            buttons = ('<div class="safety-empty">Actions unavailable on this website. '
                       'Use the owner-authorized Telegram decision process; '
                       'no token has been configured here.</div>')
        pnl = (b["paper_equity"]-b["paper_allocation"]
               if b["paper_equity"] is not None and
               b["paper_allocation"] is not None else None)
        cards.append(
            '<article class="approval-card"><div class="section-head"><h3>' +
            escape(bid) + '</h3>' + _badge("Pending human review", "warn") +
            '</div><p class="help">Coin: ' + escape(str(b["pair"])) + '</p>'
            '<p>' + escape(str(b["strategy"])) + '</p>'
            '<div class="approval-metrics">' +
            _row("Observed paper days", str(b["observed_days"])) +
            _row("Verified closed round trips", str(b["paper_closed_trades"])) +
            _row("Total paper P&L", (_money(pnl) if pnl is not None else
                                    "No account evidence available")) +
            '</div>'
            '<p class="help">These values are evidence summaries, not an independent '
            'guarantee that the bot is safe. All separate live checks still apply.</p>'
            + buttons + '</article>')
    block = (
        '<section id="pending"><h2>Pending decisions</h2>' +
        ('<div class="approval-card-grid">' + "".join(cards) + '</div>' if cards else
         '<div class="home-empty"><b>No approval requests right now.</b>'
         '<p>Research bots never appear here. Standard paper bots only reach '
         'this queue after the original qualification process.</p></div>') + '</section>')
    policy = (
        '<section id="policy"><h2>What happens after approval?</h2>'
        '<div class="learning-funnel">'
        '<div class="learn-step"><span class="step-icon">1</span><b>Verify paper results</b>'
        '<span class="help">Historical test + genuine forward trading requirements.</span></div>'
        '<div class="learn-step"><span class="step-icon">2</span><b>Owner decision</b>'
        '<span class="help">Your protected approve or decline action is recorded.</span></div>'
        '<div class="learn-step"><span class="step-icon">3</span><b>Separate live guard</b>'
        '<span class="help">Exchange verification, pilot, cap and live confirmation '
        'still must pass.</span></div></div>'
        '<p class="inline-tip"><a href="/safety">Check current safety status →</a> '
        '· <a href="/advanced#bots">View original technical bot queue →</a></p></section>')
    return headline + lead + block + policy
