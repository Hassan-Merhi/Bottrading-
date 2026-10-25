"""SQLite connection and schema shared by all modules."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,              -- candle open time, ms since epoch (UTC)
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (pair, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    config_json TEXT NOT NULL,
    data_label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    best REAL NOT NULL,
    median REAL NOT NULL,
    worst REAL NOT NULL,
    deaths INTEGER NOT NULL,
    best_genome_json TEXT NOT NULL,
    failure_summary TEXT NOT NULL,
    PRIMARY KEY (run_id, generation)
);

CREATE TABLE IF NOT EXISTS graveyard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    genome_json TEXT NOT NULL,
    cause TEXT NOT NULL,              -- which death rule fired
    diagnosis TEXT NOT NULL,          -- readable reason, used for similarity penalties
    regime TEXT NOT NULL,             -- market conditions in the bot's worst window
    volatility REAL NOT NULL,
    fitness REAL NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    genome_hash TEXT PRIMARY KEY,     -- one test-set evaluation per genome, ever
    run_id TEXT NOT NULL,
    genome_json TEXT NOT NULL,
    validation_fitness REAL NOT NULL,
    validation_json TEXT NOT NULL,
    test_json TEXT NOT NULL,
    passed_test INTEGER NOT NULL,
    stage TEXT NOT NULL,              -- test_failed | awaiting_paper
    created_at TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
