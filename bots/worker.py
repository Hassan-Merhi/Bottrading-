"""The always-on worker: trading loop, nightly evolution, reports, backups, heartbeat.

Fail-safes (section 6.7):
* A loop that hits API errors or bad data halts new entries (stops stay on the
  exchange) until enough healthy loops in a row.
* If the previous run did not shut down cleanly, the system comes back in
  PAPER and waits for the owner (`python -m bots.main confirm-live`).
* A watchdog thread alerts if the trading loop has not reported in
  `heartbeat_hours`. For a machine that dies outright, also run
  `python -m bots.main watchdog` from an external scheduler.
"""
from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from bots.config import PROJECT_DIR, resolve_path
from bots.data.fetch import make_exchange, needs_backfill
from bots.analyst.digest import for_telegram, latest_digest
from bots.data.mood import update_fear_greed
from bots.db import connect
from bots.live.exchange import (WithdrawalPermissionError, check_can_query, check_cannot_withdraw,
                                has_api_keys, make_live_exchange)
from bots.paper.exchange import PaperExchange
from bots.paper.trader import Trader, decide_graduation
from bots.report.daily import daily_report_text
from bots.report.telegram import make_alerter
from bots.risk.guard import ReadOnlyExchange, RiskGuard
from bots.risk.state import CRASH_LOCK, HEARTBEAT, LAST_MODE, RUNNING, RiskState, now_ms


