"""Phase 4: safety and approval UX must NEVER weaken risk or authorization.

Test read-only diagnostics, visible real-money warnings, token-protected
decisions and route compatibility with the original /advanced dashboard.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bots.db import connect
from bots.report.dashboard import create_app
from bots.report.safety_data import snapshot
from bots.report.safety_view import approval_page, safety_page, settings_page
from bots.risk.state import (RiskState, LAST_MODE, HEARTBEAT, KILL_SWITCH,
                             CRASH_LOCK, LIVE_RECON_HALT, OWNER_PAUSE)
from bots.tests.riskkit import add_bot


def test_safety_is_readonly_and_labels_missing_heartbeats(cfg, conn):
    now = 1_800_000_000_000
    RiskState(conn).set(LAST_MODE, "PAPER")
    before = conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0]
    d = snapshot(conn, cfg, now)
    assert d["read_only"] is True
    assert d["mode"] == "PAPER"
    assert d["worker_healthy"] is False
    assert d["market_healthy"] is False
    assert d["live_accounts"] == 0
    assert d["allocated_live_usd"] == 0
    assert d["hard_ceiling_usd"] == 100
    assert {"worker_healthy", "market_healthy", "notices"} <= d.keys()
    page = safety_page(d)
    assert "Trading worker not confirmed healthy" in page
    assert "Market prices need attention" in page
    assert "No emergency reset is available" in page
    assert "<form" not in page
    assert conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] == before


def test_flags_cannot_be_cleared_by_a_page_view(cfg, conn):
    now = 1_800_000_000_000
    RiskState(conn).set(HEARTBEAT, str(now))
    for key in (KILL_SWITCH, CRASH_LOCK, LIVE_RECON_HALT, OWNER_PAUSE):
        RiskState(conn).set(key, "manual verification pending")
    d = snapshot(conn, cfg, now)
    assert d["flags"]["emergency_stop"]
    assert d["flags"]["reconciliation_lock"]
    assert d["flags"]["restart_lock"]
    assert d["flags"]["owner_pause"]
    html = safety_page(d)
    assert "Emergency stop is active" in html
    assert "Exchange reconciliation is locked" in html
    assert "Restart approval is required" in html
    assert "Owner paused new entries" in html
    for key in (KILL_SWITCH, CRASH_LOCK, LIVE_RECON_HALT, OWNER_PAUSE):
        assert RiskState(conn).is_set(key)


def test_live_mode_is_obviously_real_and_settings_are_view_only(cfg, conn):
    now = 1_800_000_000_000
    RiskState(conn).set(LAST_MODE, "LIVE")
    d = snapshot(conn, cfg, now)
    assert d["mode"] == "LIVE"
    assert d["level"] == "danger"
    assert "Real-money mode is active" in safety_page(d)
    settings = settings_page(d)
    assert "Your settings, in plain language" in settings
    assert "Real-money trading is disabled in config" in settings
    assert "0 means no budget is assigned" in settings
    assert "Minimum verified completed trades" in settings
    assert "Hard application ceiling" in settings
    assert '<input' not in settings
    assert '<form' not in settings
    assert d["settings"]["live_enabled"] is False
    assert d["settings"]["pilot_enabled"] is False
    assert d["settings"]["pilot_max_total_usd"] == 0


def test_approval_queue_excludes_unqualified_research(cfg, conn):
    add_bot(conn, "awaiting-person", "ready_to_graduate")
    d = snapshot(conn, cfg, 1_800_000_000_000, approvals_available=True)
    assert len(d["approvals"]) == 1
    assert d["approvals"][0]["bot_id"] == "awaiting-person"
    html = approval_page(d)
    assert "awaiting-person" in html
    assert 'action="/bots/awaiting-person/approve"' in html
    assert 'formaction="/bots/awaiting-person/decline"' in html
    assert 'type="password"' in html
    assert 'data-confirm="approve"' in html
    assert "experiment" in html.lower()
    d2 = snapshot(conn, cfg, 1_800_000_000_000, approvals_available=False)
    disabled = approval_page(d2)
    assert "Actions unavailable on this website" in disabled
    assert '<form' not in disabled


def test_phase4_pages_routed_and_no_tokens_disclosed(cfg, tmp_path):
    path = tmp_path / "safety.db"
    conn = connect(path)
    add_bot(conn, "candidate-123", "ready_to_graduate")
    RiskState(conn).set(LAST_MODE, "PAPER")
    RiskState(conn).set(KILL_SWITCH, "still on")
    conn.close()
    token = "never-leak-the-approval-secret"
    client = TestClient(create_app(path, cfg, env={
        "MODE": "PAPER", "DASHBOARD_TOKEN": token}))
    for url in ("/", "/safety", "/settings", "/approvals", "/advanced",
                "/bots", "/learning", "/research/forward"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert token not in r.text
    assert 'href="/safety"' in client.get("/").text
    assert 'href="/settings"' in client.get("/").text
    assert "candidate-123" in client.get("/approvals").text
    s = client.get("/api/safety/overview")
    assert s.status_code == 200
    assert s.json()["read_only"] is True
    assert s.json()["flags"]["emergency_stop"] is True
    assert token not in s.text
    assert client.get("/healthz").json()["ok"] is True
    # The original server-side token check is unchanged.
    assert client.post("/bots/candidate-123/approve",
                       data={"token": "wrong"}).status_code == 403
    conn = connect(path)
    assert conn.execute("SELECT status FROM deployed_bots WHERE bot_id='candidate-123'").fetchone()[0] == "ready_to_graduate"
    assert RiskState(conn).is_set(KILL_SWITCH)
    conn.close()
    response = client.post("/bots/candidate-123/approve",
                           data={"token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/approvals"
    conn = connect(path)
    assert conn.execute("SELECT status FROM deployed_bots WHERE bot_id='candidate-123'").fetchone()[0] == "approved"
    assert RiskState(conn).is_set(KILL_SWITCH)
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE mode='live'").fetchone()[0] == 0
    conn.close()


def test_approval_without_dashboard_token_stays_telegram_only(cfg, tmp_path):
    path = tmp_path / "telegram.db"
    conn = connect(path)
    add_bot(conn, "telegram-bot", "ready_to_graduate")
    conn.close()
    client = TestClient(create_app(path, cfg, env={"MODE": "PAPER"}))
    page = client.get("/approvals")
    assert page.status_code == 200
    assert "Actions unavailable on this website" in page.text
    assert client.post("/bots/telegram-bot/decline",
                       data={"token": ""}).status_code == 403
    conn = connect(path)
    assert conn.execute("SELECT status FROM deployed_bots WHERE bot_id='telegram-bot'").fetchone()[0] == "ready_to_graduate"
    conn.close()
