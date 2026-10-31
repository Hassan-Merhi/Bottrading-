"""Autonomous, bounded public market research. READ-ONLY; no order or code APIs.

Google News RSS works without credentials. Optional GOOGLE_SEARCH_API_KEY and
GOOGLE_SEARCH_CX enable Google's Programmable Search JSON API. External text is
untrusted data, never executable instructions, order signals or policy changes.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

QUERIES = (
    "Kraken spot trading maker taker fee execution crypto",
    "cryptocurrency trend pullback volatility strategy backtesting slippage",
    "bitcoin ethereum solana market liquidity regime crypto risk",
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_sources (
 digest TEXT PRIMARY KEY,
 observed_ms INTEGER NOT NULL,
 query TEXT NOT NULL,
 source TEXT NOT NULL,
 title TEXT NOT NULL,
 url TEXT NOT NULL,
 snippet TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_sources_time ON research_sources(observed_ms);
CREATE TABLE IF NOT EXISTS research_reviews (
 reviewed_ms INTEGER PRIMARY KEY,
 reviewed_run TEXT,
 notes TEXT NOT NULL,
 articles_added INTEGER NOT NULL
);
"""


def _fetch(url: str, timeout: int = 9) -> bytes:
    # Endpoints are constant Google-controlled hosts, not taken from search results.
    req = urllib.request.Request(url, headers={"User-Agent": "BotResearch/1.0 (read-only)"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"research HTTP {response.status}")
        return response.read(180_000)


def _clean(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", value or ""))).strip()[:limit]


def _google_news(query: str) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(_fetch(url))
    entries = []
    for item in root.findall("./channel/item")[:12]:
        title = _clean(item.findtext("title") or "", 220)
        link = (item.findtext("link") or "").strip()[:1000]
        if not title or not link.startswith("https://news.google.com/"):
            continue
        source = item.findtext("source") or "Google News"
        entries.append({"query": query, "title": title, "url": link,
                        "source": _clean(source, 100), "snippet": ""})
    return entries


def _google_custom(query: str, env: dict) -> list[dict]:
    key, cx = env.get("GOOGLE_SEARCH_API_KEY"), env.get("GOOGLE_SEARCH_CX")
    if not (key and cx):
        return []
    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(
        {"key": key, "cx": cx, "q": query, "num": 7, "safe": "active"})
    payload = json.loads(_fetch(url))
    items = []
    for obj in payload.get("items", [])[:7]:
        link = str(obj.get("link") or "")
        parsed = urllib.parse.urlsplit(link)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        items.append({"query": query, "title": _clean(obj.get("title", ""), 220),
                      "url": link[:1000], "source": parsed.hostname[:100],
                      "snippet": _clean(obj.get("snippet", ""), 400)})
    return items


def _feedback(conn: sqlite3.Connection) -> tuple[str | None, list[str]]:
    row = conn.execute("SELECT run_id FROM runs WHERE finished_at IS NOT NULL "
                       "ORDER BY finished_at DESC LIMIT 1").fetchone()
    if row is None:
        return None, ["No completed evolution run; collect real market observations first."]
    run = row[0]
    deaths = dict(conn.execute("SELECT cause, COUNT(*) FROM graveyard "
                               "WHERE run_id=? GROUP BY cause", (run,)).fetchall())
    notes = []
    if sum(v for k, v in deaths.items() if "fees" in k) > 0:
        notes.append("Fee-heavy strategies are failing. Keep full spread/slippage/taker fees "
                     "and evaluate lower-turnover setups rather than reducing modeled costs.")
    if sum(v for k, v in deaths.items() if "few trades" in k) > 0:
        notes.append("Sparse signals are common. Compare one-hour pullback/trend setups "
                     "against independent validation without weakening test trade minimums.")
    if sum(v for k, v in deaths.items() if "lost money" in k) > 0:
        notes.append("Some strategies lose after fees. Preserve multi-regime validation "
                     "and reject negative after-cost returns.")
    lab_exists = conn.execute("SELECT 1 FROM sqlite_master "
                              "WHERE name='lab_accounts' AND type='table'").fetchone()
    if lab_exists:
        x = conn.execute("SELECT COUNT(*), COALESCE(SUM(closed_trades),0), "
                         "COALESCE(SUM(last_equity-cash),0) FROM lab_accounts").fetchone()
        notes.append(f"Isolated experimental paper league: {x[0]} accounts, "
                     f"{x[1]} closed simulated trades. Do not promote from this league.")
    return run, notes or ["No dominant failure pattern; keep gathering out-of-sample evidence."]


def run_research(conn: sqlite3.Connection, cfg: dict, env: dict | None = None,
                 now_ms: int | None = None) -> dict:
    rcfg = cfg.get("research") or {}
    if not rcfg.get("enabled", False):
        return {"enabled": False}
    env = env if env is not None else dict(os.environ)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    conn.executescript(SCHEMA)
    # Repeated starts must not make excessive calls to public search endpoints.
    last = conn.execute("SELECT MAX(reviewed_ms) FROM research_reviews").fetchone()[0]
    if last is not None and now_ms - last < 20 * 3_600_000:
        return snapshot(conn, cfg)
    added, errors = 0, []
    for query in QUERIES:
        for provider in (_google_news,):
            try:
                items = provider(query)
            except Exception as e:
                errors.append(f"Google News {query[:24]}: {str(e)[:150]}")
                items = []
            for item in items:
                digest = hashlib.sha256(item["url"].encode("utf-8")).hexdigest()[:32]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO research_sources"
                    "(digest,observed_ms,query,source,title,url,snippet) VALUES(?,?,?,?,?,?,?)",
                    (digest, now_ms, item["query"], item["source"], item["title"],
                     item["url"], item["snippet"]))
                added += max(0, cur.rowcount)
        if env.get("GOOGLE_SEARCH_API_KEY") and env.get("GOOGLE_SEARCH_CX"):
            try:
                items = _google_custom(query, env)
            except Exception as e:
                errors.append(f"Google search {query[:24]}: {str(e)[:150]}")
                items = []
            for item in items:
                digest = hashlib.sha256(item["url"].encode("utf-8")).hexdigest()[:32]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO research_sources"
                    "(digest,observed_ms,query,source,title,url,snippet) VALUES(?,?,?,?,?,?,?)",
                    (digest, now_ms, item["query"], item["source"], item["title"],
                     item["url"], item["snippet"]))
                added += max(0, cur.rowcount)
    run, notes = _feedback(conn)
    if errors:
        notes.append(f"{len(errors)} source searches failed; no fabricated research was recorded.")
    conn.execute("INSERT OR REPLACE INTO research_reviews VALUES(?,?,?,?)",
                 (now_ms, run, json.dumps(notes), added))
    conn.execute("DELETE FROM research_sources WHERE observed_ms < ?",
                 (now_ms - 45 * 86_400_000,))
    conn.commit()
    return snapshot(conn, cfg)


def snapshot(conn: sqlite3.Connection, cfg: dict) -> dict:
    rcfg = cfg.get("research") or {}
    if not rcfg.get("enabled", False):
        return {"enabled": False, "can_trade": False}
    present = conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_reviews'").fetchone()
    if not present:
        return {"enabled": True, "can_trade": False, "sources": [], "notes": []}
    row = conn.execute("SELECT * FROM research_reviews ORDER BY reviewed_ms DESC LIMIT 1").fetchone()
    articles = [dict(r) for r in conn.execute(
        "SELECT observed_ms, query, source, title, url FROM research_sources "
        "ORDER BY observed_ms DESC, source LIMIT 12")]
    return {"enabled": True, "can_trade": False, "google_news_enabled": True,
            "programmable_search_configured": bool(os.environ.get("GOOGLE_SEARCH_API_KEY")
                                                    and os.environ.get("GOOGLE_SEARCH_CX")),
            "last_reviewed_ms": row["reviewed_ms"] if row else None,
            "last_run": row["reviewed_run"] if row else None,
            "articles_added": row["articles_added"] if row else 0,
            "notes": json.loads(row["notes"]) if row else [],
            "sources": articles}
