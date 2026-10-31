"""FastAPI dashboard: a control panel for the owner.

Overview: mode, system health, the pipeline (evolution -> paper -> approval -> live),
results, every bot with its progress toward graduation, evolution progress, the
market briefing and the risk log. Works on a phone: tables stack into cards.

Read-only, except the owner's approve/decline buttons. Those need the
DASHBOARD_TOKEN env value; without it set, approvals work only from Telegram.
"""
from __future__ import annotations

import datetime as _dt
import csv
import io
import hmac
import json
import os
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from bots.analyst.digest import latest_digest
from bots.data.fetch import BACKFILL_DONE
from bots.data.health import market_health
from bots.data.mood import latest_fear_greed
from bots.data.store import get_meta
from bots.db import connect
from bots.paper.trader import decide_graduation
from bots.paper.forward import evidence, net_hold_return, history
from bots.live.pilot import pilot_status
from bots.live.scale import scale_review
from bots.report.daily import account_rows, summary
from bots.report.home import home_dashboard
from bots.report.performance import report as performance_report, export_csv as performance_csv, resolve_bot_slug, validate as validate_performance
from bots.report.performance_view import performance_page
from bots.report.bots_view import bots_index, research_detail, standard_account_detail
from bots.report.design import shell_page
from bots.paper.league import snapshot as lab_snapshot
from bots.paper.forward_history import forward_history
from bots.report.forward_page import history_page, overview_panel
from bots.research.agent import snapshot as research_snapshot
from bots.report.learning_data import (report as learning_report,
                                      source_list as learning_sources,
                                      failure_list as learning_failures)
from bots.report.learning_view import learning_page, source_page, failure_page
from bots.report.safety_data import snapshot as safety_snapshot
from bots.report.safety_view import safety_page, settings_page, approval_page
from bots.risk.guard import HARD_CAPITAL_CEILING_USD
from bots.risk.ledger import DAY_MS
from bots.risk.state import (DAILY_HALT_UNTIL, ENTRY_HALT, HEARTBEAT, KILL_SWITCH, CRASH_LOCK,
                             OWNER_PAUSE, RiskState, now_ms)
from bots.strategy.genome import Genome

CSS = """
:root{--surface:#f4f5f7;--panel:#ffffff;--panel-2:#f8f9fb;--ink:#101418;--ink-2:#4b5563;
--ink-3:#8a919c;--line:#e3e6ea;--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;
--paper:#2563eb;--paper-soft:#e8f0fe;--live:#dc2626;--live-soft:#fde8e8;--good:#15803d;
--good-soft:#e6f4ea;--bad:#c62828;--bad-soft:#fdecea;--warn:#b45309;--warn-soft:#fff4e0;
--accent:#2563eb;--shadow:0 1px 2px rgba(16,20,24,.06),0 4px 14px rgba(16,20,24,.05);
color-scheme:light}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
--surface:#0f1216;--panel:#171b21;--panel-2:#1d222a;--ink:#eef0f3;--ink-2:#b4bac4;
--ink-3:#7c838e;--line:#2a3039;--series-1:#4a8ee6;--series-2:#ef7a4b;--series-3:#2bbf88;
--paper:#4a8ee6;--paper-soft:#182942;--live:#ef5350;--live-soft:#3a1d1d;--good:#4ade80;
--good-soft:#14301f;--bad:#f87171;--bad-soft:#3a1d1d;--warn:#fbbf24;--warn-soft:#3a2c0f;
--accent:#4a8ee6;--shadow:none;color-scheme:dark}}
:root[data-theme="dark"]{--surface:#0f1216;--panel:#171b21;--panel-2:#1d222a;--ink:#eef0f3;
--ink-2:#b4bac4;--ink-3:#7c838e;--line:#2a3039;--series-1:#4a8ee6;--series-2:#ef7a4b;
--series-3:#2bbf88;--paper:#4a8ee6;--paper-soft:#182942;--live:#ef5350;--live-soft:#3a1d1d;
--good:#4ade80;--good-soft:#14301f;--bad:#f87171;--bad-soft:#3a1d1d;--warn:#fbbf24;
--warn-soft:#3a2c0f;--accent:#4a8ee6;--shadow:none;color-scheme:dark}
*{box-sizing:border-box}html{scroll-padding-top:104px;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--surface);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
.top{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line)}
.bar{max-width:1100px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:12px;
flex-wrap:wrap}
.brand{font-weight:700;font-size:17px;letter-spacing:.01em;display:flex;align-items:center;gap:8px}
.brand i{width:10px;height:10px;border-radius:50%;background:var(--good);display:inline-block}
.brand i.off{background:var(--bad)}
.mode{font-weight:800;font-size:13px;letter-spacing:.08em;padding:4px 10px;border-radius:999px}
.mode.paper{background:var(--paper-soft);color:var(--paper)}
.mode.live{background:var(--live);color:#fff}
.hb{color:var(--ink-3);font-size:13px;margin-left:auto}
.tabs{max-width:1100px;margin:0 auto;padding:0 8px;display:flex;gap:2px;overflow-x:auto;
scrollbar-width:none;-webkit-overflow-scrolling:touch}
.tabs::-webkit-scrollbar{display:none}
.tabs a{color:var(--ink-2);text-decoration:none;padding:8px 10px;font-size:14px;white-space:nowrap;
border-bottom:2px solid transparent}
.tabs a:hover,.tabs a.on{color:var(--ink);border-bottom-color:var(--accent)}
main{max-width:1100px;margin:0 auto;padding:12px 16px 56px}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:16px;margin:14px 0;box-shadow:var(--shadow)}
h2{font-size:16px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
h2 .count{background:var(--panel-2);border:1px solid var(--line);border-radius:999px;
font-size:12px;padding:0 8px;color:var(--ink-2);font-weight:600}
.help{color:var(--ink-3);font-size:13px;margin:0 0 12px}
.hero{display:grid;grid-template-columns:1.2fr 1fr;gap:14px;margin:14px 0}
.modecard{border-radius:14px;padding:18px 20px;color:#fff;box-shadow:var(--shadow)}
.modecard.paper{background:linear-gradient(135deg,#2563eb,#1d4ed8)}
.modecard.live{background:linear-gradient(135deg,#dc2626,#991b1b)}
.modecard .big{font-size:30px;font-weight:800;letter-spacing:.03em;line-height:1.1}
.modecard .sub{opacity:.95;font-size:14px;margin-top:6px}
.modecard .why{opacity:.85;font-size:12.5px;margin-top:10px;line-height:1.4}
.status{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
box-shadow:var(--shadow)}
.status h3{margin:0 0 8px;font-size:13px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.kv{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-top:1px solid var(--line);
font-size:14px}.kv:first-of-type{border-top:0}.kv span:last-child{text-align:right;color:var(--ink-2)}
.ok{color:var(--good);font-weight:600}.no{color:var(--bad);font-weight:600}.soon{color:var(--warn);font-weight:600}
.notice{border:1px solid var(--line);border-left:4px solid var(--warn);background:var(--warn-soft);
color:var(--ink);padding:10px 14px;border-radius:10px;margin:10px 0;font-size:14px}
.notice.danger{border-left-color:var(--live);background:var(--live-soft)}
.pipe{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.step{background:var(--panel-2);border:1px solid var(--line);border-radius:12px;padding:12px;
position:relative}
.step b{display:block;font-size:26px;line-height:1;font-variant-numeric:tabular-nums;margin:4px 0 6px}
.step .t{font-size:13px;font-weight:600}.step .d{font-size:12px;color:var(--ink-3);line-height:1.35}
.step.hot{border-color:var(--warn);background:var(--warn-soft)}
.step.live{border-color:var(--live)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--panel-2);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums;line-height:1.2;margin:2px 0}
.tile .lbl{color:var(--ink-2);font-size:12.5px;display:block}
.pos{color:var(--good)}.neg{color:var(--bad)}.muted{color:var(--ink-3)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.bot{background:var(--panel-2);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.bot .head{display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap}
.bot .id{font-weight:700;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;white-space:nowrap}
.bot .strategy{font-size:13px;color:var(--ink-2);line-height:1.4;margin-bottom:10px}
.badge{font-size:11.5px;font-weight:700;padding:2px 8px;border-radius:999px;margin-left:auto;
white-space:nowrap;background:var(--panel);border:1px solid var(--line);color:var(--ink-2)}
.badge.paper{background:var(--paper-soft);color:var(--paper);border-color:transparent}
.badge.live{background:var(--live);color:#fff;border-color:transparent}
.badge.ready{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.badge.open{background:var(--good-soft);color:var(--good);border-color:transparent}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px}
.stats div{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 8px}
.stats b{display:block;font-size:15px;font-variant-numeric:tabular-nums}
.stats span{font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.04em}
.spark{margin:8px 0 2px}.spark svg{width:100%;height:56px;display:block}
.spark .lbl{font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.05em}
.gate{font-size:12.5px}.gate .row{display:flex;align-items:center;gap:8px;padding:3px 0}
.gate .row span:first-child{flex:0 0 118px;color:var(--ink-2)}
.gate .track{flex:1;height:7px;border-radius:4px;background:var(--line);overflow:hidden}
.gate .fill{height:100%;background:var(--accent);border-radius:4px}
.gate .fill.done{background:var(--good)}.gate .fill.fail{background:var(--bad)}
.gate .v{flex:0 0 auto;min-width:56px;text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2)}
.gate .title{font-weight:600;color:var(--ink-2);margin:4px 0 2px;font-size:12px;text-transform:uppercase;
letter-spacing:.05em}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--ink-2);font-weight:600;font-size:13px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
form.approve{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px}
button{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--line);
background:var(--panel);color:var(--ink);cursor:pointer;font-weight:600}
button.primary{background:var(--good);border-color:var(--good);color:#fff}
button.danger{color:var(--bad)}
input{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--panel);color:var(--ink);width:160px;max-width:100%}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}
.legend{display:flex;gap:16px;font-size:13px;color:var(--ink-2);margin-bottom:6px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;
margin-right:6px}
.tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:6px 8px;font-size:13px;display:none;white-space:nowrap;box-shadow:var(--shadow)}
.digest{white-space:pre-wrap;line-height:1.55;font-size:14px;background:var(--panel-2);
border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.mood{display:flex;align-items:center;gap:14px;margin-bottom:10px}
.mood .dial{flex:1;height:10px;border-radius:5px;position:relative;
background:linear-gradient(90deg,#dc2626,#f59e0b,#16a34a)}
.mood .dial i{position:absolute;top:-4px;width:18px;height:18px;border-radius:50%;background:var(--panel);
border:3px solid var(--ink);transform:translateX(-50%)}
.how{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.how div{background:var(--panel-2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
font-size:13px}.how b{display:block;margin-bottom:2px}
.events li{padding:6px 0;border-bottom:1px solid var(--line);font-size:14px}
.events li:last-child{border-bottom:0}.events time{color:var(--ink-3);font-size:12.5px;display:block}
.events .kind{font-weight:600;margin-right:6px}
ul{padding-left:18px;margin:6px 0}
@media (max-width:760px){
 .hero{grid-template-columns:1fr}.modecard .big{font-size:24px}
 .pipe{grid-template-columns:repeat(2,1fr)}.pipe .step:last-child{grid-column:span 2}
 .stats{grid-template-columns:repeat(2,1fr)}
 .cards{grid-template-columns:1fr}
 table.stack thead{display:none}
 table.stack tr{display:block;border:1px solid var(--line);border-radius:10px;margin:8px 0;
 padding:6px 10px;background:var(--panel-2)}
 table.stack td{display:flex;justify-content:space-between;gap:12px;border:0;padding:4px 0;
 text-align:right}
 table.stack td::before{content:attr(data-label);color:var(--ink-3);font-size:12px;text-align:left;
 flex:0 0 40%}
 table.stack td.wide{display:block;text-align:left}
 table.stack td.wide::before{display:block;margin-bottom:2px}
 .gate .row span:first-child{flex-basis:96px}
 section{padding:14px 12px;border-radius:12px}main{padding:8px 10px 56px}
 .hb{display:none}
}
"""

