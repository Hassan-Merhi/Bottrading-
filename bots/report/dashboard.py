"""FastAPI dashboard: mode banner, limits, leaderboard, progress, graveyard, results.

Read-only, except the owner's approve/decline buttons. Those need the
DASHBOARD_TOKEN env value; without it set, approvals work only from Telegram.
"""
from __future__ import annotations

import datetime as _dt
import hmac
import json
import os
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from bots.db import connect
from bots.paper.trader import decide_graduation
from bots.report.daily import account_rows, summary
from bots.risk.guard import HARD_CAPITAL_CEILING_USD
from bots.risk.state import (DAILY_HALT_UNTIL, ENTRY_HALT, HEARTBEAT, KILL_SWITCH, CRASH_LOCK,
                             RiskState, now_ms)
from bots.strategy.genome import Genome

CSS = """
:root{--surface:#fcfcfb;--panel:#ffffff;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#8a8984;
--line:#e4e3df;--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;
--paper:#2a78d6;--live:#e34948;--good:#008300;--bad:#c62828;color-scheme:light}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
--surface:#1a1a19;--panel:#222221;--ink:#f0efec;--ink-2:#c3c2b7;--ink-3:#8f8e86;
--line:#3a3a37;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
--paper:#3987e5;--live:#e66767;--good:#4caf50;--bad:#ef5350;color-scheme:dark}}
:root[data-theme="dark"]{--surface:#1a1a19;--panel:#222221;--ink:#f0efec;--ink-2:#c3c2b7;
--ink-3:#8f8e86;--line:#3a3a37;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
--paper:#3987e5;--live:#e66767;--good:#4caf50;--bad:#ef5350;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
nav a{color:var(--ink-2);margin-right:16px}
.banner{border-radius:10px;padding:18px 20px;margin:12px 0;color:#fff;font-weight:700;
font-size:28px;letter-spacing:.04em}
.banner small{display:block;font-size:14px;font-weight:500;letter-spacing:0;opacity:.95}
.banner.paper{background:var(--paper)}.banner.live{background:var(--live)}
.warn{border:1px solid var(--line);border-left:4px solid var(--live);background:var(--panel);
padding:10px 14px;border-radius:6px;margin:8px 0}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin:14px 0;overflow-x:auto}
h2{font-size:17px;margin:0 0 10px}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--ink-2);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--bad)}.muted{color:var(--ink-3)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.tile{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.tile .lbl{color:var(--ink-2);font-size:13px;display:block}
button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);
background:var(--surface);color:var(--ink);cursor:pointer}
input{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;
background:var(--surface);color:var(--ink);width:120px}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}
.legend{display:flex;gap:16px;font-size:13px;color:var(--ink-2);margin-bottom:6px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;
margin-right:6px}
@media (max-width:700px){table.wide{min-width:680px}.chart,.legend{min-width:560px}.banner{font-size:22px}}
.tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:6px 8px;font-size:13px;display:none;white-space:nowrap}
"""

CHART_JS = """
document.querySelectorAll('.chart').forEach(function(c){
  var data=JSON.parse(c.dataset.points), svg=c.querySelector('svg'), tip=c.querySelector('.tip'),
      cross=svg.querySelector('.cross'), W=+svg.dataset.w, L=+svg.dataset.l, R=+svg.dataset.r;
  svg.addEventListener('mousemove',function(e){
    var b=svg.getBoundingClientRect(), x=(e.clientX-b.left)*W/b.width;
    var i=Math.round((x-L)/((W-L-R)/Math.max(data.length-1,1)));
    i=Math.max(0,Math.min(data.length-1,i)); var d=data[i];
    var px=L+i*(W-L-R)/Math.max(data.length-1,1);
    cross.setAttribute('x1',px);cross.setAttribute('x2',px);cross.style.display='';
    tip.innerHTML='Generation '+d[0]+'<br>best '+d[1].toFixed(3)+'<br>median '+d[2].toFixed(3)+
      '<br>worst '+d[3].toFixed(3);
    tip.style.display='block';
    var left=(px/W)*b.width+12; if(left>b.width-140) left-=160;
    tip.style.left=left+'px'; tip.style.top='30px';
  });
  svg.addEventListener('mouseleave',function(){tip.style.display='none';cross.style.display='none';});
});
"""


