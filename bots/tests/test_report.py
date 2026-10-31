import pytest
from fastapi.testclient import TestClient

from bots.db import connect
from bots.report.daily import daily_report_text
from bots.report.dashboard import create_app, progress_chart
from bots.report.telegram import TelegramAlerter
from bots.risk.state import LAST_MODE, RiskState, now_ms
from bots.tests.riskkit import add_bot


class FakeSession:
    def __init__(self, updates=None):
        self.posts = []
        self.updates = updates or []

    def post(self, url, json=None, timeout=None, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        if files is not None:
            self.posts.append((method, {"data": data, "filename": files["document"][0],
                                        "size": len(files["document"][1].read())}))
        else:
            self.posts.append((method, json))

        class R:
            def raise_for_status(self):
                pass

            def json(inner):
                return {"ok": True, "result": self.updates if method == "getUpdates" else {}}
        return R()


def test_telegram_alert_buttons_and_throttle():
    s = FakeSession()
    t = [0.0]
    tg = TelegramAlerter("tok", "42", throttle_minutes=60, session=s, clock=lambda: t[0])
    tg.alert("Kill switch", kind="critical")
    tg.alert("Kill switch", kind="critical")  # throttled
    t[0] += 3601
    tg.alert("Kill switch", kind="critical")
    tg.alert("Ready", kind="graduation", buttons=[("Approve", "approve:b1")])
    sends = [p for m, p in s.posts if m == "sendMessage"]
    assert len(sends) == 3
    assert sends[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:b1"


def test_telegram_buttons_only_from_owner_chat():
    updates = [
        {"update_id": 1, "callback_query": {"id": "c1", "data": "approve:b1",
                                            "message": {"chat": {"id": 999}}}},
        {"update_id": 2, "callback_query": {"id": "c2", "data": "approve:b2",
                                            "message": {"chat": {"id": 42}}}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/status"}},
    ]
    s = FakeSession(updates)
    tg = TelegramAlerter("tok", "42", session=s)
    decided = []
    tg.poll(lambda bot, ok, who: decided.append((bot, ok, who)) or "done", lambda c: "status!")
    assert decided == [("b2", True, "Telegram")]
    assert any(p.get("text") == "status!" for m, p in s.posts if m == "sendMessage")
    assert tg._offset == 4


def test_progress_chart_has_three_series_and_legend():
    gens = [{"generation": i, "best": i * 0.1, "median": 0.0, "worst": -1.0} for i in range(5)]
    svg = progress_chart(gens)
    assert svg.count("<polyline") == 3 and "legend" in svg and "data-points" in svg
    assert "No evolution run" in progress_chart([])


@pytest.fixture
def dash(tmp_path, cfg):
    db = tmp_path / "d.db"
    conn = connect(db)
    add_bot(conn, "b1", "ready_to_graduate")
    RiskState(conn).set(LAST_MODE, "PAPER")
    conn.close()
    app = create_app(db, cfg, env={"DASHBOARD_TOKEN": "s3cret"})
    return TestClient(app), db


def test_dashboard_pages(dash):
    client, _ = dash
    r = client.get("/advanced")
    assert r.status_code == 200
    assert "PAPER" in r.text
    assert "Know what your bots are doing" in client.get("/").text
    assert "never risk-free" in r.text and "real money can be lost" in r.text
    assert "Ready to graduate" in r.text
    assert client.get("/graveyard").status_code == 200
    assert client.get("/api/status").json()["mode"] == "PAPER"


def test_dashboard_approval_needs_token(dash):
    client, db = dash
    assert client.post("/bots/b1/approve", data={"token": "nope"}).status_code == 403
    assert client.post("/bots/b1/approve", data={"token": "s3cret"},
                       follow_redirects=False).status_code == 303
    conn = connect(db)
    assert conn.execute("SELECT status FROM deployed_bots").fetchone()[0] == "approved"


def test_dashboard_without_token_disables_approvals(tmp_path, cfg):
    db = tmp_path / "d.db"
    conn = connect(db)
    add_bot(conn, "b1", "ready_to_graduate")
    conn.close()
    client = TestClient(create_app(db, cfg, env={}))
    assert client.post("/bots/b1/approve", data={"token": ""}).status_code == 403
    assert "Approve from Telegram" in client.get("/advanced").text


def test_daily_report_text(conn):
    add_bot(conn, "b1", "paper")
    RiskState(conn).log("order_refused", "b1 refused: test")
    text = daily_report_text(conn, now_ms())
    assert "Daily report" in text and "Paper" in text and "refused" in text


def test_dashboard_shows_mood_and_briefing(dash):
    client, db = dash
    conn = connect(db)
    conn.execute("INSERT INTO fear_greed (ts, value) VALUES (1760000000000, 23)")
    conn.execute("INSERT INTO analyst_digests (ts, items, used_ai, text) VALUES "
                 "(1760000000000, 5, 1, 'Mood: <b>fearful</b>')")
    conn.commit()
    page = client.get("/advanced").text
    assert "Market mood and AI analyst" in page and "<b>23</b>/100" in page
    assert "Mood: &lt;b&gt;fearful&lt;/b&gt;" in page  # untrusted text is escaped
    assert "never read it" in page


def test_dashboard_draws_equity_history(dash):
    client, db = dash
    conn = connect(db)
    add_bot(conn, "b2", "paper")
    conn.execute("INSERT INTO accounts (bot_id, mode, active, epoch, allocation, cash, peak_equity, "
                 "last_equity, day_start_equity, started_at) VALUES ('b2','paper',1,1,20,21,21,21,21,0)")
    acc = conn.execute("SELECT account_id FROM accounts WHERE bot_id='b2'").fetchone()[0]
    conn.executemany("INSERT INTO equity_history (account_id, day, ts, equity) VALUES (?,?,?,?)",
                     [(acc, d, d * 86_400_000, 20 + d * 0.1) for d in range(5)])
    conn.commit()
    page = client.get("/advanced").text
    assert "Equity, last" in page and "<polyline" in page


def test_telegram_sends_files_and_passes_full_commands(tmp_path):
    s = FakeSession([{"update_id": 9, "message": {"chat": {"id": 42}, "text": "/kill confirm"}}])
    tg = TelegramAlerter("tok", "42", session=s)
    f = tmp_path / "report.md"
    f.write_text("hello")
    assert tg.send_file(f, caption="the report") is True
    sent = [p for m, p in s.posts if m == "sendDocument"][0]
    assert sent["filename"] == "report.md" and sent["size"] == 5
    assert sent["data"]["caption"] == "the report" and sent["data"]["chat_id"] == "42"
    assert tg.send_file(tmp_path / "missing.md") is False  # never raises
    seen = []
    tg.poll(lambda *a: "x", lambda text: seen.append(text) or "ok")
    assert seen == ["/kill confirm"]


def test_forward_paper_evidence_endpoint_and_status(dash):
    client, db = dash
    conn = connect(db)
    add_bot(conn, "p5", "paper")
    conn.execute("INSERT INTO accounts (bot_id, mode, active, epoch, allocation, cash, "
                 "peak_equity, last_equity, day_start_equity, started_at, hold_start_price) "
                 "VALUES ('p5','paper',1,1,20,20,20,20,20,1793000000000,50000)")
    conn.commit()
    a = conn.execute("SELECT * FROM accounts WHERE bot_id='p5'").fetchone()
    from bots.paper.forward import observe
    cfg = __import__("bots.config", fromlist=["load_config"]).load_config()
    assert observe(conn, a, 50_000, a["started_at"], cfg)
    conn.close()
    detail = client.get("/api/paper/p5/history").json()
    assert detail["simulation"] is True and detail["mode"] == "paper"
    assert detail["evidence"]["observed_days"] == 1
    assert len(detail["observations"]) == 1
    assert client.get("/api/paper/missing/history").status_code == 404
    status = client.get("/api/status").json()
    assert any(x["bot_id"] == "p5" for x in status["paper_forward"])
    page = client.get("/advanced").text
    assert "Forward paper verification" in page
    assert "Distinct market days" in page


def test_pilot_readiness_is_read_only_and_unfunded(dash):
    client, db = dash
    response = client.get("/api/pilot/readiness")
    assert response.status_code == 200
    pilot = response.json()
    assert pilot["enabled"] is False
    assert pilot["allocated_live_usd"] == 0
    assert pilot["live_accounts"] == 0
    assert pilot["configuration_blockers"]
    assert pilot["profitability_proven"] is False
    assert client.get("/api/status").json()["pilot"]["enabled"] is False
    page = client.get("/advanced").text
    assert "Controlled live pilot" in page and "OFF — PAPER ONLY" in page


def test_scaling_readiness_reports_zero_auto_funding(dash):
    client, db = dash
    response = client.get("/api/scaling/readiness")
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["status"] == "blocked"
    assert data["safe_to_auto_scale"] is False
    assert data["approved_scale_cap_usd"] == 0
    assert client.get("/api/status").json()["scaling"]["status"] == "blocked"
    html = client.get("/advanced").text
    assert "Live performance &amp; capital scaling" in html
    assert "OFF — NO SCALING" in html
