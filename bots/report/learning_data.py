"""Read-only learning analytics from historical evolution, research and failures.

Counters are distinct units: generations are optimization iterations; failures
are recorded events; candidate rows are unique genomes evaluated on unseen data.
Never confuse internal backtest metrics with observed trading profit.
"""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlsplit

from bots.research.agent import snapshot as research_snapshot

FAILURE_LABELS = (
    ("fees", "Costs & fees", ("fee", "slippage", "spread", "cost", "turnover")),
    ("low_signals", "Too few opportunities", ("few trade", "sparse", "no trade", "insufficient trade", "low trade", "signal")),
    ("losses", "Unprofitable returns", ("lost money", "loss", "negative", "unprofitable", "return below", "underperform")),
    ("risk", "Excessive risk", ("drawdown", "risk", "volatil", "stop")),
    ("validation", "Failed validation", ("test", "validation", "holdout", "overfit", "robust", "regime")),
)
CATEGORIES = ("all", *(row[0] for row in FAILURE_LABELS), "other")
LABELS = {key: label for key, label, _ in FAILURE_LABELS} | {"other": "Other reasons", "all": "All reasons"}
SCOPE = ("latest", "all")


def category_for(diagnosis: str, cause: str = "") -> str:
    text = (str(diagnosis or "") + " " + str(cause or "")).lower()
    for key, _label, keywords in FAILURE_LABELS:
        if any(k in text for k in keywords):
            return key
    return "other"


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (table,)).fetchone())


def _latest_run(conn):
    return conn.execute(
        "SELECT run_id,started_at,finished_at,data_label FROM runs "
        "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1").fetchone()