def _money(x: float | None) -> str:
    if x is None:
        return "—"
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<span class="{cls}">{x:+.2f}</span>'


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<span class="{cls}">{x * 100:+.1f}%</span>'


def progress_chart(gens: list[dict]) -> str:
    """Best / median / worst fitness per generation, one shared axis."""
    if len(gens) < 2:
        return '<p class="muted">No evolution run yet.</p>'
    W, H, L, R, T, B = 720, 240, 48, 70, 12, 28
    lo = min(g["worst"] for g in gens)
    hi = max(g["best"] for g in gens)
    lo = max(lo, hi - 20)  # keep a crash in the worst line from flattening the rest
    span = (hi - lo) or 1
    n = len(gens)

    def xy(i, v):
        return L + i * (W - L - R) / (n - 1), T + (hi - max(v, lo)) / span * (H - T - B)

    grid = []
    for k in range(5):
        v = lo + span * k / 4
        y = xy(0, v)[1]
        grid.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>'
                    f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                    f'fill="var(--ink-3)">{v:.1f}</text>')
    lines = []
    label_y = -1e9
    for key, var in (("best", "--series-1"), ("median", "--series-2"), ("worst", "--series-3")):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, g[key]) for i, g in enumerate(gens)))
        lx, ly = xy(n - 1, gens[-1][key])
        label_y = max(ly + 4, label_y + 14)  # keep end labels from colliding
        lines.append(f'<polyline points="{pts}" fill="none" stroke="var({var})" stroke-width="2" '
                     f'stroke-linejoin="round"/>'
                     f'<text x="{lx + 6:.1f}" y="{label_y:.1f}" font-size="12" '
                     f'fill="var(--ink-2)">{key}</text>')
    xlabels = (f'<text x="{L}" y="{H - 8}" font-size="11" fill="var(--ink-3)">gen 0</text>'
               f'<text x="{W - R}" y="{H - 8}" font-size="11" fill="var(--ink-3)" '
               f'text-anchor="end">gen {gens[-1]["generation"]}</text>')
    points = json.dumps([[g["generation"], g["best"], g["median"], g["worst"]] for g in gens])
    legend = "".join(f'<span><i style="background:var({v})"></i>{t}</span>' for v, t in
                     (("--series-1", "best"), ("--series-2", "median"), ("--series-3", "worst")))
    return (f'<div class="legend">{legend}</div><div class="chart" data-points=\'{escape(points)}\'>'
            f'<svg viewBox="0 0 {W} {H}" data-w="{W}" data-l="{L}" data-r="{R}" role="img" '
            f'aria-label="Fitness by generation">{"".join(grid)}{"".join(lines)}{xlabels}'
            f'<line class="cross" y1="{T}" y2="{H - B}" stroke="var(--ink-3)" '
            f'stroke-dasharray="3 3" style="display:none"/></svg><div class="tip"></div></div>')


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style>{CSS}</style></head><body><main>'
            f'<nav><a href="/">Overview</a><a href="/graveyard">Graveyard</a>'
            f'<a href="/api/status">JSON</a></nav>{body}</main><script>{CHART_JS}</script>'
            f'</body></html>')


