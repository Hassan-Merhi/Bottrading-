"""Phase 1 UX regression: new homepage and persistent access to technical views.

No test creates trades, alters risk controls, or requires network access.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bots.db import connect
from bots.report.dashboard import create_app
from bots.report.design import NAV, NEXT_CSS, shell_page
from bots.report.home import home_dashboard
from bots.risk.state import LAST_MODE, HEARTBEAT, RiskState


def test_six_primary_navigation_destinations():
    assert [x[1] for x in NAV] == [
        "Home", "My Bots", "Performance", "Learning & Research",
        "Safety", "Settings"]
    assert len(set(x[2] for x in NAV)) == 6
    assert "max-width:850px" in NEXT_CSS
    assert "max-width:620px" in NEXT_CSS
    assert 'prefers-reduced-motion' in NEXT_CSS


def test_home_is_readable_when_no_bots_have_traded(cfg, conn):
    now = 1_800_000_000_000
    RiskState(conn).set(LAST_MODE, "PAPER")
    RiskState(conn).set(HEARTBEAT, str(now))
    text = home_dashboard(conn, cfg, now)
    assert "Know what your bots are doing" in text
    assert "Fake-money starting balance" in text
    assert "Experimental net profit / loss" in text
    assert "Trades completed" in text
    assert "Real money at risk" in text
    assert "No experimental bots are running yet" in text
    assert "Is it making money?" in text
    assert "What happens next?" in text
    assert "no trades yet" not in text or "No experimental bots" in text
    assert "<form" not in text  # all trading/approval controls stay in advanced


def test_home_does_not_mislabel_stale_feed_as_healthy(cfg, conn):
    now = 1_800_000_000_000
    RiskState(conn).set(HEARTBEAT, str(now - 5 * 60 * 60 * 1000))
    html = home_dashboard(conn, cfg, now)
    assert "The trading system needs a check" in html
    assert "Market feed needs attention" in html


def test_live_state_is_loud_and_not_disguised_as_paper(cfg, conn):
    now = 1_800_000_000_000
    RiskState(conn).set(LAST_MODE, "LIVE")
    RiskState(conn).set(HEARTBEAT, str(now))
    html = home_dashboard(conn, cfg, now)
    assert "Real-money trading is enabled" in html
    assert "This is not a fake-money simulation" in html


def test_every_shell_uses_same_nav_and_accessible_toggle():
    html = shell_page("Page", "Content", "PAPER", "heartbeat",
                      current="performance", alive=True)
    assert 'aria-label="Main"' in html
    assert 'data-set-view="simple"' in html
    assert 'data-set-view="advanced"' in html
    assert 'href="/research/forward"' in html
    assert 'href="/advanced#status"' in html
    assert 'href="/advanced#limits"' in html
    assert "__BASE_CSS__" in html
    assert "__BASE_JS__" in html


def test_existing_pages_are_preserved_behind_simple_home(tmp_path, cfg):
    path = tmp_path / "dashboard.db"
    conn = connect(path)
    RiskState(conn).set(LAST_MODE, "PAPER")
    conn.close()
    client = TestClient(create_app(path, cfg, env={"MODE": "PAPER"}))
    home = client.get("/")
    assert home.status_code == 200
    assert "Know what your bots are doing" in home.text
    assert "Your research bots" in home.text
    assert 'href="/advanced"' in home.text
    assert 'aria-label="Main"' in home.text

    advanced = client.get("/advanced")
    assert advanced.status_code == 200
    assert "How a bot gets to real money" in advanced.text
    assert 'id="evolution"' in advanced.text
    assert 'id="limits"' in advanced.text
    assert 'href="/research/forward"' in advanced.text

    history = client.get("/research/forward")
    assert history.status_code == 200
    assert 'class="app-shell"' in history.text

    graveyard = client.get("/graveyard")
    assert graveyard.status_code == 200
    assert 'class="app-shell"' in graveyard.text

    old_api = client.get("/api/status")
    assert old_api.status_code == 200
    assert old_api.json()["mode"] == "PAPER"
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/api/research/forward").status_code == 200


def test_approvals_are_still_protected_on_technical_route(tmp_path, cfg):
    path = tmp_path / "approvals.db"
    conn = connect(path)
    conn.close()
    client = TestClient(create_app(path, cfg, env={"MODE": "PAPER"}))
    assert client.post("/bots/unknown/approve", data={"token": "invalid"}).status_code == 403
