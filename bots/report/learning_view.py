"""Explain learning, source collection and rejected historical strategies plainly.

Render stored records only; text from Google and strategy comments is escaped.
All performance metrics here are TRAINING/TEST statistics, not trading P&L.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import math
from urllib.parse import urlencode


def _num(n, nd=0):
    return f"{float(n):,.{nd}f}"


def _time_ms(ts):
    if ts is None:
        return "No research recorded yet"
    try:
        return datetime.fromtimestamp(int(ts) / 1000, timezone.utc).strftime("%d %b %Y at %H:%M UTC")
    except (ValueError, TypeError, OverflowError):
        return "Unknown time"


def _percent(v):
    try:
        if v is None or not math.isfinite(float(v)):
            return "—"
        return f"{float(v)*100:+.1f}%"
    except (TypeError, ValueError):
        return "—"


def _stat(title, value, description):
    return ('<div class="learning-kpi"><span class="kpi-label">' + escape(title) +
            '</span><strong>' + escape(str(value)) + '</strong><small>' +
            escape(description) + '</small></div>')


def _generation_chart(generations):
    """SVG of actual historical training fitness, not observed trading return."""
    if len(generations) < 2:
        return ('<div class="home-empty"><b>Not enough finished evolution data to draw a trend.</b>'
                '<p>The first two recorded generations will create this chart.</p></div>')
    points = generations[-100:]
    values = [float(g[key]) for g in points for key in ("best", "median", "worst")
              if math.isfinite(float(g[key]))]
    if not values:
        return '<p class="help">No numeric fitness observations are available.</p>'
    lo, hi = min(values), max(values)
    if hi - lo < 0.00001:
        hi, lo = hi + 0.1, lo - 0.1
    W, H, left, right, top, bottom = 740, 235, 20, 20, 12, 28
    def coord(i, v):
        x = left + i * (W-left-right) / max(1, len(points)-1)
        y = top + (hi-float(v)) / (hi-lo) * (H-top-bottom)
        return f"{x:.2f},{y:.2f}"
    lines = []
    for key, var in (("best", "--series-1"), ("median", "--series-3"),
                     ("worst", "--series-2")):
        coords = " ".join(coord(i, g[key]) for i, g in enumerate(points))
        lines.append('<polyline points="' + coords +
                     '" fill="none" stroke="var(' + var +
                     ')" stroke-width="2.5" stroke-linecap="round"/>')
    return (
        '<div class="legend"><span><i style="background:var(--series-1)"></i>Best</span>'
        '<span><i style="background:var(--series-3)"></i>Middle</span>'
        '<span><i style="background:var(--series-2)"></i>Worst</span></div>'
        '<svg viewBox="0 0 740 235" role="img" style="width:100%;height:auto" '
        'aria-label="Training fitness trends across recorded evolution generations">'
        + "".join(lines) + '<text x="20" y="229" font-size="12" fill="var(--ink-2)">'
        'Generation ' + escape(str(points[0]["generation"])) + '</text>'
        '<text x="720" y="229" text-anchor="end" font-size="12" fill="var(--ink-2)">'
        + escape(str(points[-1]["generation"])) + '</text></svg>'
        '<p class="help">Training fitness is an internal comparison score, not dollars, '
        'observed profit, or evidence of future returns.</p>')


def _failure_bars(data, path="/graveyard", scope="all"):
    categories = data["failure_categories"]
    maximum = max((int(item["count"]) for item in categories), default=0)
    if maximum <= 0:
        return '<div class="home-empty"><b>No strategy rejections have been recorded.</b></div>'
    rows = []
    total = sum(int(x["count"]) for x in categories)
    for c in categories:
        count = int(c["count"])
        if not count:
            continue
        width = count / maximum * 100
        href = path + "?" + urlencode({"scope": scope, "category": c["key"]})
        rows.append(
            '<a class="failure-bar-row" href="' + escape(href, quote=True) + '">'
            '<span class="failure-bar-label">' + escape(c["label"]) + '</span>'
            '<span class="failure-bar-track"><i style="width:' + f'{width:.2f}' +
            '%"></i></span><strong>' + str(count) + ' <small>(' +
            f'{count / total * 100:.0f}' + '%)</small></strong></a>')
    return '<div class="failure-chart">' + "".join(rows) + '</div>'


def _top_strategies(data):
    items = data["leaderboard"]
    if not items:
        return ('<div class="home-empty"><b>No finished generation leaderboard yet.</b>'
                '<p>Ranked historical strategies appear once an evolution run ends.</p></div>')
    cards = []
    for row in items[:5]:
        cards.append(
            '<div class="learning-strategy"><div class="learn-strategy-head">'
            '<b>#' + str(row["rank"]) + ' · ' + escape(row["pair"]) + '</b>'
            '<span class="status-pill">Historical training only</span></div>'
            '<p>' + escape(row["description"]) + '</p>'
            '<div class="learn-metrics"><span>Training return <b>' +
            escape(_percent(row["return"])) + '</b></span>'
            '<span>Buy &amp; hold <b>' + escape(_percent(row["hold_return"])) + '</b></span>'
            '<span>Worst drawdown <b>' +
            escape(_percent(row["max_drawdown"])) + '</b></span>'
            '<span>Backtest trades <b>' +
            escape(str(row["trades"] if row["trades"] is not None else "—")) +
            '</b></span></div><details><summary>Technical fitness</summary>'
            '<p>' + escape(_num(row["fitness"], 4)) +
            ' · Fitness is an internal selection score, not a return.</p>'
            '</details></div>')
    return '<div class="learn-strategy-list">' + "".join(cards) + '</div>'


def _funnel(data):
    stages = [
        ("1", "Last evolution", str(data["generation_count"]) + " generations",
         "Repeated strategy training iterations; not unique trades or people."),
        ("2", "Unseen-data evaluations", str(data["candidate_evaluations"]) + " genomes",
         "Unique strategies given a separate historical holdout assessment."),
        ("3", "Passed that historical test", str(data["candidate_passed_test"]) + " genomes",
         "Passing a backtest is not evidence that live trading makes money."),
        ("4", "Standard paper accounts", str(data["deployed_states"].get("paper", 0)) + " bots",
         "Bots that still need actual observed paper sessions."),
        ("5", "Ready / approved", str(data["deployed_states"].get("ready_to_graduate", 0) +
                                     data["deployed_states"].get("approved", 0)) + " bots",
         "Separate owner signoff and live-pilot safeguards still apply."),
    ]
    return '<div class="learning-funnel">' + "".join(
        '<div class="learn-step"><span class="step-icon">' + number +
        '</span><div><b>' + escape(name) + '</b>'
        '<span class="help">' + escape(explain) + '</span></div>'
        '<strong>' + escape(value) + '</strong></div>'
        for number, name, value, explain in stages) + '</div>'


def learning_page(data: dict, sources: dict) -> str:
    run = data["latest_run"]
    r = data["research"]
    latest = r["last_review"]
    completed_label = str(data["generation_count"]) if run else "0"
    headings = (
        '<div class="home-heading"><span class="home-eyebrow">Learning &amp; Research</span>'
        '<h1>See what the bots are exploring.</h1><p>Understand what was tested, '
        'what was rejected and what market information was collected. '
        'Learning does not guarantee future profit.</p></div>'
        '<div class="learn-warning"><b>Three separate kinds of evidence:</b> '
        '<span>Historical optimization tests strategy ideas. News research '
        'collects public reading material. Genuine paper trades are tracked '
        'separately in <a href="/research/forward">Performance</a>.</span></div>')
    kpis = (
        '<div class="learning-kpi-grid">'
        + _stat("Generations in latest run", completed_label,
                "Rounds of historical optimization, not completed trades")
        + _stat("Unique genomes tested", data["candidate_evaluations"],
                "Candidates evaluated on held-out historical data")
        + _stat("Passed unseen-data test", data["candidate_passed_test"],
                "Not automatically approved for live trading")
        + _stat("Recorded rejection events", data["total_failure_records"],
                "A strategy can generate multiple failure records")
        + _stat("Saved public sources", r["stored_sources"],
                "Source links, not validated trade signals")
        + '</div>')
    status = (
        '<section id="learning-overview"><div class="section-head"><h2>How strategies progress</h2>'
        '<a class="text-link" href="/advanced#pipeline">Technical pipeline →</a></div>'
        '<p class="help">These are different units, not a conversion funnel of '
        'one group of bots. Historical training, test passes and ongoing paper '
        'trading are independent safeguards.</p>' + _funnel(data)
        + ('<p class="inline-tip">Last completed run: ' +
           escape(str(run["run_id"])) + ' · ' + escape(str(run["data_label"])) +
           ' · finished ' + escape(str(run["finished_at"])) + '</p>' if run else
           '<p class="inline-tip">No evolution run has finished yet.</p>')
        + '</section>')
    evolution = (
        '<section id="evolution"><div class="section-head"><h2>Evolution over time</h2>'
        '<a class="text-link" href="/advanced#evolution">Advanced metrics →</a></div>'
        '<p class="help">This chart shows how training scores changed across '
        'recorded generations. It does NOT show profit from actual trades.</p>'
        + _generation_chart(data["generations"])
        + '<details class="rule-details"><summary>What is a generation?</summary>'
        '<p>A generation is a group of historical strategy experiments. '
        'The scoring system compares them on past market data and keeps '
        'promising candidates for further testing. An improving score on '
        'old data does not mean the strategy will make money in the future.</p>'
        '</details><h3 style="margin:20px 0 10px">Leading historical candidates</h3>'
        + _top_strategies(data) + '</section>')
    why = (
        '<section id="failures"><div class="section-head"><h2>Why strategies failed</h2>'
        '<a class="text-link" href="/graveyard">Explore failure records →</a></div>'
        '<p class="help">Failures are grouped by keywords in the original diagnosis. '
        'The labels are explanatory; exact historical causes are always retained. '
        'Counts are recorded failure events, not unique bots.</p>'
        + _failure_bars(data) +
        '<p class="inline-tip">Latest evolution run: ' +
        str(data["latest_run_failure_records"]) + ' recorded failures; all runs: ' +
        str(data["total_failure_records"]) +
        '.</p></section>')
    notes = latest["notes"] if latest else []
    notes_markup = ('<ul class="research-notes">' +
                    "".join('<li>' + escape(note) + '</li>' for note in notes) +
                    '</ul>') if notes else (
                    '<div class="home-empty"><b>No research feedback collected yet.</b>'
                    '<p>Insights appear after the scheduled public-news collection.</p></div>')
    research = (
        '<section id="research"><div class="section-head"><h2>Market research</h2>'
        '<a class="text-link" href="/research/sources">Browse all public sources →</a></div>'
        '<p class="help">The system collects Google News links and limited context. '
        'They are not independently fact-checked and they cannot automatically '
        'edit code, tune risk limits or place orders. '
        'A source headline is not a verified prediction.</p>'
        '<div class="pill-line"><span class="status-pill ' +
        ('good' if r["enabled"] else 'warn') + '">' +
        ('News collection enabled' if r["enabled"] else 'Research disabled') +
        '</span><span class="status-pill">No autonomous trading from news</span></div>'
        '<p class="inline-tip"><b>Last collection:</b> ' +
        escape(_time_ms(latest["observed_ms"] if latest else None)) + '</p>'
        '<h3 style="margin:18px 0 10px">What the research log noted</h3>'
        + notes_markup + '<h3 style="margin:18px 0 10px">Recent reading</h3>'
        + source_cards(sources["items"][:6])
        + '<p class="inline-tip"><a href="/research/sources">Search all saved sources →</a></p>'
        '</section>')
    education = (
        '<section id="help-learning"><h2>What these results really mean</h2>'
        '<div class="learn-faq"><details><summary>Has the AI proved it can make money?</summary>'
        '<p>No. Historical scores, news collection and strategy selection are '
        'not forward-trading proof. Check real observed simulated outcomes on '
        'the Performance page.</p></details>'
        '<details><summary>Does reading Google mean the bot changes its trading rules?</summary>'
        '<p>No. Public research is read-only. Strategy evolution is a separate '
        'predefined process with independent validation and risk controls.</p></details>'
        '<details><summary>Can a winning backtest trade real money immediately?</summary>'
        '<p>No. Any regular bot must meet forward-paper requirements and '
        'separate owner and live-pilot approvals. Research experiments cannot '
        'automatically become live bots.</p></details></div></section>')
    return headings + kpis + status + evolution + why + research + education


def source_cards(items):
    if not items:
        return ('<div class="home-empty"><b>No matching source records.</b>'
                '<p>Try a different source or topic, or wait for collection.</p></div>')
    rows = []
    for item in items:
        title, author, url = str(item["title"])[:250], str(item["source"])[:140], item["url"]
        link = ('<a href="' + escape(url, quote=True) +
                '" target="_blank" rel="noopener noreferrer nofollow">' +
                escape(title) + ' ↗</a>' if url else
                '<strong>' + escape(title) + ' (unsafe link omitted)</strong>')
        rows.append('<article class="research-card"><div class="research-source">' +
                    escape(author) + ' · ' + escape(_time_ms(item["observed_ms"])) +
                    '</div><h3>' + link + '</h3><p>' +
                    escape(str(item.get("query", ""))[:180]) + '</p>' +
                    ('<details><summary>Search excerpt</summary><p>' +
                     escape(str(item["snippet"])[:500]) + '</p></details>' if
                     item.get("snippet") else '') + '</article>')
    return '<div class="research-cards">' + "".join(rows) + '</div>'


def source_page(data):
    query = data["search"]
    source = data["source"]
    form = (
        '<div class="home-heading"><span class="home-eyebrow">Saved public reading</span>'
        '<h1>Where the research comes from.</h1>'
        '<p>Browse source headlines and links saved during scheduled research. '
        'Some articles may be inaccurate or outdated; the bot does not '
        'act on their claims.</p></div>'
        '<section><form class="perf-filters" method="get" action="/research/sources">'
        '<label>Topic or headline<input type="search" name="search" maxlength="100" '
        'value="' + escape(query, quote=True) + '" placeholder="Bitcoin, fees, volatility…"></label>'
        '<label>Publisher<select name="source">'
        '<option value="">All sources</option>' +
        "".join('<option value="' + escape(item, quote=True) + '"' +
                (' selected' if source == item else '') + '>' + escape(item) +
                '</option>' for item in data["sources"]) +
        '</select></label><button type="submit">Search sources</button>'
        '<a class="text-link" href="/research/sources">Clear</a></form>'
        '<p class="help">' + str(data["total"]) + ' saved sources match your query. '
        'Search uses stored material only, not a live Google request.</p>' +
        source_cards(data["items"]) +
        paging("/research/sources", data["offset"], data["limit"], data["total"],
               search=query, source=source) + '</section>')
    return form


def paging(path, offset, limit, total, **args):
    links = []
    if offset:
        links.append(('< Previous', max(0, offset-limit)))
    if offset+limit < total:
        links.append(('Next >', offset+limit))
    if not links:
        return ""
    return '<nav class="learn-pages" aria-label="Pagination">' + "".join(
        '<a href="' + escape(path + '?' + urlencode({**args, 'offset': pos}),
                             quote=True) + '">' + escape(label) + '</a>'
        for label, pos in links) + '</nav>'


def failure_page(data, listing):
    scope, category = listing["scope"], listing["category"]
    cats = [("all", "All reasons")] + [
        (x["key"], x["label"]) for x in data["failure_categories"]]
    scope_nav = ('<div class="period-tabs"><a href="/graveyard?scope=all"' +
                 (' aria-current="page"' if scope == "all" else '') +
                 '>All runs</a><a href="/graveyard?scope=latest"' +
                 (' aria-current="page"' if scope == "latest" else '') +
                 '>Latest finished run</a></div>')
    picks = '<nav class="learn-category-nav" aria-label="Failure reason filter">' + "".join(
        '<a href="' + escape('/graveyard?' +
             urlencode({"scope": scope, "category": k}), quote=True) + '"' +
        (' aria-current="page"' if k == category else '') +
        '>' + escape(label) + '</a>'
        for k, label in cats) + '</nav>'
    entries = []
    for row in listing["items"]:
        entries.append(
            '<details class="failure-record"><summary><span><b>' +
            escape(str(row["diagnosis"])[:250]) + '</b><small>Run ' +
            escape(str(row["run_id"])) + ' · generation ' +
            str(row["generation"]) + '</small></span><span class="status-pill">' +
            escape(str(row["cause"])) + '</span></summary>'
            '<p><b>Original diagnosis:</b> ' + escape(str(row["diagnosis"])) + '</p>'
            '<p><b>Strategy:</b> ' + escape(str(row["strategy"])) + '</p>'
            '<p><b>Market:</b> ' + escape(str(row["regime"])) +
            ' · recorded volatility ' + escape(_num(float(row["volatility"])*100, 1)) +
            '%</p></details>')
    return (
        '<div class="home-heading"><span class="home-eyebrow">Strategy rejection history</span>'
        '<h1>Learn why some strategies failed.</h1>'
        '<p>Past optimization rejects ideas that performed poorly on its historical '
        'checks. Failures are normal; avoiding the same historical mistake '
        'does not guarantee trading success.</p></div>'
        '<div class="learning-kpi-grid">' +
        _stat("Recorded failure events", data["total_failure_records"],
              "Not distinct trading bots or actual losses") +
        _stat("Latest finished run failures", data["latest_run_failure_records"],
              "Historical optimization rejections") +
        _stat("Total candidate evaluations", data["candidate_evaluations"],
              "Unique strategies with held-out tests") +
        '</div>'
        '<section><h2>Most common failure reasons</h2>'
        '<p class="help">The bars group actual recorded diagnosis text into broad '
        'categories. Click a bar to inspect the matching original diagnoses.</p>'
        + _failure_bars(data, scope=scope) + '</section>'
        '<section id="failure-history"><div class="section-head"><h2>Detailed rejection records</h2>'
        '<span class="status-pill">' + str(listing["total"]) + ' matching records</span></div>'
        + scope_nav + picks
        + ('<div class="failure-records">' + "".join(entries) + '</div>' if entries else
           '<div class="home-empty"><b>No matching rejection records.</b>'
           '<p>Change the scope or category to see other results.</p></div>')
        + paging("/graveyard", listing["offset"], listing["limit"], listing["total"],
                 scope=scope, category=category) +
        '</section><p class="inline-tip"><a href="/learning">Back to Learning &amp; Research →</a></p>')