JS = """
document.querySelectorAll('.chart').forEach(function(c){
  var data=JSON.parse(c.dataset.points), svg=c.querySelector('svg'), tip=c.querySelector('.tip'),
      cross=svg.querySelector('.cross'), W=+svg.dataset.w, L=+svg.dataset.l, R=+svg.dataset.r;
  function show(clientX){
    var b=svg.getBoundingClientRect(), x=(clientX-b.left)*W/b.width;
    var i=Math.round((x-L)/((W-L-R)/Math.max(data.length-1,1)));
    i=Math.max(0,Math.min(data.length-1,i)); var d=data[i];
    var px=L+i*(W-L-R)/Math.max(data.length-1,1);
    cross.setAttribute('x1',px);cross.setAttribute('x2',px);cross.style.display='';
    tip.innerHTML='Generation '+d[0]+'<br>best '+d[1].toFixed(3)+'<br>median '+d[2].toFixed(3)+
      '<br>worst '+d[3].toFixed(3);
    tip.style.display='block';
    var left=(px/W)*b.width+12; if(left>b.width-140) left-=160;
    tip.style.left=left+'px'; tip.style.top='30px';
  }
  svg.addEventListener('mousemove',function(e){show(e.clientX);});
  svg.addEventListener('touchmove',function(e){show(e.touches[0].clientX);},{passive:true});
  svg.addEventListener('mouseleave',function(){tip.style.display='none';cross.style.display='none';});
});
(function(){
  var links=[].slice.call(document.querySelectorAll('.tabs a[href^="#"]'));
  var secs=links.map(function(a){return document.querySelector(a.getAttribute('href'));});
  function mark(){
    var y=window.scrollY+120, on=0;
    secs.forEach(function(s,i){if(s&&s.offsetTop<=y) on=i;});
    links.forEach(function(a,i){a.classList.toggle('on',i===on);});
  }
  if(links.length){window.addEventListener('scroll',mark,{passive:true});mark();}
})();
"""


# -- small formatting helpers --------------------------------------------------

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


def _utc(ms: int | float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc)


def _ago(ms: int | None, now: int) -> str:
    if not ms:
        return "never"
    m = (now - int(ms)) / 60000
    if m < 1:
        return "just now"
    if m < 90:
        return f"{m:.0f} min ago"
    h = m / 60
    if h < 36:
        return f"{h:.0f} h ago"
    return f"{h / 24:.0f} days ago"


def _next_at(hour: int, now: int, ran_today: bool = False) -> str:
    """'today 02:00 UTC (in 3 h)' or 'tomorrow 02:00 UTC'."""
    n = _utc(now)
    target = n.replace(hour=hour, minute=0, second=0, microsecond=0)
    if n >= target or ran_today:
        target += _dt.timedelta(days=1)
    hours = (target - n).total_seconds() / 3600
    when = "today" if target.date() == n.date() else "tomorrow"
    left = f"in {hours * 60:.0f} min" if hours < 1 else f"in {hours:.0f} h"
    return f"{when} {hour:02d}:00 UTC ({left})"


def _table(headers: list[tuple[str, str]], rows: list[list[str]]) -> str:
    """A table that stacks into cards on narrow screens.

    headers: (label, css class) pairs; the class is applied to both th and td.
    """
    head = "".join(f'<th class="{c}">{escape(h)}</th>' for h, c in headers)
    body = []
    for r in rows:
        body.append("<tr>" + "".join(
            f'<td class="{c}" data-label="{escape(h)}">{cell}</td>'
            for (h, c), cell in zip(headers, r)) + "</tr>")
    return f'<table class="stack"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


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


OVERVIEW_TABS = [("#status", "Status"), ("#league", "Paper league"), ("#research", "Research"),
                 ("#pipeline", "Pipeline"), ("#results", "Results"),
                 ("#bots", "Bots"), ("#evolution", "Evolution"), ("#briefing", "Briefing"),
                 ("#events", "Risk log"), ("#limits", "Limits")]


