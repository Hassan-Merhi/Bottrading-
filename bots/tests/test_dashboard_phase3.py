"""Phase 3 UI: no fake intelligence, no fabricated returns and safe sources."""
from __future__ import annotations

import json
from fastapi.testclient import TestClient

from bots.db import connect
from bots.report.dashboard import create_app
from bots.report.learning_data import category_for, failure_list, report, source_list
from bots.report.learning_view import learning_page, source_page, failure_page
from bots.risk.state import LAST_MODE, RiskState
from bots.strategy.genome import Genome
from bots.tests.riskkit import base_genome


def seed_learning(conn):
    g = base_genome()
    genome = g.to_json()
    conn.execute(
        "INSERT INTO runs(run_id,started_at,finished_at,config_json,data_label) "
        "VALUES('old','2025-01-01','2025-01-01','{}','old'),"
        "('r-new','2026-10-28','2026-10-28','{}','2026 dataset')")
    for gen in range(1, 4):
        conn.execute(
            "INSERT INTO generations(run_id,generation,best,median,worst,deaths,"
            "best_genome_json,failure_summary) VALUES(?,?,?,?,?,?,?,?)",
            ("r-new", gen, float(gen), 0.5, -1.2, 2, genome, "many fees"))
    conn.execute(
        "INSERT INTO population(run_id,rank,genome_json,fitness,summary_json) "
        "VALUES(?,?,?,?,?)",
        ("r-new", 0, genome, 1.2, json.dumps(
            {"return": 0.07, "hold_return": 0.04, "max_drawdown": 0.05, "trades": 21})))
    conn.execute(
        "INSERT INTO candidates(genome_hash,run_id,genome_json,validation_fitness,"
        "validation_json,test_json,passed_test,stage,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("h1", "r-new", genome, 0.2, "{}", "{}", 1, "awaiting_paper", "2026-10-29"))
    conn.execute(
        "INSERT INTO candidates(genome_hash,run_id,genome_json,validation_fitness,"
        "validation_json,test_json,passed_test,stage,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("h2", "r-new", genome, -1, "{}", "{}", 0, "test_failed", "2026-10-29"))
    reasons = [
        ("r-new", "fees exceed spread costs", "fees"),
        ("r-new", "too few trades", "no trades"),
        ("old", "lost money after fees", "negative return"),
        ("r-new", "<script>alert(1)</script>", "unknown"),
    ]
    for i, (run, diag, cause) in enumerate(reasons):
        conn.execute(
            "INSERT INTO graveyard(run_id,generation,genome_json,cause,diagnosis,"
            "regime,volatility,fitness,metrics_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run, i+1, genome, cause, diag, "sideways", 0.07, -1, "{}", "2026-10-28"))
    conn.execute(
        "CREATE TABLE research_sources (digest TEXT PRIMARY KEY,observed_ms INTEGER,"
        "query TEXT,source TEXT,title TEXT,url TEXT,snippet TEXT)")
    conn.execute(
        "CREATE TABLE research_reviews (reviewed_ms INTEGER PRIMARY KEY,"
        "reviewed_run TEXT,notes TEXT,articles_added INTEGER)")
    conn.execute(
        "INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)",
        ("a", 1800000000000, "fees trading", "Google News",
         "<script>buy bitcoin</script>", "https://news.google.com/articles/abc", "<b>Risk</b>"))
    conn.execute(
        "INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)",
        ("b", 1800000000001, "fees crypto", "Evil Source",
         "Untrusted headline", "javascript:alert(1)", "Pay me"))
    conn.execute(
        "INSERT INTO research_reviews VALUES(?,?,?,?)",
        (1800000000001, "r-new", '["Costs were high; do not reduce them"]', 2))
    conn.commit()


def test_empty_learning_is_candid(cfg, conn):
    d = report(conn, cfg)
    assert d["latest_run"] is None
    assert d["generation_count"] == 0
    assert d["total_failure_records"] == 0
    assert d["candidate_passed_test"] == 0
    html = learning_page(d, source_list(conn))
    assert "No evolution run has finished yet" in html
    assert "No strategy rejections have been recorded" in html
    assert "does not guarantee future profit" in html
    assert "not proof of positive future" in d["explanation"]


def test_actual_history_and_labelled_training_not_profit(cfg, conn):
    seed_learning(conn)
    d = report(conn, cfg)
    assert d["latest_run"]["run_id"] == "r-new"
    assert d["generation_count"] == 3
    assert d["candidate_evaluations"] == 2
    assert d["candidate_passed_test"] == 1
    assert d["candidate_failed_test"] == 1
    assert d["total_failure_records"] == 4
    assert d["latest_run_failure_records"] == 3
    assert d["research"]["stored_sources"] == 2
    assert d["research"]["last_review"]["articles_added"] == 2
    assert d["leaderboard"][0]["rank"] == 1
    html = learning_page(d, source_list(conn))
    assert "Training fitness is an internal comparison score" in html
    assert "not dollars" in html
    assert "not automatically approved" in html.lower()
    assert "Cannot place live orders" not in html  # no implied live execution
    assert "<script>buy bitcoin</script>" not in html
    assert "&lt;script&gt;buy bitcoin&lt;/script&gt;" in html
    assert "https://news.google.com/articles/abc" in html


def test_failure_category_filters_use_original_diagnosis(cfg, conn):
    seed_learning(conn)
    d = report(conn, cfg)
    assert category_for("fees exceed spread costs") == "fees"
    assert category_for("too few trades") == "low_signals"
    assert category_for("huge drawdown") == "risk"
    assert category_for("unknown error") == "other"
    latest = failure_list(conn, scope="latest")
    assert latest["total"] == 3
    fees = failure_list(conn, scope="all", category="fees")
    assert fees["total"] == 2  # one old and one new
    assert all(row["category"] == "fees" for row in fees["items"])
    assert failure_list(conn, scope="latest", category="low_signals")["total"] == 1
    html = failure_page(d, latest)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Costs &amp; fees" in html


def test_source_search_filters_and_unsafe_link_are_handled(cfg, conn):
    seed_learning(conn)
    d = source_list(conn, "fees", limit=20)
    assert d["total"] == 2
    by_publisher = source_list(conn, source="Google News")
    assert by_publisher["total"] == 1
    assert by_publisher["items"][0]["url"].startswith("https://")
    unsafe = source_list(conn, source="Evil Source")
    assert unsafe["items"][0]["url"] == ""
    html = source_page(unsafe)
    assert "unsafe link omitted" in html
    assert "javascript:alert(1)" not in html
    assert source_list(conn, "nonexistent")["total"] == 0


def test_phase3_routes_readonly_and_preserve_old_dashboard(cfg, tmp_path):
    path = tmp_path / "learn.db"
    conn = connect(path)
    RiskState(conn).set(LAST_MODE, "PAPER")
    seed_learning(conn)
    previous_candidates = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    conn.close()
    client = TestClient(create_app(path, cfg, env={"MODE": "PAPER"}))
    assert client.get("/learning").status_code == 200
    assert "Learning &amp; Research" in client.get("/learning").text
    assert 'href="/learning"' in client.get("/").text
    assert client.get("/research/sources").status_code == 200
    assert client.get("/research/sources?search=fees").status_code == 200
    assert client.get("/graveyard?category=fees").status_code == 200
    assert client.get("/graveyard?category=INVALID").status_code == 400
    assert client.get("/api/learning/summary").status_code == 200
    assert client.get("/api/learning/summary").json()["can_trade"] is False
    assert client.get("/api/learning/sources?source=Google%20News").json()["total"] == 1
    assert client.get("/api/learning/failures?scope=latest").json()["total"] == 3
    assert client.get("/api/learning/failures?category=WRONG").status_code == 400
    assert client.get("/advanced").status_code == 200
    assert "How a bot gets to real money" in client.get("/advanced").text
    assert client.get("/api/status").status_code == 200
    assert client.post("/bots/example/approve", data={"token": "bad"}).status_code == 403
    conn = connect(path)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == previous_candidates
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    conn.close()
