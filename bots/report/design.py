"""Phase 1 visual language shared by every dashboard screen.

Presentation only. Never issues trades, changes risk settings, or inflates P&L.
All user-visible dynamic strings must be HTML-escaped by their callers.
"""
from __future__ import annotations

from html import escape

NAV = (
    ("home", "Home", "/", "01"),
    ("bots", "My Bots", "/bots", "02"),
    ("performance", "Performance", "/research/forward", "03"),
    ("learning", "Learning & Research", "/learning", "04"),
    ("safety", "Safety", "/advanced#status", "05"),
    ("settings", "Settings", "/advanced#limits", "06"),
)

# Added *after* legacy CSS, so the existing report components inherit the
# same typography, surfaces and accessibility improvements.
NEXT_CSS = """
:root {
  --surface:#f4f7fc; --panel:#fff; --panel-2:#f7f9fd; --ink:#152235;
  --ink-2:#526278; --ink-3:#738297; --line:#e4ebf3;
  --accent:#3864dc; --good:#168462; --bad:#ca4560; --warn:#b87524;
  --paper:#3864dc; --paper-soft:#eaf0ff; --good-soft:#e3f6ee;
  --warn-soft:#fff5e3; --live-soft:#fff0f3;
  --series-1:#3864dc; --series-2:#eb9260; --series-3:#159a7f;
  --shadow:0 8px 32px rgba(34,58,99,.055);
}
:root[data-theme="dark"] {
  --surface:#0c1423; --panel:#152137; --panel-2:#1c2a43; --ink:#f3f6ff;
  --ink-2:#c3cede; --ink-3:#92a6bf; --line:#2b3952; --accent:#8ba9ff;
  --good:#67e7b2; --bad:#ff889e; --warn:#ffd08a; --paper:#a1b9ff;
  --paper-soft:#25365c; --good-soft:#184638; --warn-soft:#4a371e;
  --live-soft:#482536; --series-1:#88a9fc; --series-2:#ffae77;
  --series-3:#57cbaa; --shadow:0 8px 32px rgba(0,0,0,.12);
}
@media (prefers-color-scheme:dark) {
 :root:not([data-theme="light"]) {
  --surface:#0c1423; --panel:#152137; --panel-2:#1c2a43; --ink:#f3f6ff;
  --ink-2:#c3cede; --ink-3:#92a6bf; --line:#2b3952; --accent:#8ba9ff;
  --good:#67e7b2; --bad:#ff889e; --warn:#ffd08a; --paper:#a1b9ff;
  --paper-soft:#25365c; --good-soft:#184638; --warn-soft:#4a371e;
  --live-soft:#482536; --series-1:#88a9fc; --series-2:#ffae77;
  --series-3:#57cbaa; --shadow:0 8px 32px rgba(0,0,0,.12);
 }
}
html{scroll-behavior:smooth;scroll-padding-top:84px}
body{font:15px/1.52 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;letter-spacing:-.008em}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,
summary:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
a{color:var(--accent)}.app-shell{display:grid;grid-template-columns:250px minmax(0,1fr);min-height:100vh}
.side{z-index:31;background:#111e36;color:#f4f7ff;padding:27px 16px 20px;
  position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:26px}
.side-brand{display:flex;align-items:center;gap:12px;text-decoration:none!important;color:#fff!important;
  font-size:17px;font-weight:750;letter-spacing:-.035em;padding:3px 10px}
.brand-mark{width:34px;height:34px;display:grid;place-items:center;border-radius:12px;
  background:linear-gradient(135deg,#83adff,#3155c8);color:#fff;font-size:18px}
.brand-note{font-size:11px;font-weight:500;letter-spacing:.04em;color:#a8b9d5;text-transform:uppercase}
.side-group-label{font-size:11px;letter-spacing:.12em;color:#8e9fbd;
  font-weight:700;margin:0 12px 9px;text-transform:uppercase}
.primary-nav{display:flex;flex-direction:column;gap:4px}
.primary-nav a{display:flex;align-items:center;gap:12px;padding:11px 12px;color:#b6c7dd;
  text-decoration:none;border-radius:11px;font-size:13.5px;font-weight:560;transition:background .15s}
.primary-nav a:hover{background:#243655;color:#fff}
.primary-nav a[aria-current="page"]{background:#294a86;color:#fff;font-weight:720}
.nav-icon{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;
  background:rgba(255,255,255,.07);font-size:11px;font-weight:800;font-variant-numeric:tabular-nums}
.primary-nav a[aria-current="page"] .nav-icon{background:rgba(255,255,255,.17)}
.side-foot{margin-top:auto;padding:14px 12px;border-radius:13px;background:#1c2c49;
  font-size:12px;line-height:1.5;color:#c0cfe5}
.side-foot strong{color:#fff;display:block;margin-bottom:4px}
.side-foot a{color:#d9e6ff}
.app-area{min-width:0}
.top{position:sticky;top:0;z-index:25;background:var(--panel);border-bottom:1px solid var(--line);
  box-shadow:0 2px 14px rgba(20,33,55,.035)}
.bar{max-width:1320px;min-height:74px;padding:11px 34px;gap:10px}
.bar .brand{font-size:16px;letter-spacing:-.03em;font-weight:750}
.top-copy{min-width:180px}
.top-eyebrow{font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.11em;font-weight:760}
.top-title{font-size:19px;font-weight:770;line-height:1.3;letter-spacing:-.035em}
.mode{margin-left:auto;font-size:11px;letter-spacing:.03em;padding:8px 11px}
.mode.paper{background:var(--good-soft);color:var(--good)}
.mode.live{background:var(--live-soft);color:var(--bad)}
.hb{margin-left:0;font-size:12px}
.view-switch{display:flex;align-items:center;gap:2px;background:var(--panel-2);
 padding:4px;border:1px solid var(--line);border-radius:12px;margin-left:7px}
.view-switch a{padding:7px 11px;text-decoration:none;border-radius:8px;color:var(--ink-2);
 font-size:12px;font-weight:700}
.view-switch a[aria-current="true"]{background:var(--panel);color:var(--accent);
 box-shadow:0 1px 5px rgba(27,50,85,.12)}
.tabs{max-width:1320px;padding:0 29px}.tabs a{font-size:12px;padding:9px 11px}
main{max-width:1320px;padding:25px 34px 70px}
main section{border:1px solid var(--line);border-radius:18px;padding:24px;
 box-shadow:var(--shadow);margin:15px 0;background:var(--panel)}
main h2{font-size:18px;letter-spacing:-.025em;margin-bottom:7px}
.help{color:var(--ink-3);line-height:1.55}.grid{gap:13px}
.tile{border-radius:14px;background:var(--panel-2);padding:16px}
.tile b{font-size:24px;letter-spacing:-.035em}
.cards{gap:13px}.bot{border-radius:14px;padding:18px}
.bot .head{margin-bottom:9px}
.notice{line-height:1.6}.notice.danger{background:var(--live-soft)}
.status{box-shadow:none;border-radius:14px}
button,.button-link{border-radius:10px;padding:10px 15px;transition:background .15s}
button:hover,.button-link:hover{filter:brightness(.97)}
button:disabled{opacity:.55;cursor:not-allowed}
.stack td{overflow-wrap:anywhere}
.section-jump{padding:2px 0}
.hb-compact{font-size:11px;color:var(--ink-3)}
.home-page{display:flex;flex-direction:column;gap:19px}
.home-page section{margin:0}
.home-heading{margin:5px 0 0}
.home-heading h1{font-size:clamp(27px,3.4vw,39px);letter-spacing:-.055em;line-height:1.15;margin:4px 0}
.home-heading p{color:var(--ink-2);margin:9px 0 0;max-width:750px}
.home-eyebrow{display:inline-flex;align-items:center;gap:8px;letter-spacing:.12em;
 font-size:11px;font-weight:800;text-transform:uppercase;color:var(--accent)}
.home-eyebrow:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--accent)}
.home-hero{background:#172948!important;color:#fff;border:0!important;
  padding:27px 30px!important;display:grid;grid-template-columns:1.45fr 1fr;gap:24px;align-items:center;
  background-image:radial-gradient(circle at 88% 20%,rgba(104,152,252,.22),transparent 35%),
  linear-gradient(135deg,#172948,#0e1a30)!important}
.home-hero .hero-status{display:inline-flex;align-items:center;gap:8px;font-size:12px;
  font-weight:760;padding:7px 10px;background:rgba(255,255,255,.12);border-radius:999px}
.hero-status .dot{width:8px;height:8px;background:#69dfb9;border-radius:50%}
.home-hero.attention .hero-status .dot{background:#ffcc77}
.home-hero.live .hero-status .dot{background:#ff8291}
.home-hero h2{font-size:clamp(22px,3vw,31px);margin:14px 0 8px;color:#fff}
.home-hero p{margin:0;color:#d3deef;max-width:640px}
.hero-ministats{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.hero-ministats>div{padding:16px;border:1px solid rgba(255,255,255,.15);
  background:rgba(255,255,255,.08);border-radius:13px}
.hero-ministats strong{display:block;font-size:23px;line-height:1.35;font-variant-numeric:tabular-nums}
.hero-ministats span{display:block;font-size:11px;color:#c7d5ea}
.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px}
.kpi-card{background:var(--panel);padding:18px 20px;border:1px solid var(--line);
 border-radius:16px;box-shadow:var(--shadow)}
.kpi-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.kpi-label{font-size:12px;color:var(--ink-2);font-weight:680}
.info-dot{display:inline-grid;place-items:center;width:19px;height:19px;border-radius:50%;
 background:var(--panel-2);border:1px solid var(--line);font-size:11px;
 color:var(--ink-2);cursor:help}
.kpi-value{font-size:clamp(21px,2.2vw,29px);font-weight:820;letter-spacing:-.055em;
 margin:10px 0 4px;line-height:1.2;font-variant-numeric:tabular-nums}
.kpi-foot{font-size:12px;color:var(--ink-3);line-height:1.4}
.home-two-col{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(280px,.8fr);gap:16px;align-items:start}
.section-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px}
.section-head h2{margin:0}.text-link{font-size:12px;font-weight:750;text-decoration:none}
.text-link:hover{text-decoration:underline}
.experiment-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px}
.experiment{padding:17px;border-radius:14px;background:var(--panel-2);border:1px solid var(--line)}
.experiment-name{font-weight:760;font-size:15px;letter-spacing:-.025em}
.experiment-pair{font-size:12px;color:var(--ink-3);margin:2px 0 12px}
.experiment-balance{font-weight:800;font-size:23px;font-variant-numeric:tabular-nums}
.experiment-pnl{font-size:13px;font-weight:700;margin:4px 0 11px}
.experiment-state{display:inline-flex;border-radius:999px;font-size:11px;
 padding:5px 9px;background:var(--paper-soft);color:var(--paper);font-weight:700}
.experiment-state.holding{background:var(--good-soft);color:var(--good)}
.experiment-state.warning{background:var(--warn-soft);color:var(--warn)}
.next-steps{margin:0;padding:0;list-style:none}
.next-steps li{display:flex;gap:12px;align-items:start;padding:13px 0;border-top:1px solid var(--line)}
.next-steps li:first-child{border:0;padding-top:5px}
.step-icon{flex:0 0 30px;height:30px;display:grid;place-items:center;
 background:var(--paper-soft);color:var(--paper);border-radius:10px;font-weight:800;font-size:12px}
.next-steps b{display:block;font-size:13px}
.next-steps span{display:block;color:var(--ink-2);font-size:12px;margin-top:3px}
.pill-line{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.status-pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 10px;
 background:var(--panel-2);font-size:12px;font-weight:650;color:var(--ink-2)}
.status-pill.good{background:var(--good-soft);color:var(--good)}
.status-pill.bad{background:var(--live-soft);color:var(--bad)}
.status-pill.warn{background:var(--warn-soft);color:var(--warn)}
.home-empty{padding:29px 18px;text-align:center;background:var(--panel-2);border:1px dashed var(--line);border-radius:14px}
.home-empty b{display:block;font-size:15px;margin-bottom:6px}
.home-empty p{margin:0;color:var(--ink-2);font-size:12px}
.home-chart{margin-top:10px}
.home-chart .notice{border:1px dashed var(--line);background:var(--panel-2);border-radius:13px;
  padding:22px 14px;text-align:center;color:var(--ink-2);font-size:13px}
.status-list{display:grid;gap:12px}
.status-row{display:flex;align-items:center;justify-content:space-between;gap:12px;
 border-bottom:1px solid var(--line);padding:9px 0;font-size:13px}
.status-row:last-child{border-bottom:0}
.status-row span{color:var(--ink-2)}
.status-row strong{font-weight:710;text-align:right}
.inline-tip{font-size:12px;color:var(--ink-3);margin-top:13px}
.advanced-note{font-size:12px;padding:9px 12px;border-radius:10px;background:var(--panel-2);
 color:var(--ink-2);margin:12px 0}
@media (max-width:1150px){
 .app-shell{grid-template-columns:212px minmax(0,1fr)}
 .side{padding:20px 10px}.bar{padding:11px 20px}main{padding:22px 20px 70px}
 .experiment-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:850px){
 .app-shell{grid-template-columns:1fr}
 .side{position:static;width:100%;height:auto;min-height:0;padding:11px 12px;gap:8px}
 .side-brand{font-size:15px;padding:0 8px}.brand-mark{width:28px;height:28px}
 .side-group-label,.side-foot{display:none}
 .primary-nav{flex-direction:row;overflow-x:auto;gap:3px;scrollbar-width:none;-webkit-overflow-scrolling:touch}
 .primary-nav::-webkit-scrollbar{display:none}
 .primary-nav a{white-space:nowrap;padding:9px 11px;font-size:12px;gap:5px}
 .nav-icon{display:none}
 .bar{min-height:64px;padding:10px 15px;flex-wrap:wrap}
 main{padding:18px 13px 64px}
 .home-hero{grid-template-columns:1fr}
 .hero-ministats{grid-template-columns:repeat(2,1fr)}
 .tabs{padding:0 10px}
}
@media (max-width:620px){
 .top-title{font-size:16px}.hb{display:none}.mode{margin-left:auto;font-size:10px}
 .view-switch{margin-left:0}.view-switch a{font-size:11px;padding:7px}
 .home-two-col{grid-template-columns:1fr}
 .experiment-grid{grid-template-columns:1fr}
 .kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
 .kpi-card{padding:14px 12px}
 .kpi-value{font-size:22px}
 main section{padding:17px 14px}
 .home-hero{padding:21px 19px!important}
 .hero-ministats>div{padding:12px}
 .hero-ministats strong{font-size:19px}
 .section-head{flex-wrap:wrap}
 .pipe{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:380px){.kpi-grid{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){
 html{scroll-behavior:auto}
 *,*::before,*::after{transition:none!important}
}

/* Phase 2: crisp data hierarchy and usable filter controls on every screen. */
.perf-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:17px 0}
.perf-tile{display:flex;flex-direction:column;gap:9px;background:var(--panel);
 border:1px solid var(--line);border-radius:16px;padding:19px;box-shadow:var(--shadow)}
.perf-tile .perf-title{font-size:12px;font-weight:730;color:var(--ink-2)}
.perf-tile strong{font-size:clamp(22px,2.1vw,29px);line-height:1.15;letter-spacing:-.045em;
 font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.perf-tile small{font-size:12px;color:var(--ink-3);line-height:1.5}
.perf-filters{display:flex;gap:13px;align-items:end;flex-wrap:wrap;margin:13px 0 18px}
.perf-filters label{display:grid;gap:6px;color:var(--ink-2);font-size:12px;font-weight:700;min-width:145px}
.perf-filters select,.perf-filters input{height:41px;width:100%;max-width:265px;
 padding:9px 12px;border:1px solid var(--line);background:var(--panel);
 color:var(--ink);border-radius:10px;font:inherit;font-size:13px}
.perf-filters button{min-height:41px;background:var(--accent);color:#fff;border-color:transparent}
.perf-filters .text-link{padding:11px 4px}
.period-tabs{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin:18px 0 15px}
.period-tabs a{padding:9px 13px;border-radius:10px;border:1px solid var(--line);
 text-decoration:none;background:var(--panel);color:var(--ink-2);font-size:12px;font-weight:720}
.period-tabs a:hover,.period-tabs a[aria-current="page"]{background:var(--paper-soft);color:var(--accent);
 border-color:var(--accent)}
.perf-summary{margin-top:10px!important;padding:12px 20px!important}
.fee-card{padding:14px 0}
.fee-bar{display:flex;width:100%;height:16px;border-radius:999px;overflow:hidden;background:var(--line)}
.fee-bar span:first-child{background:var(--series-1)}
.fee-bar span:last-child{background:var(--series-2)}
.fee-legend{display:flex;gap:22px;flex-wrap:wrap;margin-top:16px}
.fee-legend span{font-size:13px;color:var(--ink-2)}
.fee-legend b{margin-left:6px;color:var(--ink)}
.trade-table-wrap{max-width:100%;overflow-x:auto}
.perf-table{min-width:780px}
.perf-table td{font-size:12px;padding:12px 8px;vertical-align:middle}
.perf-table th{font-size:12px}
.trade-detail{font-size:12px;min-width:100px;color:var(--ink-2)}
.trade-detail summary,.rule-details summary{color:var(--accent);cursor:pointer;font-weight:690}
.trade-detail p{margin:6px 0}
.rule-details{padding:13px 15px;background:var(--panel-2);border-radius:11px}
.rule-details p{margin-bottom:0;overflow-wrap:anywhere}
.export-links{display:flex;gap:17px;flex-wrap:wrap;margin:18px 0 0;font-size:12px;font-weight:700}
.export-links a{text-decoration:none}
.export-links a:hover{text-decoration:underline}
.bot-directory{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:16px 0}
.bot-directory-card{border:1px solid var(--line);background:var(--panel-2);
 border-radius:15px;padding:19px;display:flex;flex-direction:column;gap:7px}
.bot-directory-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.bot-directory-top h3{margin:0;font-size:16px;letter-spacing:-.03em}
.bot-directory-top span{color:var(--ink-3);font-size:12px}
.bot-directory-top .status-pill{font-size:10px;white-space:normal;text-align:right}
.bot-directory-value{font-size:28px;font-weight:800;letter-spacing:-.04em;margin-top:7px}
.bot-directory-card p{font-size:12px;color:var(--ink-2);min-height:37px}
.bot-directory-card .button-link{margin-top:auto;align-self:flex-start;display:inline-flex;
 padding:9px 13px;background:var(--paper-soft);color:var(--accent);font-size:12px;
 font-weight:730;text-decoration:none}
.experiment .bot-detail-link{margin-top:12px;display:inline-flex;font-size:12px;
 text-decoration:none;font-weight:750;color:var(--accent)}
@media(max-width:1150px){
 .perf-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .bot-directory{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:660px){
 .perf-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
 .perf-tile{padding:14px 12px}
 .perf-tile strong{font-size:22px}
 .bot-directory{grid-template-columns:1fr}
 .perf-filters label{flex:1 1 125px;min-width:0}
 .perf-filters input,.perf-filters select{max-width:none}
 .trade-table-wrap{overflow:visible}
 .perf-table{min-width:0}
 .fee-legend{gap:9px}
 .fee-legend span{font-size:11px}
}


.equity-interactive{position:relative}
.forward-equity{cursor:crosshair;border-radius:11px;outline-offset:4px}
.equity-chart-tip{font-size:12px;color:var(--ink-2);margin-top:8px;
 padding:11px 13px;background:var(--panel-2);border:1px solid var(--line);border-radius:10px;
 font-variant-numeric:tabular-nums;min-height:42px}


/* Phase 3: human-readable learning timeline, research and rejection charts */
.learning-kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:13px;margin:18px 0}
.learning-kpi{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:17px;
 box-shadow:var(--shadow);display:flex;flex-direction:column;gap:7px}
.learning-kpi strong{font-size:26px;letter-spacing:-.04em;font-variant-numeric:tabular-nums;color:var(--ink)}
.learning-kpi small{color:var(--ink-3);font-size:12px;line-height:1.45}
.learn-warning{padding:15px 18px;border-radius:14px;background:var(--paper-soft);
 border:1px solid var(--line);display:grid;gap:4px;margin:17px 0;font-size:13px}
.learn-warning b{color:var(--accent)}
.learn-warning span{color:var(--ink-2)}
.learning-funnel{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:17px 0}
.learn-step{display:flex;flex-direction:column;gap:10px;border:1px solid var(--line);border-radius:13px;
 background:var(--panel-2);padding:14px}
.learn-step .step-icon{flex:0 0 30px;width:30px}
.learn-step b{font-size:13px}
.learn-step span.help{font-size:12px;display:block;margin:6px 0 0}
.learn-step strong{font-size:18px;white-space:normal;margin-top:auto}
.learn-strategy-list,.research-cards,.failure-records{display:grid;gap:10px}
.learning-strategy,.research-card,.failure-record{border:1px solid var(--line);
 background:var(--panel-2);padding:15px 17px;border-radius:13px}
.learn-strategy-head{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.learning-strategy p,.research-card p{font-size:12px;color:var(--ink-2);line-height:1.6}
.learn-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:10px 0}
.learn-metrics span{display:grid;gap:3px;font-size:11px;color:var(--ink-3)}
.learn-metrics b{font-size:14px;color:var(--ink)}
.learning-strategy details summary,.research-card details summary{cursor:pointer;font-size:12px;
 color:var(--accent);font-weight:720}
.failure-chart{display:grid;gap:12px;margin:18px 0}
.failure-bar-row{display:grid;grid-template-columns:minmax(165px,1fr) 2.2fr 100px;
 gap:12px;align-items:center;text-decoration:none;color:var(--ink);font-size:12px}
.failure-bar-label{font-weight:690;color:var(--ink-2)}
.failure-bar-track{display:block;height:13px;border-radius:10px;overflow:hidden;background:var(--panel-2);
 border:1px solid var(--line)}
.failure-bar-track i{display:block;height:100%;background:var(--series-2);
 border-radius:10px;transition:width .25s ease}
.failure-bar-row strong{text-align:right;font-variant-numeric:tabular-nums}
.failure-bar-row small{font-weight:500;color:var(--ink-3)}
.failure-bar-row:hover .failure-bar-label{text-decoration:underline}
.research-source{font-size:11px;color:var(--ink-3);font-weight:650}
.research-card h3{font-size:15px;line-height:1.5;margin:8px 0 4px}
.research-card h3 a{text-decoration:none;overflow-wrap:anywhere}
.research-card h3 a:hover{text-decoration:underline}
.research-card details p{overflow-wrap:anywhere}
.research-notes{padding-left:20px;margin:0}
.research-notes li{font-size:13px;padding:5px 0;color:var(--ink-2)}
.learn-faq{display:grid;gap:9px}
.learn-faq details{padding:14px 16px;border-radius:12px;border:1px solid var(--line);background:var(--panel-2)}
.learn-faq summary{cursor:pointer;font-size:13px;font-weight:740}
.learn-faq p{font-size:13px;color:var(--ink-2)}
.learn-category-nav{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0 20px}
.learn-category-nav a{border:1px solid var(--line);border-radius:999px;padding:8px 12px;
 font-size:12px;font-weight:700;color:var(--ink-2);text-decoration:none;background:var(--panel)}
.learn-category-nav a[aria-current="page"],.learn-category-nav a:hover{background:var(--paper-soft);
 border-color:var(--accent);color:var(--accent)}
.failure-record summary{display:flex;align-items:center;justify-content:space-between;gap:12px;
 cursor:pointer}
.failure-record summary span:first-child{display:grid;gap:5px;min-width:0}
.failure-record summary b{font-size:13px;overflow-wrap:anywhere}
.failure-record summary small{font-size:11px;color:var(--ink-3)}
.failure-record summary .status-pill{max-width:32%;overflow-wrap:anywhere;font-size:10px}
.failure-record p{font-size:13px;overflow-wrap:anywhere}
.learn-pages{display:flex;gap:10px;margin:16px 0;flex-wrap:wrap}
.learn-pages a{padding:10px 14px;border:1px solid var(--line);border-radius:9px;
 font-weight:700;font-size:12px;text-decoration:none;background:var(--panel-2)}
@media(max-width:1200px){.learning-funnel{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:700px){
 .learning-funnel{grid-template-columns:repeat(2,minmax(0,1fr))}
 .learn-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
 .failure-bar-row{grid-template-columns:minmax(110px,1fr) 1.7fr 65px;gap:7px}
 .failure-bar-row strong{font-size:11px}
}
@media(max-width:430px){
 .learning-kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
 .learning-kpi{padding:12px}
 .learning-kpi strong{font-size:21px}
 .learning-funnel{grid-template-columns:1fr}
 .failure-bar-row{grid-template-columns:1fr 1fr;gap:4px}
 .failure-bar-label{grid-column:span 2}
 .failure-bar-row strong{order:3;text-align:right}
}

"""
NEXT_JS = """
(function(){
  var root=document.documentElement;
  var themeKey='trading-dashboard-theme', viewKey='trading-dashboard-view';
  try{
    var theme=localStorage.getItem(themeKey);
    if(theme==='dark'||theme==='light')root.setAttribute('data-theme',theme);
    if(window.location.pathname==='/'&&!window.location.hash&&
       localStorage.getItem(viewKey)==='advanced'){
      window.location.replace('/advanced');return;
    }
  }catch(e){}
  document.querySelectorAll('[data-set-view]').forEach(function(el){
    el.addEventListener('click',function(){
      try{localStorage.setItem(viewKey,el.getAttribute('data-set-view'));}catch(e){}
    });
  });
  document.querySelectorAll('[data-toggle-theme]').forEach(function(el){
    el.addEventListener('click',function(){
      var now=root.getAttribute('data-theme');
      var dark=now==='dark'||(!now&&window.matchMedia&&
               window.matchMedia('(prefers-color-scheme: dark)').matches);
      var next=dark?'light':'dark';
      root.setAttribute('data-theme',next);
      try{localStorage.setItem(themeKey,next);}catch(e){}
      el.textContent=next==='dark'?'Light theme':'Dark theme';
    });
    el.textContent=root.getAttribute('data-theme')==='dark'?'Light theme':'Dark theme';
  });

  // Genuine observed marks only: never interpolate missing hours or create fills.
  document.querySelectorAll('.forward-equity[data-points]').forEach(function(svg){
    var points, at=0;
    try{points=JSON.parse(svg.getAttribute('data-points'));}catch(e){return;}
    if(!points || points.length<2)return;
    var info=svg.parentElement.querySelector('.equity-chart-tip');
    if(!info)return;
    function cash(n){return (n>=0?'+':'')+String.fromCharCode(36)+Number(n).toFixed(2);}
    function show(i){
      at=Math.max(0,Math.min(points.length-1,i));
      var p=points[at];
      var date=p.ts ? new Date(p.ts).toISOString().replace('T',' ').slice(0,16)+' UTC' :
                 'Earlier mark without verified timestamp';
      info.textContent=date+' · Net '+cash(p.pnl)+' · Buy and hold '+cash(p.hold);
    }
    svg.addEventListener('pointermove',function(e){
      var rect=svg.getBoundingClientRect();
      if(!rect.width)return;
      var fraction=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width));
      var logical=fraction*790;
      show(Math.round((logical-80)/690*(points.length-1)));
    });
    svg.addEventListener('click',function(){show(at);});
    svg.addEventListener('focus',function(){show(at);});
    svg.addEventListener('keydown',function(e){
      if(e.key==='ArrowRight'||e.key==='ArrowLeft'){
        e.preventDefault();
        show(at+(e.key==='ArrowRight'?1:-1));
      }
    });
  });
})();
"""