def page(title: str, body: str, mode: str = "PAPER", heartbeat: str = "",
         tabs: list[tuple[str, str]] | None = None, alive: bool = True,
         current: str = "home") -> str:
    """Shared shell: the same navigation and styling on every page.

    The legacy technical sections remain fully accessible under /advanced.
    Rendered data is unchanged; only presentation and routing are new.
    """
    html = shell_page(title, body, mode, heartbeat, tabs, current=current, alive=alive)
    return html.replace("__BASE_CSS__", CSS).replace("__BASE_JS__", JS)


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
            s["market_data"] = market_health(conn, cfg)
            s["pilot"] = pilot_status(conn, cfg, now_ms())
            s["scaling"] = scale_review(conn, cfg, now_ms())
            s["research_league"] = lab_snapshot(conn, cfg)
            s["research"] = research_snapshot(conn, cfg)
            return JSONResponse(json.loads(json.dumps(s, default=str)))
        finally:
            conn.close()

    @app.get("/api/research/league")
    def research_league_status():
        conn = db()
        try:
            return JSONResponse(lab_snapshot(conn, cfg))
        finally:
            conn.close()

    @app.get("/api/research/sources")
    def research_sources_status():
        conn = db()
        try:
            return JSONResponse(research_snapshot(conn, cfg))
        finally:
            conn.close()

    @app.get("/api/research/forward")
    def research_forward_api(name: str = "all", limit: int = 720):
        conn = db()
        try:
            try:
                return JSONResponse(forward_history(conn, cfg, name=name, limit=limit))
            except ValueError:
                raise HTTPException(404, "Unknown research bot") from None
        finally:
            conn.close()

    @app.get("/api/research/forward.csv")
    def research_forward_csv(name: str = "all", kind: str = "equity"):
        if kind not in ("equity", "trades"):
            raise HTTPException(400, "kind must be equity or trades")
        conn = db()
        try:
            try:
                result = forward_history(conn, cfg, name=name, limit=1440, fills_limit=500)
            except ValueError:
                raise HTTPException(404, "Unknown research bot") from None
        finally:
            conn.close()
        output = io.StringIO()
        writer = csv.writer(output)
        if kind == "equity":
            fields = ["candle_ts", "observed_ms", "equity", "net_pnl",
                      "benchmark_equity", "benchmark_net_pnl", "quote_source"]
            writer.writerow(fields)
            for row in result["observations"]:
                writer.writerow([row.get(k) for k in fields])
        else:
            fields = ["id", "name", "ts", "side", "qty", "price", "fee", "pnl", "reason"]
            writer.writerow(fields)
            for row in reversed(result["fills"]):
                writer.writerow([row.get(k) for k in fields])
        return Response(content=output.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="paper-forward-{kind}.csv"'})

    def verified_performance(conn, name: str, period: str, side: str,
                             result: str, search: str) -> dict:
        # A URL must never create an arbitrary lab identity or bypass read-only
        # accounting views. Only the three fixed research experiments are valid.
        from bots.paper.league import research_genomes
        if name != "all" and name not in research_genomes():
            raise HTTPException(404, "Unknown research experiment")
        try:
            return performance_report(conn, cfg, now_ms(), name=name, period=period,
                                      side=side, result=result, search=search)
        except ValueError:
            raise HTTPException(400, "Invalid performance filter") from None

    @app.get("/api/performance")
    def performance_api(name: str = "all", period: str = "7d",
                        side: str = "all", result: str = "all", search: str = ""):
        conn = db()
        try:
            return JSONResponse(verified_performance(conn, name, period, side, result, search))
        finally:
            conn.close()

    @app.get("/api/performance.csv")
    def performance_export(name: str = "all", period: str = "7d",
                           side: str = "all", result: str = "all",
                           search: str = "", kind: str = "trades"):
        if kind not in ("trades", "equity"):
            raise HTTPException(400, "Invalid CSV export kind")
        conn = db()
        try:
            result_data = verified_performance(conn, name, period, side, result, search)
            data = performance_csv(result_data, kind)
        finally:
            conn.close()
        return Response(content=data, media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="forward-paper-{kind}-{period}.csv"',
                                 "Cache-Control": "no-store"})

    @app.get("/research/forward", response_class=HTMLResponse)
    def forward_evidence_page(name: str = "all", period: str = "7d",
                              side: str = "all", result: str = "all",
                              search: str = ""):
        conn = db()
        try:
            data = verified_performance(conn, name, period, side, result, search)
            state = RiskState(conn)
            now = now_ms()
            hb = state.get(HEARTBEAT)
            alive = bool(hb) and now - int(hb) < 3 * 60 * 60 * 1000
            return page("Profit and performance", performance_page(data),
                        summary(conn, now)["mode"], f"last trading loop {_ago(hb, now)}",
                        tabs=[("/bots", "My Bots"), ("#equity", "Profit chart"),
                              ("#fees", "Fees"), ("#trades", "Trade history")],
                        alive=alive, current="performance")
        finally:
            conn.close()

    @app.get("/bots", response_class=HTMLResponse)
    def bots_directory():
        conn = db()
        try:
            st = RiskState(conn)
            now = now_ms()
            hb = st.get(HEARTBEAT)
            return page("My Trading Bots", bots_index(conn, cfg),
                        summary(conn, now)["mode"], f"last trading loop {_ago(hb, now)}",
                        alive=bool(hb) and now - int(hb) < 3 * 60 * 60 * 1000,
                        current="bots")
        finally:
            conn.close()

    @app.get("/bots/research/{slug}", response_class=HTMLResponse)
    def bot_research_detail(slug: str, period: str = "7d"):
        try:
            name = resolve_bot_slug(slug)
            validate_performance(period)
        except ValueError:
            raise HTTPException(404, "Unknown bot or invalid period") from None
        conn = db()
        try:
            now = now_ms()
            state = RiskState(conn)
            hb = state.get(HEARTBEAT)
            return page("Research bot: " + name, research_detail(conn, cfg, now, name, period),
                        summary(conn, now)["mode"], f"last trading loop {_ago(hb, now)}",
                        tabs=[("/bots", "All bots"), ("#history", "Performance"),
                              ("#activity", "Activity")],
                        alive=bool(hb) and now - int(hb) < 3 * 60 * 60 * 1000,
                        current="bots")
        finally:
            conn.close()

    @app.get("/bots/account/{account_id}", response_class=HTMLResponse)
    def standard_bot_detail(account_id: int):
        conn = db()
        try:
            now = now_ms()
            state = RiskState(conn)
            hb = state.get(HEARTBEAT)
            try:
                body = standard_account_detail(conn, account_id)
            except ValueError:
                raise HTTPException(404, "Unknown trading account") from None
            return page("Trading bot account", body, summary(conn, now)["mode"],
                        f"last trading loop {_ago(hb, now)}",
                        tabs=[("/bots", "All bots")],
                        alive=bool(hb) and now - int(hb) < 3 * 60 * 60 * 1000,
                        current="bots")
        finally:
            conn.close()

    @app.get("/api/learning/summary")
    def learning_summary():
        """Read-only historical learning status; not forward profitability."""
        conn = db()
        try:
            return JSONResponse(learning_report(conn, cfg))
        finally:
            conn.close()

    @app.get("/api/learning/sources")
    def saved_research_sources(search: str = "", source: str = "",
                               offset: int = 0, limit: int = 24):
        conn = db()
        try:
            return JSONResponse(learning_sources(conn, search, source, offset, limit))
        finally:
            conn.close()

    @app.get("/api/learning/failures")
    def failed_strategy_history(scope: str = "all", category: str = "all",
                                offset: int = 0, limit: int = 40):
        conn = db()
        try:
            try:
                return JSONResponse(learning_failures(conn, scope, category, offset, limit))
            except ValueError:
                raise HTTPException(400, "Unknown strategy failure filter") from None
        finally:
            conn.close()

    @app.get("/learning", response_class=HTMLResponse)
    def learning_explained():
        conn = db()
        try:
            now = now_ms()
            hb = RiskState(conn).get(HEARTBEAT)
            data = learning_report(conn, cfg)
            sources = learning_sources(conn, limit=8)
            return page("Learning and Research", learning_page(data, sources),
                        summary(conn, now)["mode"], f"last trading loop {_ago(hb, now)}",
                        tabs=[("#learning-overview", "Pipeline"), ("#evolution", "Evolution"),
                              ("#failures", "Failures"), ("#research", "News & sources")],
                        alive=bool(hb) and 0 <= now - int(hb) < 3*60*60*1000,
                        current="learning")
        finally:
            conn.close()

    @app.get("/research/sources", response_class=HTMLResponse)
    def source_library(search: str = "", source: str = "", offset: int = 0):
        conn = db()
        try:
            now = now_ms()
            hb = RiskState(conn).get(HEARTBEAT)
            rows = learning_sources(conn, search, source, offset)
            return page("Saved research sources", source_page(rows),
                        summary(conn, now)["mode"], f"last trading loop {_ago(hb, now)}",
                        tabs=[("/learning#research", "Learning & Research")],
                        alive=bool(hb) and 0 <= now-int(hb) < 3*60*60*1000,
                        current="learning")
        finally:
            conn.close()

    @app.get("/api/safety/overview")
    def safety_overview():
        """Read-only evidence: never exposes tokens or operates the guard."""
        conn = db()
        try:
            return JSONResponse(safety_snapshot(conn, cfg, now_ms(),
                                                bool(env.get("DASHBOARD_TOKEN"))))
        finally:
            conn.close()

    @app.get("/safety", response_class=HTMLResponse)
    def safety_center():
        conn = db()
        try:
            now = now_ms()
            hb = RiskState(conn).get(HEARTBEAT)
            data = safety_snapshot(conn, cfg, now, bool(env.get("DASHBOARD_TOKEN")))
            return page("Safety center", safety_page(data),
                        data["mode"], f"last trading loop {_ago(hb, now)}",
                        tabs=[("#warnings", "Warnings"), ("#feeds", "Market data"),
                              ("#events", "Risk history")],
                        alive=data["worker_healthy"], current="safety")
        finally:
            conn.close()

    @app.get("/settings", response_class=HTMLResponse)
    def clear_settings():
        conn = db()
        try:
            now = now_ms()
            hb = RiskState(conn).get(HEARTBEAT)
            data = safety_snapshot(conn, cfg, now, bool(env.get("DASHBOARD_TOKEN")))
            return page("Trading settings", settings_page(data),
                        data["mode"], f"last trading loop {_ago(hb, now)}",
                        tabs=[("/safety", "Safety"), ("/approvals", "Approvals"),
                              ("/advanced#limits", "Technical limits")],
                        alive=data["worker_healthy"], current="settings")
        finally:
            conn.close()

    @app.get("/approvals", response_class=HTMLResponse)
    def owner_approvals():
        conn = db()
        try:
            now = now_ms()
            hb = RiskState(conn).get(HEARTBEAT)
            data = safety_snapshot(conn, cfg, now, bool(env.get("DASHBOARD_TOKEN")))
            return page("Owner approvals", approval_page(data),
                        data["mode"], f"last trading loop {_ago(hb, now)}",
                        tabs=[("/safety", "Safety"), ("#pending", "Pending decisions"),
                              ("#policy", "Approval process")],
                        alive=data["worker_healthy"], current="settings")
        finally:
            conn.close()

    @app.get("/api/pilot/readiness")
    def pilot_readiness():
        conn = db()
        try:
            return JSONResponse(pilot_status(conn, cfg, now_ms()))
        finally:
            conn.close()

    @app.get("/api/scaling/readiness")
    def scaling_readiness():
        conn = db()
        try:
            return JSONResponse(scale_review(conn, cfg, now_ms()))
        finally:
            conn.close()

    @app.get("/api/paper/{bot_id}/history")
    def paper_history(bot_id: str, limit: int = 168):
        conn = db()
        try:
            row = conn.execute(
                "SELECT account_id FROM accounts WHERE bot_id=? AND mode='paper' "
                "ORDER BY account_id DESC LIMIT 1", (bot_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "unknown paper bot")
            size = max(1, min(int(limit), 720))
            return JSONResponse({
                "bot_id": bot_id, "mode": "paper", "simulation": True,
                "evidence": evidence(conn, row["account_id"], now_ms()),
                "observations": history(conn, row["account_id"], size),
            })
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
        return RedirectResponse("/approvals", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def overview():
        """Simple default view: four clear KPIs rather than technical reports."""
        conn = db()
        try:
            st = RiskState(conn)
            now = now_ms()
            hb = st.get(HEARTBEAT)
            alive = bool(hb) and now - int(hb) < 3 * 60 * 60 * 1000
            body = home_dashboard(conn, cfg, now)
            return page("Home dashboard", body, summary(conn, now)["mode"],
                        f"last trading loop {_ago(hb, now)}",
                        alive=alive, current="home")
        finally:
            conn.close()

    @app.get("/advanced", response_class=HTMLResponse)
    def advanced():
        """Preserve all existing technical reports, decisions and approvals."""
        conn = db()
        try:
            st = RiskState(conn)
            now = now_ms()
            hb = st.get(HEARTBEAT)
            alive = bool(hb) and now - int(hb) < 3 * 60 * 60 * 1000
            body = _overview(conn, cfg, bool(env.get("DASHBOARD_TOKEN")))
            return page("Technical dashboard", body, summary(conn, now)["mode"],
                        f"last trading loop {_ago(hb, now)}", tabs=OVERVIEW_TABS,
                        alive=alive, current="advanced")
        finally:
            conn.close()

    @app.get("/graveyard", response_class=HTMLResponse)
    def graveyard(scope: str = "all", category: str = "all", offset: int = 0):
        conn = db()
        try:
            st = RiskState(conn)
            now = now_ms()
            hb = st.get(HEARTBEAT)
            data = learning_report(conn, cfg)
            try:
                listing = learning_failures(conn, scope, category, offset)
            except ValueError:
                raise HTTPException(400, "Unknown failure history filter") from None
            return page("Why strategies failed", failure_page(data, listing),
                        summary(conn, now)["mode"],
                        f"last trading loop {_ago(hb, now)}",
                        tabs=[("/learning", "Learning & Research"),
                              ("#failure-history", "Failure records")],
                        alive=bool(hb) and 0 <= now - int(hb) < 3 * 60 * 60 * 1000,
                        current="learning")
        finally:
            conn.close()

    return app


# -- overview sections -----------------------------------------------------------

def _latest_price(conn, pair: str, timeframe: str) -> float | None:
    row = conn.execute("SELECT close FROM candles WHERE pair=? AND timeframe=? "
                       "ORDER BY ts DESC LIMIT 1", (pair, timeframe)).fetchone()
    return float(row[0]) if row else None


def _gate_progress(a: dict, cfg: dict, price: float | None, now: int,
                   conn=None) -> str:
    """Transparent paper verification progress from stored observations and fills."""
    g = cfg["graduation"]
    days = max(0.0, (now - a["started_at"]) / DAY_MS)
    ret = a["pnl"] / a["allocation"] if a["allocation"] else 0.0
    hold = net_hold_return(a.get("hold_start_price"), price, cfg)
    ev = evidence(conn, a["account_id"], now) if conn else {}
    observed = ev.get("observed_days", 0)
    verified = ev.get("verified_trades", 0)
    factor = ev.get("profit_factor", 0.0)
    first = ev.get("first_observation_ms")
    span = max(0.0, (now - first) / DAY_MS) if first is not None else 0.0
    gap_hours = ev.get("max_observation_gap_hours", 0)
    last = ev.get("last_observation_ms")
    fresh = last is not None and 0 <= now - last <= max(180_000, cfg["worker"]["poll_seconds"] * 3000)
    dd_limit = g["max_paper_drawdown_pct"] / 100

    def bar(label, frac, value, done, failed=False):
        frac = max(0.0, min(1.0, frac))
        cls = "done" if done else "fail" if failed else ""
        return (f'<div class="row"><span>{label}</span><div class="track">'
                f'<div class="fill {cls}" style="width:{frac * 100:.0f}%"></div></div>'
                f'<span class="v">{value}</span></div>')

    rows = [
        bar("Live-observed paper span", min(days, span) / g["min_paper_days"],
            f'{min(days, span):.1f} / {g["min_paper_days"]}d',
            min(days, span) >= g["min_paper_days"]),
        bar("Distinct market days", observed / g["min_observed_days"],
            f'{observed} / {g["min_observed_days"]}', observed >= g["min_observed_days"]),
        bar("Verified round trips", verified / g["min_paper_trades"],
            f'{verified} / {g["min_paper_trades"]}', verified >= g["min_paper_trades"]),
        bar("After-fee paper return", 1 if ret > 0 else 0,
            f"{ret * 100:+.1f}%", ret > 0, failed=ret <= 0 and verified > 0),
        bar("Profit factor", factor / g["min_paper_profit_factor"],
            f'{factor:.2f} / {g["min_paper_profit_factor"]:g}',
            factor >= g["min_paper_profit_factor"]),
        bar("Max drawdown", 1 if a["max_drawdown"] < dd_limit else 0,
            f'{a["max_drawdown"] * 100:.1f}% (limit {g["max_paper_drawdown_pct"]}%)',
            a["max_drawdown"] < dd_limit, failed=a["max_drawdown"] >= dd_limit),
        bar("Recent verified quote", 1 if fresh else 0,
            "current" if fresh else "missing / stale", fresh, failed=not fresh),
        bar("Longest observation gap", 1 if gap_hours <= g["max_observation_gap_hours"] else 0,
            f'{gap_hours}h / {g["max_observation_gap_hours"]}h',
            gap_hours <= g["max_observation_gap_hours"], failed=gap_hours > g["max_observation_gap_hours"]),
    ]
    if g.get("must_beat_hold"):
        if hold is None:
            rows.append(bar("Beats net holding", 0, "n/a", False))
        else:
            rows.append(bar("Beats net holding", 1 if ret > hold else 0,
                            f"{(ret - hold) * 100:+.1f}%", ret > hold, failed=ret <= hold))
    return ('<div class="gate"><div class="title">Forward paper verification</div>' +
            "".join(rows) + "</div>")


def _sparkline(conn, a: dict, now: int) -> str:
    """Equity over the last 90 days as a small line, with the starting stake as a baseline."""
    rows = conn.execute("SELECT day, equity FROM equity_history WHERE account_id=? "
                        "ORDER BY day DESC LIMIT 90", (a["account_id"],)).fetchall()
    pts = [(int(r["day"]), float(r["equity"])) for r in reversed(rows)]
    today = now // DAY_MS
    if not pts or pts[-1][0] != today:
        pts.append((today, float(a["last_equity"])))
    if len(pts) < 2:
        return ('<div class="spark"><span class="lbl">Equity over time</span>'
                '<p class="muted" style="margin:2px 0 0;font-size:12.5px">Chart appears after '
                'the first full day.</p></div>')
    W, H, P = 300, 56, 4
    base = float(a["allocation"])
    ys = [e for _, e in pts] + [base]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    x0, x1 = pts[0][0], pts[-1][0]
    xspan = (x1 - x0) or 1

    def xy(d, e):
        return P + (d - x0) / xspan * (W - 2 * P), P + (hi - e) / span * (H - 2 * P)

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(d, e) for d, e in pts))
    by = xy(x0, base)[1]
    colour = "var(--good)" if pts[-1][1] >= base else "var(--bad)"
    days = x1 - x0
    return (f'<div class="spark"><span class="lbl">Equity, last {days} day{"s" if days != 1 else ""}'
            f'</span><svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" '
            f'aria-label="Equity over time"><line x1="{P}" x2="{W - P}" y1="{by:.1f}" y2="{by:.1f}" '
            f'stroke="var(--ink-3)" stroke-dasharray="3 3"/><polyline points="{line}" fill="none" '
            f'stroke="{colour}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
            f'</svg></div>')


