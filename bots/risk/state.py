"""Persistent risk flags and the risk event log."""
from __future__ import annotations

import sqlite3
import time

# Keys in the risk_state table
KILL_SWITCH = "kill_switch"              # set by the total-drawdown kill; only the owner clears it
CRASH_LOCK = "crash_lock"                # set when the previous run did not shut down cleanly
RUNNING = "running"                      # set while a worker runs, cleared on clean shutdown
DAILY_HALT_UNTIL = "daily_halt_until"    # ms; live entries blocked until then
ENTRY_HALT = "entry_halt"                # fail-safe: API errors / bad data block new entries
LIVE_EPOCH = "live_epoch"                # bumps on each owner confirmation
LAST_MODE = "last_mode"
HEARTBEAT = "heartbeat"                  # ms of the last completed trading loop


def now_ms() -> int:
    return int(time.time() * 1000)


class RiskState:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO risk_state (key, value) VALUES (?, ?)",
                          (key, str(value)))
        self.conn.commit()

    def clear(self, key: str) -> None:
        self.conn.execute("DELETE FROM risk_state WHERE key=?", (key,))
        self.conn.commit()

    def is_set(self, key: str) -> bool:
        return self.get(key) is not None

    def log(self, kind: str, detail: str, ts: int | None = None) -> None:
        self.conn.execute("INSERT INTO risk_events (ts, kind, detail) VALUES (?, ?, ?)",
                          (ts or now_ms(), kind, detail))
        self.conn.commit()

    def events_since(self, ts: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM risk_events WHERE ts >= ? ORDER BY ts",
                                 (ts,)).fetchall()
