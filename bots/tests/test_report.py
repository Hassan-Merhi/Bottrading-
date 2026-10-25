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

    def post(self, url, json=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        method = url.rsplit("/", 1)[-1]

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
    r = client.get("/")
    assert r.status_code == 200
    assert "PAPER" in r.text
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
    assert "Approve from Telegram" in client.get("/").text


def test_daily_report_text(conn):
    add_bot(conn, "b1", "paper")
    RiskState(conn).log("order_refused", "b1 refused: test")
    text = daily_report_text(conn, now_ms())
    assert "Daily report" in text and "Paper" in text and "refused" in text