def _bot_card(a: dict, cfg: dict, conn, now: int, mode: str, extra: str = "") -> str:
    g = Genome.from_dict(json.loads(a["genome_json"]))
    status = a["status"]
    badge = {"paper": ("paper", "PAPER"), "live": ("live", "LIVE"),
             "ready_to_graduate": ("ready", "READY FOR APPROVAL"),
             "approved": ("ready", "APPROVED"), "retired": ("", "RETIRED")}.get(
                 status, ("", status.upper()))
    pos = "open" if a["qty"] else "flat"
    pos_badge = f'<span class="badge open">position open</span>' if a["qty"] else ""
    ret = a["pnl"] / a["allocation"] if a["allocation"] else 0
    stats = (f'<div class="stats">'
             f'<div><b>${a["last_equity"]:.2f}</b><span>equity</span></div>'
             f'<div><b>{_pct(ret)}</b><span>return</span></div>'
             f'<div><b>{a["max_drawdown"] * 100:.1f}%</b><span>max drawdown</span></div>'
             f'<div><b>{a["trades"]}</b><span>trades</span></div></div>')
    meta = (f'<div class="help" style="margin:0 0 8px">Started with ${a["allocation"]:.2f} · '
            f'{escape(pos)} · running {(now - a["started_at"]) / DAY_MS:.0f} days'
            + (f' · protected ${a["protected"]:.2f}' if a["protected"] else "") + '</div>')
    gate = ""
    if mode == "paper" and status == "paper":
        gate = _gate_progress(
            a, cfg, _latest_price(conn, g.pair, cfg["data"]["base_timeframe"]), now, conn)
    spark = _sparkline(conn, a, now) if a.get("account_id") is not None else ""
    if mode == "paper" and a.get("account_id") is not None:
        observations = history(conn, a["account_id"], limit=1)
        last = observations[-1] if observations else None
        fee_text = f' · paid ${last["fees"]:.2f} simulated fees' if last else ''
        meta += f'<div class="help" style="margin:0 0 8px">Verified snapshots: {evidence(conn, a["account_id"], now)["observed_hours"]} hours{fee_text}</div>'
    return (f'<div class="bot"><div class="head"><span class="id">{escape(a["bot_id"])}</span>'
            f'{pos_badge}<span class="badge {badge[0]}">{badge[1]}</span></div>'
            f'<div class="strategy">{escape(g.describe())}</div>{stats}{meta}{spark}{gate}{extra}</div>')