def create_app(db_path: str | Path, cfg: dict, env: dict | None = None) -> FastAPI:
    env = env if env is not None else dict(os.environ)
    app = FastAPI(title="Trading bots", docs_url=None, redoc_url=None)

    def db():
        return connect(db_path)

    def check_token(token: str) -> None:
        expected = env.get("DASHBOARD_TOKEN") or ""
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPException(403, "approvals need the DASHBOARD_TOKEN")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/status")
    def status():
        conn = db()
        try:
            s = summary(conn, now_ms())
            st = RiskState(conn)
            s["flags"] = {k: st.get(k) for k in (KILL_SWITCH, CRASH_LOCK, ENTRY_HALT,
                                                 DAILY_HALT_UNTIL, HEARTBEAT)}
            return JSONResponse(json.loads(json.dumps(s, default=str)))
        finally:
            conn.close()

    @app.post("/bots/{bot_id}/{action}")
    def decide(bot_id: str, action: str, token: str = Form("")):
        check_token(token)
        if action not in ("approve", "decline"):
            raise HTTPException(404)
        conn = db()
        try:
            decide_graduation(conn, bot_id, action == "approve", "dashboard")
        finally:
            conn.close()
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def overview():
        conn = db()
        try:
            return page("Trading bots", _overview(conn, cfg, bool(env.get("DASHBOARD_TOKEN"))))
        finally:
            conn.close()

    @app.get("/graveyard", response_class=HTMLResponse)
    def graveyard():
        conn = db()
        try:
            return page("Graveyard", _graveyard(conn))
        finally:
            conn.close()

    return app


