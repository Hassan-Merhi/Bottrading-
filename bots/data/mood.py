"""Crypto Fear & Greed Index (alternative.me): daily market mood, 0 = extreme fear, 100 = greed.

History goes back to February 2018, so strategies that use it can be
backtested like any other rule.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import requests

FNG_URL = "https://api.alternative.me/fng/"
DAY_MS = 86_400_000


def fetch_fear_greed(limit: int = 0, session=None) -> list[tuple[int, int]]:
    """(day start ms, value) pairs. limit=0 returns the full history."""
    session = session or requests
    r = session.get(FNG_URL, params={"limit": limit, "format": "json"}, timeout=30)
    r.raise_for_status()
    out = []
    for row in r.json()["data"]:
        value = int(row["value"])
        if 0 <= value <= 100:
            out.append((int(row["timestamp"]) * 1000 // DAY_MS * DAY_MS, value))
    return sorted(out)


def store_fear_greed(conn: sqlite3.Connection, rows: list[tuple[int, int]]) -> int:
    conn.executemany("INSERT OR REPLACE INTO fear_greed (ts, value) VALUES (?, ?)", rows)
    conn.commit()
    return len(rows)


def update_fear_greed(conn: sqlite3.Connection, session=None) -> int:
    have = conn.execute("SELECT COUNT(*) FROM fear_greed").fetchone()[0]
    return store_fear_greed(conn, fetch_fear_greed(0 if have < 30 else 10, session))


def load_fear_greed(conn: sqlite3.Connection) -> tuple[np.ndarray, np.ndarray] | None:
    rows = conn.execute("SELECT ts, value FROM fear_greed ORDER BY ts").fetchall()
    if not rows:
        return None
    arr = np.array([tuple(r) for r in rows], dtype="int64")
    return arr[:, 0], arr[:, 1].astype(float)


def latest_fear_greed(conn: sqlite3.Connection) -> tuple[int, int] | None:
    row = conn.execute("SELECT ts, value FROM fear_greed ORDER BY ts DESC LIMIT 1").fetchone()
    return (int(row[0]), int(row[1])) if row else None