def shell_page(title: str, body: str, mode: str, heartbeat: str,
               subnav: list[tuple[str, str]] | None = None,
               current: str = "home", alive: bool = True) -> str:
    label = {
        "home": "Home dashboard",
        "performance": "Performance",
        "learning": "Learning & Research",
        "safety": "System safety",
        "settings": "Settings",
        "bots": "My Bots",
        "advanced": "Technical overview",
        "graveyard": "Failed strategies",
    }.get(current, "Trading dashboard")
    links = "".join(
        '<a href="' + escape(href, quote=True) + '"' +
        (' aria-current="page"' if current == key else '') +
        (' data-set-view="simple"' if key == "home" else '') +
        '><span class="nav-icon" aria-hidden="true">' + symbol + '</span><span>' +
        escape(name) + '</span></a>'
        for key, name, href, symbol in NAV)
    sub = ('<nav class="tabs section-jump" aria-label="Page sections">' +
           "".join('<a href="' + escape(href, quote=True) + '">' +
                   escape(name) + '</a>' for href, name in subnav) + '</nav>') if subnav else ''
    mode_badge = ('<span class="mode live">REAL MONEY</span>' if mode == "LIVE"
                  else '<span class="mode paper">FAKE MONEY ONLY</span>')
    sidebar_notice = (
        '<strong>Real-money mode</strong>Live trading can lose actual funds. '
        'Review the Safety and Settings sections.'
        if mode == "LIVE" else
        '<strong>Research mode</strong>Your experimental bots use fake money. '
        'Performance is simulated, not a guarantee of earnings.')
    switch = ('<div class="view-switch" role="group" aria-label="Dashboard detail level">'
              '<a href="/" data-set-view="simple" ' +
              ('aria-current="true"' if current == "home" else 'aria-current="false"') +
              '>Simple</a><a href="/advanced" data-set-view="advanced" ' +
              ('aria-current="true"' if current == "advanced" else 'aria-current="false"') +
              '>Advanced</a></div>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="theme-color" content="#111e36">'
        '<title>' + escape(title) + ' · Trading Bots</title>'
        '<style>__BASE_CSS__' + NEXT_CSS + '</style>'
        '</head><body><div class="app-shell">'
        '<aside class="side" aria-label="Primary navigation">'
        '<a class="side-brand" href="/" data-set-view="simple">'
        '<span class="brand-mark" aria-hidden="true">↗</span><span>TradePilot'
        '<span class="brand-note" style="display:block">Bot Control Center</span></span></a>'
        '<div><div class="side-group-label">Workspace</div>'
        '<nav class="primary-nav" aria-label="Main">' + links + '</nav></div>'
        '<div class="side-foot">' + sidebar_notice +
        '<div style="margin-top:12px"><a href="/graveyard">Failed strategies</a> · '
        '<a href="/api/status">System data</a></div></div></aside>'
        '<div class="app-area"><header class="top"><div class="bar">'
        '<div class="top-copy"><span class="top-eyebrow">Trading overview</span>'
        '<div class="top-title">' + escape(label) + '</div></div>'
        + mode_badge + switch
        + '<button type="button" data-toggle-theme aria-label="Change color theme">Dark theme</button>'
        + '<span class="hb">' + escape(heartbeat) + (' · online' if alive else ' · delayed') +
        '</span></div>' + sub + '</header>'
        '<main id="content">' + body + '</main>'
        '<footer style="text-align:center;font-size:11px;color:var(--ink-3);padding:0 12px 26px">'
        'Research results are simulated. No performance is guaranteed.</footer></div></div>'
        '<script>__BASE_JS__' + NEXT_JS + '</script></body></html>'
    )