def _overview(conn, cfg: dict, approvals_enabled: bool) -> str:
    s = summary(conn, now_ms())
    st = RiskState(conn)
    mode = s["mode"]
    reasons = st.get("mode_reasons", "")
    risk = cfg["risk"]
    if mode == "LIVE":
        banner = ('<div class="banner live">LIVE — REAL MONEY<small>Live trading can lose money, '
                  f'up to the caps below (daily ${risk["daily_loss_limit_usd"]}, total '
                  f'${risk["total_drawdown_kill_usd"]}).</small></div>')
    else:
        banner = ('<div class="banner paper">PAPER — fake money, real prices<small>No real money '
                  f'at risk. {escape(reasons)}</small></div>')
    parts = [banner,
             '<div class="warn">Live trading is never risk-free. If live mode is unlocked, real '
             f'money can be lost up to the configured caps: ${risk["daily_loss_limit_usd"]} per '
             f'day and ${risk["total_drawdown_kill_usd"]} in total before the kill switch, with '
             f'at most ${risk["max_total_capital_usd"]} of capital (hard ceiling '
             f'${HARD_CAPITAL_CEILING_USD:.0f}). Past results do not guarantee future profits.'
             '</div>']
    flags = [(KILL_SWITCH, "Kill switch"), (CRASH_LOCK, "Waiting for owner after restart"),
             (ENTRY_HALT, "New trades halted"), (DAILY_HALT_UNTIL, "Daily loss pause until")]
    for key, label in flags:
        v = st.get(key)
        if v and not (key == DAILY_HALT_UNTIL and int(v) < now_ms()):
            parts.append(f'<div class="warn"><b>{label}:</b> {escape(v)}</div>')

    hb = st.get(HEARTBEAT)
    hb_txt = f"{(now_ms() - int(hb)) / 60000:.0f} min ago" if hb else "never"
    parts.append(
        '<section><h2>Results</h2><div class="grid">'
        f'<div class="tile"><span class="lbl">Paper P&amp;L, USD ({s["paper_bots"]} bots)</span>'
        f'<b>{_money(s["paper_pnl"])}</b><span class="lbl">today '
        f'{_money(s["paper_pnl_today"])}</span></div>'
        f'<div class="tile"><span class="lbl">Live P&amp;L, USD ({s["live_bots"]} bots)</span>'
        f'<b>{_money(s["live_pnl"])}</b><span class="lbl">today '
        f'{_money(s["live_pnl_today"])}</span></div>'
        f'<div class="tile"><span class="lbl">Protected funds (house money)</span>'
        f'<b>${s["protected"]:.2f}</b><span class="lbl">never traded again</span></div>'
        f'<div class="tile"><span class="lbl">Last trading loop</span><b>{hb_txt}</b></div>'
        '</div></section>')

    parts.append(
        '<section><h2>Limits</h2><table>'
        + "".join(f"<tr><th>{escape(k)}</th><td class='n'>{escape(str(v))}</td></tr>" for k, v in (
            ("Max total live capital", f"${risk['max_total_capital_usd']}"),
            ("Max per bot", f"${risk['max_per_bot_usd']}"),
            ("Max live bots", risk["max_live_bots"]),
            ("Daily loss limit", f"${risk['daily_loss_limit_usd']}"),
            ("Total drawdown kill", f"${risk['total_drawdown_kill_usd']}"),
            ("live_enabled in config", cfg.get("live_enabled") is True)))
        + "</table></section>")

    waiting = conn.execute("SELECT * FROM deployed_bots WHERE status='ready_to_graduate'").fetchall()
    if waiting:
        rows = []
        for b in waiting:
            g = Genome.from_dict(json.loads(b["genome_json"]))
            form = ""
            if approvals_enabled:
                bid = escape(b["bot_id"])
                form = (f'<form method="post" action="/bots/{bid}/approve">'
                        '<input type="password" name="token" placeholder="dashboard token" '
                        'required aria-label="dashboard token"> <button>Approve</button> '
                        f'<button formaction="/bots/{bid}/decline">Decline</button></form>')
            else:
                form = '<span class="muted">Approve from Telegram (no DASHBOARD_TOKEN set)</span>'
            rows.append(f"<tr><td>{escape(b['bot_id'])}</td><td>{escape(g.describe())}</td>"
                        f"<td>{form}</td></tr>")
        parts.append('<section><h2>Ready to graduate — needs your approval</h2><table class="wide">'
                     '<tr><th>Bot</th><th>Strategy</th><th></th></tr>' + "".join(rows) +
                     '</table></section>')

    for mode_name, title in (("live", "Live bots"), ("paper", "Paper bots")):
        accs = account_rows(conn, mode_name)
        if not accs:
            parts.append(f'<section><h2>{title}</h2><p class="muted">None.</p></section>')
            continue
        rows = "".join(
            f"<tr><td>{escape(a['bot_id'])}</td><td>{escape(a['status'])}</td>"
            f"<td>{escape(Genome.from_dict(json.loads(a['genome_json'])).describe())}</td>"
            f"<td class='n'>${a['allocation']:.2f}</td><td class='n'>${a['last_equity']:.2f}</td>"
            f"<td class='n'>{_pct(a['pnl'] / a['allocation'])}</td>"
            f"<td class='n'>{a['max_drawdown'] * 100:.1f}%</td><td class='n'>{a['trades']}</td>"
            f"<td class='n'>{'open' if a['qty'] else 'flat'}</td>"
            f"<td class='n'>${a['protected']:.2f}</td></tr>" for a in accs)
        parts.append(f'<section><h2>{title}</h2><table class="wide"><tr><th>Bot</th><th>Status</th>'
                     '<th>Strategy</th><th>Start</th><th>Equity</th><th>Return</th><th>Max DD</th>'
                     '<th>Trades</th><th>Position</th><th>Protected</th></tr>'
                     f'{rows}</table></section>')

    run = conn.execute("SELECT run_id, data_label FROM runs WHERE finished_at IS NOT NULL "
                       "ORDER BY finished_at DESC LIMIT 1").fetchone()
    if run:
        gens = [dict(r) for r in conn.execute(
            "SELECT generation, best, median, worst, failure_summary FROM generations "
            "WHERE run_id=? ORDER BY generation", (run["run_id"],))]
        parts.append(f'<section><h2>Generation progress — run {escape(run["run_id"])}</h2>'
                     f'<p class="muted">{escape(run["data_label"])}. Fitness = risk-adjusted '
                     f'return after fees, minus penalties.</p>{progress_chart(gens)}'
                     + (f'<p>{escape(gens[-1]["failure_summary"])}</p>' if gens else "")
                     + '</section>')
        top = conn.execute("SELECT * FROM population WHERE run_id=? ORDER BY rank LIMIT 20",
                           (run["run_id"],)).fetchall()
        rows = []
        for r in top:
            sm = json.loads(r["summary_json"])
            g = Genome.from_dict(json.loads(r["genome_json"]))
            rows.append(f"<tr><td class='n'>{r['rank'] + 1}</td><td>{escape(g.describe())}</td>"
                        f"<td class='n'>{r['fitness']:.3f}</td><td class='n'>{_pct(sm['return'])}</td>"
                        f"<td class='n'>{_pct(sm['hold_return'])}</td>"
                        f"<td class='n'>{sm['max_drawdown'] * 100:.1f}%</td>"
                        f"<td class='n'>{sm['trades']}</td></tr>")
        parts.append('<section><h2>Population leaderboard (train period)</h2><table class="wide"><tr><th>#</th>'
                     '<th>Strategy</th><th>Fitness</th><th>Return</th><th>Hold</th><th>Max DD</th>'
                     '<th>Trades</th></tr>' + "".join(rows) + '</table></section>')
        summaries = conn.execute("SELECT generation, failure_summary FROM generations WHERE "
                                 "run_id=? ORDER BY generation DESC LIMIT 5",
                                 (run["run_id"],)).fetchall()
        parts.append('<section><h2>Recent failure summaries</h2><ul>' + "".join(
            f"<li>{escape(r['failure_summary'])}</li>" for r in summaries) + '</ul></section>')

    events = conn.execute("SELECT * FROM risk_events ORDER BY ts DESC LIMIT 20").fetchall()
    ev_rows = "".join(
        f"<tr><td>{_dt.datetime.fromtimestamp(e['ts'] / 1000, _dt.timezone.utc):%Y-%m-%d %H:%M}"
        f"</td><td>{escape(e['kind'])}</td><td>{escape(e['detail'])}</td></tr>" for e in events)
    parts.append('<section><h2>Risk events</h2>' + (
        f'<table class="wide"><tr><th>UTC</th><th>Kind</th><th>Detail</th></tr>{ev_rows}</table>'
        if events else '<p class="muted">None.</p>') + '</section>')
    return "".join(parts)


