"""Candle storage in SQLite."""
from __future__ import annotations

import sqlite3

import pandas as pd

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def upsert_candles(conn: sqlite3.Connection, pair: str, timeframe: str, df: pd.DataFrame,
                   commit: bool = True) -> int:
    """Insert or replace candles. `df` needs the COLUMNS above (ts in ms)."""
    if df.empty:
        return 0
    rows = [
        (pair, timeframe, int(r.ts), float(r.open), float(r.high), float(r.low),
         float(r.close), float(r.volume))
        for r in df[COLUMNS].itertuples(index=False)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO candles (pair, timeframe, ts, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    if commit:
        conn.commit()
    return len(rows)


def load_candles(conn: sqlite3.Connection, pair: str, timeframe: str,
                 start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
    q = "SELECT ts, open, high, low, close, volume FROM candles WHERE pair = ? AND timeframe = ?"
    args: list = [pair, timeframe]
    if start_ms is not None:
        q += " AND ts >= ?"
        args.append(start_ms)
    if end_ms is not None:
        q += " AND ts < ?"
        args.append(end_ms)
    q += " ORDER BY ts"
    return pd.read_sql_query(q, conn, params=args)


def candle_range(conn: sqlite3.Connection, pair: str, timeframe: str) -> tuple[int, int, int] | None:
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE pair = ? AND timeframe = ?",
        (pair, timeframe),
    ).fetchone()
    if row is None or row[2] == 0:
        return None
    return int(row[0]), int(row[1]), int(row[2])


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str, commit: bool = True) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    if commit:
        conn.commit()