def _overview(conn, cfg: dict, approvals_enabled: bool) -> str:
    now = now_ms()
    s = summary(conn, now)
    st = RiskState(conn)
    mode = s["mode"]
    reasons = st.get("mode_reasons", "")
    risk = cfg["risk"]
    w = cfg["worker"]
    parts: list[str] = []

    # -- 1. status: mode card + system health -------------------------------
    if mode == "LIVE":
        modecard = ('<div class="modecard live"><div class="big">LIVE — REAL MONEY</div>'
                    f'<div class="sub">Live trading can lose money, up to the caps: '
                    f'${risk["daily_loss_limit_usd"]} per day, ${risk["total_drawdown_kill_usd"]} '
                    'in total.</div></div>')
    else:
        modecard = ('<div class="modecard paper"><div class="big">PAPER MODE</div>'
                    '<div class="sub">Fake money, real prices. Nothing real is at risk.</div>'
                    f'<div class="why">Why not live: {escape(reasons) or "live mode has not been unlocked."}'
                    '</div></div>')
    hb = st.get(HEARTBEAT)
    hb_ok = bool(hb) and now - int(hb) < 3 * w["poll_seconds"] * 1000 + 120_000
    market = market_health(conn, cfg, now)
    backfill_done = get_meta(conn, BACKFILL_DONE) is not None
    counts = f'{market["fresh_pairs"]}/{market["total_pairs"]} current pairs'
    if not backfill_done:
        data_txt = f'<span class="soon">downloading history</span> · {counts}'
    elif market["healthy"] and not market["last_errors"]:
        data_txt = f'<span class="ok">healthy</span> · {counts}'
    else:
        bad = ", ".join(market["unhealthy_pairs"]) or market["last_errors"]
        data_txt = f'<span class="no">needs attention</span> · {counts} · {escape(bad[:180])}'
    last_refresh = market["last_attempt_ms"]
    data_txt += f' · refreshed {_ago(last_refresh, now)}' if last_refresh else ' · not refreshed yet'
    today = _utc(now).strftime("%Y-%m-%d")
    acfg = cfg.get("analyst") or {}
    sched = [("Trading loop", f'<span class="{"ok" if hb_ok else "no"}">'
                              f'{"running" if hb_ok else "not running"}</span> · {_ago(hb, now)}'),
             ("Market data", data_txt),
             ("Next evolution", _next_at(w["evolution_hour_utc"], now,
                                         st.get("last_evolution") == today)),
             ("Next daily report", _next_at(w["daily_report_hour_utc"], now,
                                            st.get("last_report") == today))]
    if acfg.get("enabled"):
        sched.append(("Next briefing", _next_at(acfg["hour_utc"], now,
                                                st.get("last_digest") == today)))
    sched.append(("Next backup", _next_at(w["backup_hour_utc"], now,
                                          st.get("last_backup") == today)))
    status = ('<div class="status"><h3>System</h3>' + "".join(
        f'<div class="kv"><span>{escape(k)}</span><span>{v}</span></div>' for k, v in sched)
        + '</div>')
    parts.append(f'<div id="status" class="hero">{modecard}{status}</div>')

    # -- alerts ---------------------------------------------------------------
    flags = [(KILL_SWITCH, "Kill switch is on", "All live trading is stopped until you run "
              "confirm-live again."),
             (CRASH_LOCK, "Waiting for you after a restart", "The worker restarted without a clean "
              "shutdown. It stays in paper mode until you run `confirm-live`. Paper trading is not "
              "affected."),
             (ENTRY_HALT, "New trades are paused", "An API or data problem was detected. Exits still "
              "work; new entries resume after a few healthy loops."),
             (OWNER_PAUSE, "You paused trading", "No new trades until you send /resume on "
              "Telegram. Open positions keep their stops and targets."),
             (DAILY_HALT_UNTIL, "Daily loss limit hit", "Live trading pauses until the next UTC day.")]
    for key, label, explain in flags:
        v = st.get(key)
        if v and not (key == DAILY_HALT_UNTIL and int(v) < now):
            danger = key in (KILL_SWITCH, DAILY_HALT_UNTIL)
            detail = _utc(int(v)).strftime("%Y-%m-%d %H:%M UTC") if key == DAILY_HALT_UNTIL else v
            parts.append(f'<div class="notice{" danger" if danger else ""}"><b>{label}.</b> '
                         f'{explain} <span class="muted">({escape(str(detail))})</span></div>')
    parts.append(
        '<div class="notice">Live trading is never risk-free. If live mode is unlocked, real '
        f'money can be lost up to the configured caps: ${risk["daily_loss_limit_usd"]} per '
        f'day and ${risk["total_drawdown_kill_usd"]} in total before the kill switch, with '
        f'at most ${risk["max_total_capital_usd"]} of capital (hard ceiling '
        f'${HARD_CAPITAL_CEILING_USD:.0f}). Past results do not guarantee future profits.'
        '</div>')

    # -- 2. pipeline ------------------------------------------------------------
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM deployed_bots GROUP BY status")}
    cand = {r["stage"]: r["n"] for r in conn.execute(
        "SELECT stage, COUNT(*) n FROM candidates GROUP BY stage")}
    run = conn.execute("SELECT run_id, data_label, finished_at FROM runs WHERE finished_at IS NOT NULL "
                       "ORDER BY finished_at DESC LIMIT 1").fetchone()
    gens_done = conn.execute("SELECT COUNT(*) FROM generations WHERE run_id=?",
                             (run["run_id"],)).fetchone()[0] if run else 0
    graves = conn.execute("SELECT COUNT(*) FROM graveyard").fetchone()[0]
    waiting_n = counts.get("ready_to_graduate", 0) + counts.get("approved", 0)
    ecfg = cfg["evolution"]
    steps = [
        ("", "1 · Evolution", f'{ecfg["population_size"]}',
         f'bots per generation, {gens_done} generations in the last run, {graves} retired to the '
         'graveyard so far' if run else 'bots per generation. No run finished yet.'),
        ("", "2 · Unseen-data test", f'{cand.get("awaiting_paper", 0) + cand.get("deployed", 0)}',
         f'passed ({cand.get("test_failed", 0)} failed). Only bots that made money in every '
         'training window get this one-time check; passed bots wait for a paper slot.'),
        ("", "3 · Paper trading", f'{counts.get("paper", 0)} / {cfg["paper"]["max_bots"]}',
         f'slots in use. Needs {cfg["graduation"]["min_paper_days"]}+ calendar days, '
         f'{cfg["graduation"]["min_observed_days"]} observed days, '
         f'{cfg["graduation"]["min_paper_trades"]}+ settled trades, profit, low drawdown.'),
        ("hot" if waiting_n else "", "4 · Your approval", f'{waiting_n}',
         'waiting for you' if waiting_n else 'nothing waiting. You get a Telegram alert.'),
        ("live", "5 · Live", f'{counts.get("live", 0)} / {risk["max_live_bots"]}',
         f'bots with real money, at most ${risk["max_per_bot_usd"]} each'
         if mode == "LIVE" else 'locked while in paper mode'),
    ]
    p_status = pilot_status(conn, cfg, now)
    blockers = "; ".join(p_status["configuration_blockers"]) or "pilot switches configured"
    parts.append('<section id="pilot"><h2>Controlled live pilot</h2>'
                 '<p class="help">No real-money pilot may start without 30+ days of genuine '
                 'forward paper trading, independent Kraken validation, and your separate '
                 'funding approval. Software readiness is not profit evidence.</p>'
                 f'<div class="notice"><b>{"OFF — PAPER ONLY" if not p_status["enabled"] else "Pilot configured"}</b>'
                 f' · live accounts: {p_status["live_accounts"]}'
                 f' · allocated: ${p_status["allocated_live_usd"]:.2f}'
                 f' · blockers: {escape(blockers)}</div></section>')

    scale = scale_review(conn, cfg, now)
    reason = "; ".join(scale["review_blockers"]) or "eligible for a separate manual review"
    parts.append(
        '<section id="scaling"><h2>Live performance &amp; capital scaling</h2>'
        '<p class="help">Real-money performance must be measured separately from paper. '
        'Scaling never happens automatically and cannot be earned by backtests. '
        'Future allocations still need a separate owner decision, Kraken verification '
        'and the hard $100 system ceiling.</p>'
        f'<div class="notice"><b>{"OFF — NO SCALING" if not scale["enabled"] else escape(scale["status"].upper())}</b>'
        f' · live evidence accounts: {scale["live_accounts"]}'
        f' · allowable automatic increase: $0'
        f' · blockers: {escape(reason)}</div>'
        '</section>')

    parts.append('<section id="pipeline"><h2>How a bot gets to real money</h2>'
                 '<p class="help">Every bot must pass each stage in order. Losers are retired at '
                 'every step and their cause of death is recorded so new bots avoid it.</p>'
                 '<div class="pipe">' + "".join(
                     f'<div class="step {cls}"><div class="t">{escape(t)}</div><b>{n}</b>'
                     f'<div class="d">{escape(d)}</div></div>' for cls, t, n, d in steps)
                 + '</div></section>')

    # -- 3. results -------------------------------------------------------------
    best = s["best"]
    best_txt = (f'{escape(best["bot_id"])} <span class="lbl">'
                f'{_pct(best["pnl"] / best["allocation"])} ({best["mode"]})</span>') if best else "—"
    parts.append(
        '<section id="results"><h2>Results</h2>'
        '<p class="help">Profit and loss in USD across all bots, since each one started.</p>'
        '<div class="grid">'
        f'<div class="tile"><span class="lbl">Paper P&amp;L ({s["paper_bots"]} bots)</span>'
        f'<b>{_money(s["paper_pnl"])}</b><span class="lbl">today {_money(s["paper_pnl_today"])}</span></div>'
        f'<div class="tile"><span class="lbl">Live P&amp;L ({s["live_bots"]} bots)</span>'
        f'<b>{_money(s["live_pnl"])}</b><span class="lbl">today {_money(s["live_pnl_today"])}</span></div>'
        f'<div class="tile"><span class="lbl">Best bot</span><b style="font-size:16px">{best_txt}</b></div>'
        f'<div class="tile"><span class="lbl">Protected funds</span><b>${s["protected"]:.2f}</b>'
        '<span class="lbl">locked profit, never traded again</span></div>'
        '</div></section>')

    # Research data stays in separate ledgers, never in promoted paper or live.
    league = lab_snapshot(conn, cfg)
    examples = league.get("bots", [])
    lab_rows = "".join(
        '<div class="kv"><span>' + escape(b["name"]) + ' · ' + escape(b["pair"]) +
        ' · ' + str(b["trades"]) + ' settled trades</span><span class="' +
        ('ok' if b["pnl"] >= 0 else 'no') + '">USD ' +
        f'{b["equity"]:.2f} ({b["pnl"]:+.2f})</span></div>'
        for b in examples)
    parts.append(
        '<section id="league"><h2>Experimental paper league</h2>'
        '<p class="help">Real Kraken closed candles, fake 20 USD per experiment, '
        'conservative taker fees + spread + slippage. No backfilled trades and NO '
        'path to real-money approval. This league is deliberately separate '
        'from strictly qualified paper trading.</p>'
        '<div class="grid"><div class="tile"><span class="lbl">Experimental net P&amp;L</span>'
        f'<b class="{"ok" if league.get("pnl", 0) >= 0 else "no"}">'
        f'USD {league.get("pnl", 0):+.2f}</b></div>'
        '<div class="tile"><span class="lbl">Experimental capital (fake)</span>'
        f'<b>USD {league.get("capital", 0):.2f}</b></div>'
        '<div class="tile"><span class="lbl">Closed trades</span>'
        f'<b>{league.get("completed_trades", 0)}</b></div>'
        '<div class="tile"><span class="lbl">Mode</span><b>PAPER ONLY</b></div></div>'
        '<div class="status" style="margin-top:12px"><h3>Live forward experiments</h3>'
        + (lab_rows or '<p class="help">Initializing after a fresh closed Kraken candle.</p>')
        + '</div>' + overview_panel(forward_history(conn, cfg))
        + '</section>')
    research = research_snapshot(conn, cfg)
    news = research.get("sources", [])
    source_rows = "".join(
        '<div class="kv"><a href="' + escape(item["url"], quote=True) +
        '" target="_blank" rel="noopener noreferrer nofollow">' +
        escape(item["title"]) + '</a><span>' + escape(item["source"]) + '</span></div>'
        for item in news[:8])
    note_rows = "".join('<li>' + escape(note) + '</li>' for note in research.get("notes", []))
    parts.append(
        '<section id="research"><h2>Autonomous market research</h2>'
        '<p class="help">Daily Google News research, plus optional Google Programmable Search '
        'when credentials are configured. Web content is untrusted: research can describe '
        'market ideas but never modify trading code, risk limits, approvals or orders.</p>'
        '<div class="status"><h3>Recent research &amp; feedback</h3>'
        + ('<ul>' + note_rows + '</ul>' if note_rows else
           '<p class="help">Research history will appear after the first daily collection.</p>')
        + source_rows + '</div></section>')

    # -- 4. bots ----------------------------------------------------------------
    waiting = conn.execute("SELECT * FROM deployed_bots WHERE status='ready_to_graduate'").fetchall()
    bots_parts = []
    if waiting:
        cards = []
        for b in waiting:
            g = Genome.from_dict(json.loads(b["genome_json"]))
            acc = conn.execute("SELECT * FROM accounts WHERE bot_id=? AND mode='paper' "
                               "ORDER BY account_id DESC LIMIT 1", (b["bot_id"],)).fetchone()
            bid = escape(b["bot_id"])
            if approvals_enabled:
                form = (f'<form class="approve" method="post" action="/bots/{bid}/approve">'
                        '<input type="password" name="token" placeholder="dashboard token" '
                        'required aria-label="dashboard token" autocomplete="current-password"> '
                        '<button class="primary">Approve</button> '
                        f'<button class="danger" formaction="/bots/{bid}/decline">Decline</button></form>')
            else:
                form = ('<p class="muted" style="margin:8px 0 0">Approve from Telegram '
                        '(no DASHBOARD_TOKEN set).</p>')
            if acc:
                a = dict(acc)
                a.update(genome_json=b["genome_json"], status=b["status"],
                         pnl=a["last_equity"] - a["allocation"])
                cards.append(_bot_card(a, cfg, conn, now, "paper", form))
            else:
                cards.append(f'<div class="bot"><div class="head"><span class="id">{bid}</span>'
                             '<span class="badge ready">READY FOR APPROVAL</span></div>'
                             f'<div class="strategy">{escape(g.describe())}</div>{form}</div>')
        bots_parts.append(
            f'<h2>Ready to graduate — needs your approval <span class="count">{len(waiting)}</span></h2>'
            '<p class="help">These bots passed 30+ days of paper trading. Approving one lets it '
            'trade real money once live mode is unlocked, inside the caps. Declining retires it.</p>'
            f'<div class="cards">{"".join(cards)}</div>')

    for mode_name, title, help_txt in (
            ("live", "Live bots", "Trading real money. Each has its own allocation and stop-loss."),
            ("paper", "Paper bots", "Trading fake money at real prices. The bars show how close "
                                    "each one is to graduating.")):
        accs = [a for a in account_rows(conn, mode_name) if a["status"] != "ready_to_graduate"]
        head = f'<h2>{title} <span class="count">{len(accs)}</span></h2><p class="help">{help_txt}</p>'
        if not accs:
            why = ("None yet. Bots arrive here after the nightly evolution and the unseen-data test."
                   if mode_name == "paper" else
                   "None. Live bots appear only after your approval and the live unlock.")
            bots_parts.append(head + f'<p class="muted">{why}</p>')
            continue
        bots_parts.append(head + '<div class="cards">' + "".join(
            _bot_card(a, cfg, conn, now, mode_name) for a in accs) + '</div>')
    parts.append('<section id="bots">' + "<hr style='border:0;border-top:1px solid var(--line);"
                 "margin:16px 0'>".join(bots_parts) + '</section>')

    # -- 5. evolution -----------------------------------------------------------
    evo = ['<section id="evolution"><h2>Evolution</h2>'
           '<p class="help">Every night at '
           f'{w["evolution_hour_utc"]:02d}:00 UTC, {ecfg["population_size"]} bots are backtested on '
           f'{cfg["data"]["history_years"]} years of history for {ecfg["generations"]} generations. '
           'The worst die, the best breed. Fitness = risk-adjusted return after fees, minus '
           'penalties.</p>']
    if run:
        gens = [dict(r) for r in conn.execute(
            "SELECT generation, best, median, worst, failure_summary FROM generations "
            "WHERE run_id=? ORDER BY generation", (run["run_id"],))]
        evo.append(f'<p class="muted" style="font-size:13px">Last run {escape(run["run_id"])} · '
                   f'{escape(run["data_label"])}</p>{progress_chart(gens)}')
        if gens:
            evo.append(f'<p style="font-size:14px">{escape(gens[-1]["failure_summary"])}</p>')
        top = conn.execute("SELECT * FROM population WHERE run_id=? ORDER BY rank LIMIT 10",
                           (run["run_id"],)).fetchall()
        rows = []
        for r in top:
            sm = json.loads(r["summary_json"])
            g = Genome.from_dict(json.loads(r["genome_json"]))
            rows.append([str(r["rank"] + 1), escape(g.describe()), f'{r["fitness"]:.3f}',
                         _pct(sm["return"]), _pct(sm["hold_return"]),
                         f'{sm["max_drawdown"] * 100:.1f}%', str(sm["trades"])])
        evo.append('<h2 style="margin-top:16px">Top 10 of the last run</h2>'
                   '<p class="help">Ranked on the training period. "Hold" is what simply holding '
                   'the coin would have returned over the same time.</p>'
                   + _table([("#", "n"), ("Strategy", "wide"), ("Fitness", "n"), ("Return", "n"),
                             ("Hold", "n"), ("Max DD", "n"), ("Trades", "n")], rows))
        summaries = conn.execute("SELECT generation, failure_summary FROM generations WHERE "
                                 "run_id=? ORDER BY generation DESC LIMIT 5",
                                 (run["run_id"],)).fetchall()
        evo.append('<h2 style="margin-top:16px">What killed bots recently</h2><ul>' + "".join(
            f"<li>{escape(r['failure_summary'])}</li>" for r in summaries) + '</ul>')
    else:
        evo.append('<p class="muted">No evolution run has finished yet. The first one starts '
                   f'{_next_at(w["evolution_hour_utc"], now)} once the history download is done.</p>')
    evo.append('</section>')
    parts.append("".join(evo))

    # -- 6. briefing ------------------------------------------------------------
    digest = latest_digest(conn)
    fg = latest_fear_greed(conn)
    if fg:
        label = ("extreme fear" if fg[1] < 25 else "fear" if fg[1] < 45 else "neutral" if fg[1] < 55
                 else "greed" if fg[1] < 75 else "extreme greed")
        mood = (f'<div class="mood"><div>Fear &amp; Greed Index: <b>{fg[1]}</b>/100 '
                f'<span class="muted">{label}, {_utc(fg[0]):%Y-%m-%d}</span></div>'
                f'<div class="dial"><i style="left:{fg[1]}%"></i></div></div>')
    else:
        mood = '<p class="muted">No Fear &amp; Greed data yet.</p>'
    when = (f'<p class="muted" style="font-size:13px">Written {_utc(digest["ts"]):%Y-%m-%d %H:%M} UTC'
            f'{" by Claude" if digest["used_ai"] else " (plain digest, no AI key set)"}.</p>'
            ) if digest else ""
    parts.append('<section id="briefing"><h2>Market mood and AI analyst</h2>'
                 '<p class="help">The analyst reads crypto news and the public channels you list and '
                 'writes you a briefing. It is information only: the trading bots never read it '
                 'and it cannot place or change a trade.</p>' + mood
                 + (when + f'<div class="digest">{escape(digest["text"])}</div>' if digest
                    else '<p class="muted">No briefing yet.</p>') + '</section>')

    # -- 7. risk log -------------------------------------------------------------
    events = conn.execute("SELECT * FROM risk_events ORDER BY ts DESC LIMIT 20").fetchall()
    ev = "".join(
        f'<li><time>{_utc(e["ts"]):%Y-%m-%d %H:%M} UTC</time><span class="kind">'
        f'{escape(e["kind"].replace("_", " "))}</span>{escape(e["detail"])}</li>' for e in events)
    parts.append('<section id="events"><h2>Risk log</h2>'
                 '<p class="help">Everything the risk guard refused, halted or changed. Last 20.</p>'
                 + (f'<ul class="events" style="list-style:none;padding:0;margin:0">{ev}</ul>'
                    if events else '<p class="muted">Nothing yet.</p>') + '</section>')

    # -- 8. limits and how it works ---------------------------------------------
    parts.append(
        '<section id="limits"><h2>Hard limits</h2>'
        '<p class="help">Set in bots/config.yaml. The code refuses anything above the $100 ceiling.</p>'
        + _table([("Rule", "wide"), ("Value", "n")], [
            ["Max total live capital", f"${risk['max_total_capital_usd']}"],
            ["Max per bot", f"${risk['max_per_bot_usd']}"],
            ["Max live bots", str(risk["max_live_bots"])],
            ["Daily loss limit", f"${risk['daily_loss_limit_usd']}"],
            ["Total drawdown kill switch", f"${risk['total_drawdown_kill_usd']}"],
            ["live_enabled in config", "yes" if cfg.get("live_enabled") is True else "no"]])
        + '<h2 style="margin-top:16px">How to read this page</h2><div class="how">'
        '<div><b>Paper mode</b>Fake money at real prices. Every bot starts here.</div>'
        '<div><b>Graduation</b>A paper bot needs 30+ real days, at least 21 observed UTC '
        'market days, 20 settled trades, profit after fees, profit factor at least 1.1, '
        'drawdown under 10%, no long data gaps, and better returns than net buy-and-hold.</div>'
        '<div><b>Drawdown</b>The worst drop from a bot\'s peak equity. Smaller is safer.</div>'
        '<div><b>ATR stop</b>Some bots place their stop a multiple of the recent average candle '
        'range (ATR) below entry, so it widens in wild markets and tightens in calm ones. It is '
        'always kept within the fixed stop\'s 0.5–5% range.</div>'
        '<div><b>Entries and fees</b>Default entry and exit execution uses market orders, '
        'charged conservative taker fees and spread/slippage. Maker-limit experiments may '
        'go unfilled; their simulated fills are never assumed from candle highs/lows.</div>'
        '<div><b>Trailing stop, time exit</b>Some bots also raise their stop as price climbs '
        '(locking in gains) or close a trade after a set number of candles. Evolution decides '
        'whether either helps.</div>'
        '<div><b>Protected funds</b>When a live bot doubles its money, its original stake is locked '
        'and never traded again.</div>'
        '<div><b>Kill switch</b>If total live losses hit the limit, everything is closed and live '
        'stays off until you confirm again.</div>'
        '<div><b>Going live</b>Needs MODE=LIVE, live_enabled: true, LIVE_CONFIRMED.txt and a '
        'Kraken key that cannot withdraw. Anything missing keeps paper mode.</div>'
        '</div></section>')
    return "".join(parts)