def _graveyard(conn) -> str:
    causes = conn.execute("SELECT diagnosis, COUNT(*) n FROM graveyard GROUP BY diagnosis "
                          "ORDER BY n DESC").fetchall()
    total = sum(r["n"] for r in causes)
    rows = conn.execute("SELECT * FROM graveyard ORDER BY id DESC LIMIT 200").fetchall()
    body = [f'<section><h2>Causes of death ({total} bots)</h2><table>'
            '<tr><th>Diagnosis</th><th>Bots</th></tr>' + "".join(
                f"<tr><td>{escape(r['diagnosis'])}</td><td class='n'>{r['n']}</td></tr>"
                for r in causes) + '</table></section>']
    items = []
    for r in rows:
        g = Genome.from_dict(json.loads(r["genome_json"]))
        items.append(f"<tr><td>{escape(r['run_id'])} / {r['generation']}</td>"
                     f"<td>{escape(g.describe())}</td><td>{escape(r['cause'])}</td>"
                     f"<td>{escape(r['diagnosis'])}</td><td>{escape(r['regime'])}, vol "
                     f"{r['volatility'] * 100:.0f}%</td></tr>")
    body.append('<section><h2>Latest 200 dead bots</h2><table class="wide"><tr><th>Run / gen</th>'
                '<th>Strategy</th><th>Death rule</th><th>Why</th><th>Market</th></tr>'
                + "".join(items) + '</table></section>')
    return "".join(body)