def backup_database(db_path: Path, backups_dir: Path, keep: int) -> Path:
    """Consistent online copy via SQLite's backup API; keeps the newest `keep` files."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    out = backups_dir / f"bots-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(out))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for old in sorted(backups_dir.glob("bots-*.db"))[:-keep]:
        old.unlink()
    return out


class Worker:
    def __init__(self, cfg: dict, env: dict, db_path: Path | None = None, alerter=None,
                 market_data=None, live_exchange_factory=make_live_exchange):
        self.cfg = cfg
        self.env = env
        self.db_path = db_path or resolve_path(cfg["database_path"])
        self.alerter = alerter or make_alerter(cfg, env)
        self._market_data = market_data
        self._live_factory = live_exchange_factory
        self._stop = threading.Event()
        self._evolution: subprocess.Popen | None = None
        self._backfill: subprocess.Popen | None = None
        self._backfill_at = 0.0

    # -- startup / shutdown ------------------------------------------------

    def startup(self) -> None:
        self.conn = connect(self.db_path)
        self.state = RiskState(self.conn)
        if self.state.is_set(RUNNING):
            self.state.set(CRASH_LOCK, "previous run stopped without a clean shutdown")
            self.state.log("crash_restart", "Restarted after an unclean stop; staying in PAPER "
                                            "until the owner confirms")
            self.alerter.alert("System restarted after a crash. It stays in PAPER until you run "
                               "`python -m bots.main confirm-live`.", kind="critical")
        self.state.set(RUNNING, now_ms())

        live = None
        if has_api_keys(self.env):
            live = self._live_factory(self.env)
            try:
                check_cannot_withdraw(live)
                check_can_query(live)
            except WithdrawalPermissionError as e:
                self.alerter.alert(f"REFUSING TO RUN: {e}", kind="critical")
                self.state.log("withdraw_permission", str(e))
                self.state.clear(RUNNING)
                raise SystemExit(f"Refusing to run: {e}")
            except Exception as e:  # noqa: BLE001 - cannot verify the key: run paper only
                self.alerter.alert(f"Kraken key check failed ({e}); running PAPER only.",
                                   kind="critical")
                live = None

        bt = self.cfg["backtest"]
        self.paper = PaperExchange(bt["taker_fee_pct"] / 100, bt["slippage_pct"] / 100)
        self.guard = RiskGuard(self.cfg, self.conn, self.paper, live, self.alerter, self.env)
        md = self._market_data or ReadOnlyExchange(make_exchange(self.cfg["data"]["exchange"]))
        self.trader = Trader(self.cfg, self.conn, self.guard, md, self.paper, self.alerter)
        self.trader.restore_paper_stops()
        decision = self._record_mode()
        self.alerter.alert(f"Worker started in {decision.mode} mode."
                           + ("" if decision.mode == "LIVE" else
                              " Reasons: " + "; ".join(decision.reasons)), kind="info")

    def shutdown(self) -> None:
        self.state.clear(RUNNING)
        self.conn.close()

    def _record_mode(self):
        decision = self.guard.mode()
        previous = self.state.get(LAST_MODE)
        self.state.set(LAST_MODE, decision.mode)
        self.state.set("mode_reasons", "; ".join(decision.reasons))
        if previous and previous != decision.mode:
            self.alerter.alert(f"Mode changed {previous} → {decision.mode}.", kind="risk")
        return decision

    # -- loop --------------------------------------------------------------

    def loop_once(self) -> None:
        self.trader.deploy_candidates()
        try:
            report = self.trader.tick()
            if report.errors:
                print("tick errors:", "; ".join(report.errors))
        except Exception as e:  # noqa: BLE001 - a broken loop must not open trades
            self.guard.halt_entries(f"trading loop error: {e}")
        self._record_mode()
        poll = getattr(self.alerter, "poll", None)
        if poll:
            poll(lambda bot_id, ok, who: decide_graduation(self.conn, bot_id, ok, who),
                 self._command)
        self._scheduled()

    def _command(self, cmd: str) -> str:
        if cmd in ("/status", "/report"):
            return daily_report_text(self.conn, now_ms())
        if cmd == "/digest":
            row = latest_digest(self.conn)
            return for_telegram(row["text"]) if row else "No briefing yet today."
        return "Commands: /status, /digest"

    def _due(self, name: str, hour: int) -> bool:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour < hour or self.state.get(f"last_{name}") == today:
            return False
        self.state.set(f"last_{name}", today)
        return True

    def _scheduled(self) -> None:
        w = self.cfg["worker"]
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
        if self.state.get("last_mood_update") != hour:
            self.state.set("last_mood_update", hour)
            try:
                update_fear_greed(self.conn)
            except Exception as e:  # noqa: BLE001 - stale mood blocks mood-filtered entries
                print(f"Fear & Greed update failed: {e}")
        acfg = self.cfg.get("analyst") or {}
        if acfg.get("enabled") and self._due("digest", acfg["hour_utc"]):
            self._spawn("digest")  # sends its own Telegram message
        if self._due("report", w["daily_report_hour_utc"]):
            self.alerter.alert(daily_report_text(self.conn, now_ms()), kind="report")
        if self._due("backup", w["backup_hour_utc"]):
            try:
                backup_database(self.db_path, resolve_path(w["backups_dir"]), w["backups_keep"])
            except Exception as e:  # noqa: BLE001
                self.alerter.alert(f"Database backup failed: {e}", kind="risk")
        if needs_backfill(self.conn, self.cfg) and (
                self._backfill is None or
                (self._backfill.poll() is not None and time.time() - self._backfill_at > 3600)):
            # History backfill takes hours and resumes where it stopped; retried hourly if it fails.
            self._backfill = self._spawn("fetch")
            self._backfill_at = time.time()
        backfilling = self._backfill is not None and self._backfill.poll() is None
        if not backfilling and (self._evolution is None or self._evolution.poll() is not None) \
                and self._due("evolution", w["evolution_hour_utc"]):
            self._evolution = self._spawn("evolve")

    def _spawn(self, command: str) -> subprocess.Popen:
        """Long jobs run in their own process so trading keeps its rhythm."""
        return subprocess.Popen([sys.executable, "-m", "bots.main", command], cwd=PROJECT_DIR)

    def _watchdog(self) -> None:
        limit_ms = self.cfg["risk"]["heartbeat_hours"] * 3_600_000
        conn = connect(self.db_path)
        state = RiskState(conn)
        while not self._stop.wait(300):
            hb = state.get(HEARTBEAT)
            if hb and now_ms() - int(hb) > limit_ms:
                self.alerter.alert(f"System has not reported in "
                                   f"{(now_ms() - int(hb)) / 3_600_000:.1f} hours.",
                                   kind="critical")
        conn.close()

    def run_forever(self) -> None:
        self.startup()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._stop.set())
        threading.Thread(target=self._watchdog, daemon=True).start()
        try:
            while not self._stop.is_set():
                started = time.time()
                self.loop_once()
                self._stop.wait(max(1.0, self.cfg["worker"]["poll_seconds"] -
                                    (time.time() - started)))
        finally:
            self.shutdown()