def _graveyard(conn) -> str:
    causes = conn.execute("SELECT diagnosis, COUNT(*) n FROM graveyard GROUP BY diagnosis "
                          "ORDER BY n DESC").fetchall()
    total = sum(r["n"] for r in causes)
    rows = conn.execute("SELECT * FROM graveyard ORDER BY id DESC LIMIT 200").fetchall()
    cause_rows = []
    for r in causes:
        share = r["n"] / total if total else 0
        cause_rows.append([escape(r["diagnosis"]), str(r["n"]),
                           f'<div class="track" style="height:8px;border-radius:4px;background:var(--line)">'
                           f'<div style="width:{share * 100:.0f}%;height:100%;background:var(--series-2);'
                           f'border-radius:4px"></div></div>'])
    body = [f'<section><h2>Causes of death <span class="count">{total} bots</span></h2>'
            '<p class="help">Why bots were retired during evolution. New bots are steered away from '
            'repeating the most common causes.</p>'
            + (_table([("Diagnosis", "wide"), ("Bots", "n"), ("Share", "wide")], cause_rows)
               if causes else '<p class="muted">No bot has died yet.</p>') + '</section>']
    items = []
    for r in rows:
        g = Genome.from_dict(json.loads(r["genome_json"]))
        items.append([f'{escape(r["run_id"])} / {r["generation"]}', escape(g.describe()),
                      escape(r["cause"]), escape(r["diagnosis"]),
                      f'{escape(r["regime"])}, vol {r["volatility"] * 100:.0f}%'])
    body.append('<section><h2>Latest 200 dead bots</h2>'
                + (_table([("Run / gen", ""), ("Strategy", "wide"), ("Death rule", ""),
                           ("Why", "wide"), ("Market", "")], items)
                   if items else '<p class="muted">Nothing here yet.</p>') + '</section>')
    return "".join(body)
