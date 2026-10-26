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
    stage TEXT NOT NULL,              -- test_failed | awaiting_paper | deployed
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS population (   -- final generation of each run, for the leaderboard
    run_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    genome_json TEXT NOT NULL,
    fitness REAL NOT NULL,
    summary_json TEXT NOT NULL,
    PRIMARY KEY (run_id, rank)
);

CREATE TABLE IF NOT EXISTS deployed_bots (
    bot_id TEXT PRIMARY KEY,
    genome_json TEXT NOT NULL,
    genome_hash TEXT NOT NULL,
    status TEXT NOT NULL,             -- paper | ready_to_graduate | approved | live | retired
    status_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (     -- one per bot per mode; live accounts are kept forever
    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    mode TEXT NOT NULL,               -- paper | live
    active INTEGER NOT NULL,
    epoch INTEGER NOT NULL,           -- live confirmation epoch the account belongs to
    allocation REAL NOT NULL,
    cash REAL NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    entry_price REAL,
    entry_cost REAL,                  -- cash spent on the open position, fee included
    entry_ts INTEGER,
    stop_price REAL,
    target_price REAL,
    stop_order_id TEXT,
    protected REAL NOT NULL DEFAULT 0,
    peak_equity REAL NOT NULL,
    max_drawdown REAL NOT NULL DEFAULT 0,
    last_equity REAL NOT NULL,
    day INTEGER NOT NULL DEFAULT 0,
    day_start_equity REAL NOT NULL,
    trades_today INTEGER NOT NULL DEFAULT 0,
    trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    hold_start_price REAL,
    high_since_entry REAL,            -- highest price seen while in a trade (trailing stop)
    last_candle_ts INTEGER NOT NULL DEFAULT 0,
    started_at INTEGER NOT NULL,      -- ms
    closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS equity_history (   -- one mark per account per UTC day, for charts
    account_id INTEGER NOT NULL,
    day INTEGER NOT NULL,             -- UTC day number
    ts INTEGER NOT NULL,              -- ms of the latest mark that day
    equity REAL NOT NULL,
    PRIMARY KEY (account_id, day)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    bot_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    ts INTEGER NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    pnl REAL,                         -- set on sells
    reason TEXT NOT NULL,
    order_id TEXT
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fear_greed (
    ts INTEGER PRIMARY KEY,           -- day start, ms UTC
    value INTEGER NOT NULL            -- 0 extreme fear .. 100 extreme greed
);

CREATE TABLE IF NOT EXISTS analyst_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    items INTEGER NOT NULL,
    used_ai INTEGER NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # Each thread (trader, dashboard) opens its own connection; WAL lets them coexist.
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release: (table, column, SQL type and default).
MIGRATIONS = [
    ("accounts", "high_since_entry", "REAL"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that an older database is missing. CREATE IF NOT EXISTS does not."""
    for table, column, decl in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