def _parse_list(raw) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
        return [str(v)[:500] for v in parsed[:12]] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _safe_metrics(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def report(conn: sqlite3.Connection, cfg: dict) -> dict:
    recent = _latest_run(conn)
    run_id = str(recent["run_id"]) if recent else None
    generations = [dict(r) for r in conn.execute(
        "SELECT generation,best,median,worst,deaths,failure_summary "
        "FROM generations WHERE run_id=? ORDER BY generation",
        (run_id,))] if run_id else []
    # The latest population is NOT a tally of all genomes ever created.
    best = []
    if run_id:
        for row in conn.execute(
                "SELECT rank,genome_json,fitness,summary_json FROM population "
                "WHERE run_id=? ORDER BY rank LIMIT 8", (run_id,)):
            from bots.strategy.genome import Genome
            try:
                g = Genome.from_dict(json.loads(row["genome_json"]))
                description = g.describe()
                pair = g.pair
            except (TypeError, ValueError, KeyError):
                description = "Detailed rules unavailable"
                pair = "Unknown pair"
            metrics = _safe_metrics(row["summary_json"])
            best.append({"rank": int(row["rank"]) + 1, "pair": pair,
                         "description": description, "fitness": float(row["fitness"]),
                         "return": metrics.get("return"),
                         "hold_return": metrics.get("hold_return"),
                         "max_drawdown": metrics.get("max_drawdown"),
                         "trades": metrics.get("trades")})
    candidates = {str(row["stage"]): int(row["n"]) for row in conn.execute(
        "SELECT stage,COUNT(*) n FROM candidates GROUP BY stage")}
    all_candidates = int(sum(candidates.values()))
    passed = int(conn.execute("SELECT COUNT(*) FROM candidates WHERE passed_test=1").fetchone()[0])
    # The pass count records unseen-data evaluation only, not approved live bots.
    deployed = {str(row["status"]): int(row["n"]) for row in conn.execute(
        "SELECT status,COUNT(*) n FROM deployed_bots GROUP BY status")}
    total_failures = int(conn.execute("SELECT COUNT(*) FROM graveyard").fetchone()[0])
    latest_failures = int(conn.execute(
        "SELECT COUNT(*) FROM graveyard WHERE run_id=?", (run_id,)).fetchone()[0]) if run_id else 0
    research = research_snapshot(conn, cfg)
    all_sources = int(conn.execute(
        "SELECT COUNT(*) FROM research_sources").fetchone()[0]) if _has_table(conn, "research_sources") else 0
    last_review = None
    if _has_table(conn, "research_reviews"):
        item = conn.execute(
            "SELECT reviewed_ms,reviewed_run,articles_added,notes FROM research_reviews "
            "ORDER BY reviewed_ms DESC LIMIT 1").fetchone()
        if item:
            last_review = {
                "observed_ms": int(item["reviewed_ms"]),
                "reviewed_run": item["reviewed_run"],
                "articles_added": int(item["articles_added"]),
                "notes": _parse_list(item["notes"])}
    counts = {key: 0 for key in CATEGORIES if key != "all"}
    examples: dict[str, list[str]] = {key: [] for key in counts}
    for row in conn.execute("SELECT cause,diagnosis FROM graveyard"):
        key = category_for(row["diagnosis"], row["cause"])
        counts[key] += 1
        if len(examples[key]) < 3 and row["diagnosis"]:
            d = str(row["diagnosis"])[:220]
            if d not in examples[key]:
                examples[key].append(d)
    return {
        "read_only": True, "can_trade": False,
        "latest_run": dict(recent) if recent else None,
        "generations": generations[-100:],
        "generation_count": len(generations),
        "last_generation_deaths": int(generations[-1]["deaths"]) if generations else None,
        "leaderboard": best, "candidate_evaluations": all_candidates,
        "candidate_passed_test": passed,
        "candidate_failed_test": int(conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE passed_test=0").fetchone()[0]),
        "candidate_stages": candidates, "deployed_states": deployed,
        "total_failure_records": total_failures, "latest_run_failure_records": latest_failures,
        "failure_categories": [{"key": k, "label": LABELS[k], "count": v,
                                "examples": examples[k]} for k, v in counts.items()],
        "research": {"enabled": bool(research.get("enabled", False)),
                     "last_review": last_review, "stored_sources": all_sources,
                     "notes": research.get("notes", []),
                     "google_news_enabled": research.get("google_news_enabled", False),
                     "programmable_search_configured":
                         research.get("programmable_search_configured", False)},
        "explanation": ("Historical optimization, unseen-data testing and news collection "
                        "are not proof of positive future paper or live trading results."),
    }


def source_list(conn: sqlite3.Connection, query: str = "", source: str = "",
                offset: int = 0, limit: int = 24) -> dict:
    offset = max(0, min(int(offset), 10_000))
    limit = max(1, min(int(limit), 50))
    query, source = str(query).strip()[:100], str(source).strip()[:120]
    if not _has_table(conn, "research_sources"):
        return {"items": [], "total": 0, "offset": offset, "limit": limit,
                "search": query, "source": source, "sources": []}
    providers = [str(row[0]) for row in conn.execute(
        "SELECT source FROM research_sources GROUP BY source "
        "ORDER BY COUNT(*) DESC,source LIMIT 30")]
    clauses, args = [], []
    if query:
        clauses.append("(title LIKE ? OR query LIKE ? OR snippet LIKE ?)")
        needle = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses[-1] = "(title LIKE ? ESCAPE '\\' OR query LIKE ? ESCAPE '\\' OR snippet LIKE ? ESCAPE '\\')"
        args.extend([needle, needle, needle])
    if source:
        clauses.append("source=?")
        args.append(source)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = int(conn.execute("SELECT COUNT(*) FROM research_sources" + where, args).fetchone()[0])
    rows = conn.execute(
        "SELECT observed_ms,query,source,title,url,snippet FROM research_sources" +
        where + " ORDER BY observed_ms DESC,title LIMIT ? OFFSET ?",
        (*args, limit, offset))
    items = []
    for item in rows:
        item = dict(item)
        # Never surface javascript:/data:/http:, malformed hostnames or
        # embedded-credential URLs. Bad external data must not crash the UI.
        try:
            u = urlsplit(str(item["url"]))
            if u.scheme != "https" or not u.hostname or u.username or u.password:
                item["url"] = ""
        except ValueError:
            item["url"] = ""
        items.append(item)
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "search": query, "source": source, "sources": providers}


def failure_list(conn: sqlite3.Connection, scope: str = "all",
                 category: str = "all", offset: int = 0, limit: int = 40) -> dict:
    if scope not in SCOPE or category not in CATEGORIES:
        raise ValueError("Unknown failure filter")
    offset = max(0, min(int(offset), 10_000))
    limit = max(1, min(int(limit), 100))
    recent = _latest_run(conn)
    run_id = recent["run_id"] if recent else None
    if scope == "latest" and run_id is None:
        return {"items": [], "total": 0, "scope": scope, "category": category,
                "offset": offset, "limit": limit}
    where = " WHERE run_id=?" if scope == "latest" else ""
    args = (run_id,) if scope == "latest" else ()
    if category == "all":
        rows = conn.execute(
            "SELECT id,run_id,generation,genome_json,cause,diagnosis,regime,volatility,"
            "created_at FROM graveyard" + where +
            " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        count = int(conn.execute("SELECT COUNT(*) FROM graveyard" + where, args).fetchone()[0])
        records = [dict(row) for row in rows]
    else:
        # Classification comes from original diagnoses and can change with
        # labels. Query actual stored records before paging to avoid wrong
        # category totals or skipped matches.
        rows = conn.execute(
            "SELECT id,run_id,generation,genome_json,cause,diagnosis,regime,volatility,"
            "created_at FROM graveyard" + where + " ORDER BY id DESC", args)
        matches = (dict(r) for r in rows if category_for(r["diagnosis"], r["cause"]) == category)
        records = []
        count = 0
        for r in matches:
            if offset <= count < offset + limit:
                records.append(r)
            count += 1
    from bots.strategy.genome import Genome
    for rec in records:
        try:
            rec["strategy"] = Genome.from_dict(json.loads(rec.pop("genome_json"))).describe()
        except (ValueError, TypeError, KeyError):
            rec.pop("genome_json", None)
            rec["strategy"] = "Strategy details unavailable"
        rec["category"] = category_for(rec["diagnosis"], rec["cause"])
    return {"items": records, "total": count, "scope": scope, "category": category,
            "offset": offset, "limit": limit}
