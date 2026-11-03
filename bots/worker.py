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

import gzip
import shutil
import tempfile

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
from bots.data.health import market_health
from bots.analyst.digest import for_telegram, latest_digest
from bots.data.mood import update_fear_greed
from bots.db import connect
from bots.live.exchange import (WithdrawalPermissionError, check_can_query, check_cannot_withdraw,
                                has_api_keys, make_live_exchange)
from bots.paper.exchange import PaperExchange
from bots.paper.trader import Trader, decide_graduation
from bots.report.daily import daily_report_text, account_rows
from bots.report.telegram import make_alerter
from bots.risk.guard import ReadOnlyExchange, RiskGuard
from bots.risk.ledger import DAY_MS
from bots.risk.state import CRASH_LOCK, HEARTBEAT, LAST_MODE, RUNNING, RiskState, now_ms


# Tables worth keeping off the box. Candles are big and re-downloadable.
EXPORT_SKIP = {"candles"}


def export_state(db_path: Path, out_path: Path) -> Path:
    """A gzip-compressed SQLite copy of everything except price candles."""
    src = sqlite3.connect(str(db_path))
    tmp = out_path.with_suffix(".db")
    if tmp.exists():
        tmp.unlink()
    dst = sqlite3.connect(str(tmp))
    try:
        tables = src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' "
                             "AND name NOT LIKE 'sqlite_%'").fetchall()
        for name, sql in tables:
            if name in EXPORT_SKIP:
                continue
            dst.execute(sql)
            rows = src.execute(f'SELECT * FROM "{name}"').fetchall()
            if rows:
                marks = ",".join("?" * len(rows[0]))
                dst.executemany(f'INSERT INTO "{name}" VALUES ({marks})', rows)
        dst.commit()
    finally:
        dst.close()
        src.close()
    with open(tmp, "rb") as fin, gzip.open(out_path, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    tmp.unlink()
    return out_path


def send_offsite_backup(db_path: Path, alerter) -> str | None:
    """Send the compressed state (no candles) to the owner's Telegram chat."""
    send = getattr(alerter, "send_file", None)
    if send is None:
        return None
    out = Path(tempfile.gettempdir()) / f"bots-state-{datetime.now(timezone.utc):%Y%m%d}.sqlite.gz"
    export_state(db_path, out)
    size_kb = out.stat().st_size / 1024
    ok = send(out, caption=f"Nightly backup, {size_kb:.0f} KB. Bots, accounts, results and "
                           "settings; price history is not included (it re-downloads). "
                           "To restore: gunzip, then copy over bots.db in BOTS_STATE_DIR.")
    out.unlink(missing_ok=True)
    return f"sent {size_kb:.0f} KB" if ok else "send failed"


# Free space kept on the state disk. SQLite needs room for its WAL and journal; when the
# disk filled on 2026-11-01 every startup write failed and the worker crash-looped.
DISK_RESERVE_BYTES = 64 * 1024 * 1024


def _backup_files(backups_dir: Path) -> list[Path]:
    """Backups, oldest first. Old full copies (bots-*.db, candles included) sort before the
    compact ones (bots-*.sqlite.gz) so they are the first to go."""
    if not backups_dir.is_dir():
        return []
    files = list(backups_dir.glob("bots-*.db")) + list(backups_dir.glob("bots-*.sqlite.gz"))
    return sorted(files, key=lambda f: (f.name.endswith(".sqlite.gz"), f.name.split(".")[0]))


def free_disk_space(state_dir: Path, backups_dir: Path,
                    reserve: int = DISK_RESERVE_BYTES) -> list[str]:
    """Delete the oldest backups until `reserve` bytes are free on the state disk.

    Returns what was removed. The live database is never touched.
    """
    removed: list[str] = []
    files = _backup_files(backups_dir)
    while files and shutil.disk_usage(state_dir).free < reserve:
        old = files.pop(0)
        size_mb = old.stat().st_size / 1_048_576
        old.unlink()
        removed.append(f"{old.name} ({size_mb:.0f} MB)")
    return removed


def backup_database(db_path: Path, backups_dir: Path, keep: int,
                    reserve: int = DISK_RESERVE_BYTES) -> Path:
    """Nightly compact backup: every table except price candles, gzip-compressed.

    Candles are most of the database and re-download, so full copies are not kept; 14 of
    them filled the 1 GB disk. Keeps the newest `keep` compact backups, removes old full
    copies, and refuses to run if the disk would be left without `reserve` bytes free.
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    for old in _backup_files(backups_dir)[:-max(1, keep - 1)]:
        old.unlink()
    free_disk_space(db_path.parent, backups_dir, reserve)
    if shutil.disk_usage(backups_dir).free < reserve:
        raise OSError("not enough free disk space for a backup; the database itself is "
                      "close to filling the disk")
    out = backups_dir / f"bots-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite.gz"
    try:
        export_state(db_path, out)
    except Exception:
        out.unlink(missing_ok=True)
        out.with_suffix(".db").unlink(missing_ok=True)
        raise
    for old in _backup_files(backups_dir):
        if old.name.endswith(".db"):
            old.unlink()
    for old in _backup_files(backups_dir)[:-keep]:
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
        self._market_refresh: subprocess.Popen | None = None
        self._market_refresh_at = 0.0
        self._arb_venues: dict | None = None
        self._arb_futures = None
        self._arb_live_clients: dict | None = None

    # -- startup / shutdown ------------------------------------------------

    def startup(self) -> None:
        # Make room before the first write: on a full disk even the crash marker below fails.
        freed = free_disk_space(self.db_path.parent, resolve_path(self.cfg["worker"]["backups_dir"]))
        self.conn = connect(self.db_path)
        self.state = RiskState(self.conn)
        if freed:
            self.state.log("disk_cleanup", "Low disk space at startup; removed old backups: "
                                           + ", ".join(freed))
            self.alerter.alert("The state disk was almost full, so old backups were removed: "
                               + ", ".join(freed), kind="risk")
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
        self.paper = PaperExchange(bt["taker_fee_pct"] / 100, bt["slippage_pct"] / 100,
                                   maker_fee=bt.get("maker_fee_pct", bt["taker_fee_pct"]) / 100,
                                   spread_bps=bt.get("spread_bps", 0),
                                   limit_timeout_s=self.cfg.get("execution", {}).get(
                                       "limit_timeout_s", 120))
        self.guard = RiskGuard(self.cfg, self.conn, self.paper, live, self.alerter, self.env)
        if self.guard.ledger.active("live") or (
                live is not None and self.cfg.get("live_enabled") is True):
            if live is None:
                from bots.risk.state import LIVE_RECON_HALT
                self.state.set(LIVE_RECON_HALT, "existing live exposure without verified Kraken key")
                self.alerter.alert("Unverified live exposure: no authenticated Kraken connection. "
                                   "Live entries remain locked.", kind="critical")
            else:
                try:
                    self.guard.verify_live_orders()
                except Exception:
                    # Fail closed; positions and their ledger evidence remain
                    # available for explicit reconciliation rather than discarded.
                    pass
        md = self._market_data or ReadOnlyExchange(make_exchange(self.cfg["data"]["exchange"]))
        self.trader = Trader(self.cfg, self.conn, self.guard, md, self.paper, self.alerter)
        self.trader.restore_paper_stops()
        self._enforce_market_health()
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
        self._enforce_market_health()
        feed = market_health(self.conn, self.cfg)
        if feed["healthy"] and not feed["last_errors"] and not self.state.get(
                "market_refresh_process_error"):
            self.trader.deploy_candidates()
        report = None
        try:
            report = self.trader.tick()
            if report.errors:
                print("tick errors:", "; ".join(report.errors))
        except Exception as e:  # noqa: BLE001 - a broken loop must not open trades
            self.guard.halt_entries(f"trading loop error: {e}")
        # Separate research-only league: never writes to candidate/real-money accounts.
        # A failed lab experiment cannot halt the production PAPER worker.
        try:
            from bots.paper.league import tick as lab_tick
            if feed["healthy"] and not feed["last_errors"] and report is not None and report.healthy:
                lab_tick(self.conn, self.cfg, now_ms(), allow_entries=not bool(
                    self.state.get("owner_pause") or self.state.get("entry_halt")),
                    price_source=self.trader.md)
        except Exception as e:  # noqa: BLE001
            print(f"Research paper league tick skipped: {e}", flush=True)
        # Paper arena: evolved bots on fake money, same isolation as the league.
        try:
            from bots.paper.arena import tick as arena_tick
            if feed["healthy"] and not feed["last_errors"] and report is not None and report.healthy:
                out = arena_tick(self.conn, self.cfg, now_ms(), allow_entries=not bool(
                    self.state.get("owner_pause") or self.state.get("entry_halt")),
                    price_source=self.trader.md)
                if out.get("retired"):
                    print(f"Paper arena retired {out['retired']} bot(s) to the graveyard", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"Paper arena tick skipped: {e}", flush=True)
        # Arbitrage scanner + paper arbitrage: public order books only, fake money.
        # Its own failures never touch the trading loop or the risk guard.
        try:
            self._arbitrage_tick()
        except Exception as e:  # noqa: BLE001
            print(f"Arbitrage scan skipped: {e}", flush=True)
        # Re-assert the independent whitelist gate even if Trader saw only healthy
        # active-bot pairs and tried to lift a previous error halt.
        self._enforce_market_health()
        self._record_mode()
        poll = getattr(self.alerter, "poll", None)
        if poll:
            poll(lambda bot_id, ok, who: decide_graduation(self.conn, bot_id, ok, who),
                 self._command)
        self._scheduled()

    def _arbitrage_tick(self) -> None:
        acfg = self.cfg.get("arbitrage") or {}
        if not acfg.get("enabled", False):
            return
        now = now_ms()
        last = self.state.get("arb_last_scan_ms")
        if last and now - int(last) < int(acfg.get("scan_seconds", 120)) * 1000:
            return
        from bots.arb.paper import tick as arb_paper_tick
        from bots.arb.scanner import make_venues, scan

        if self._arb_venues is None:
            self._arb_venues = make_venues(acfg)
            fcfg = acfg.get("funding") or {}
            if fcfg.get("enabled"):
                from bots.data.fetch import make_exchange
                self._arb_futures = make_exchange(fcfg.get("venue", "krakenfutures"))
        self.state.set("arb_last_scan_ms", str(now))
        result = scan(self.conn, self.cfg, self._arb_venues, now, futures_api=self._arb_futures)
        if result.get("errors"):
            print("arbitrage scan errors: " + "; ".join(
                f"{k}: {v}" for k, v in result["errors"].items()), flush=True)
        arb_paper_tick(self.conn, self.cfg, result.get("edges") or [], now,
                       allow_entries=not bool(self.state.get("owner_pause")))
        self._arbitrage_live(result.get("edges") or [], now)

    def _arbitrage_live(self, edges: list, now: int) -> None:
        """Real-money arbitrage, if and only if every gate in bots.arb.live passes."""
        from bots.arb import live as arb_live

        lcfg = (self.cfg.get("arbitrage") or {}).get("live") or {}
        if not lcfg.get("enabled") or str(self.env.get("MODE", "")).upper() != "LIVE":
            return
        if self._arb_live_clients is None:
            try:
                self._arb_live_clients = arb_live.make_live_venues(self.cfg["arbitrage"], self.env)
                self.alerter.alert("Live arbitrage connections verified for: "
                                   + ", ".join(sorted(self._arb_live_clients)), kind="info")
            except Exception as e:  # noqa: BLE001 - no keys or a key that can withdraw: stay locked
                self._arb_live_clients = {}
                self.state.log("arb_live_locked", str(e)[:200])
                self.alerter.alert(f"Live arbitrage stays locked: {e}", kind="critical")
        out = arb_live.tick(self.conn, self.cfg, self.env, self.state, self._arb_live_clients,
                            edges, now, alerter=self.alerter)
        if out.get("attempted"):
            print(f"live arbitrage: {out}", flush=True)

    HELP = ("Commands:\n/status - today's numbers\n/bots - every running bot\n"
            "/digest - this morning's briefing\n/pause - stop opening new trades "
            "(exits and stops keep working)\n/resume - allow new trades again\n"
            "/kill confirm - close every live position, cancel orders, lock live mode\n"
            "/evolve - start an evolution run now (one at a time)\n"
            "/arbresume confirm - clear a live arbitrage halt after reconciling both venues")

    def _command(self, text: str) -> str:
        parts = text.strip().split()
        cmd = parts[0].lower() if parts else ""
        arg = " ".join(parts[1:]).lower()
        if cmd == "/arbresume":
            from bots.arb.live import ARB_LIVE_HALT
            if arg != "confirm":
                return (f"Arbitrage halt: {self.state.get(ARB_LIVE_HALT) or 'none'}. Reconcile both "
                        "venues first, then send /arbresume confirm.")
            self.state.clear(ARB_LIVE_HALT)
            self.state.log("arb_live_resume", "owner cleared the arbitrage halt")
            return "Arbitrage halt cleared. Live arbitrage will resume when every gate passes."
        if cmd in ("/status", "/report"):
            return daily_report_text(self.conn, now_ms())
        if cmd == "/digest":
            row = latest_digest(self.conn)
            return for_telegram(row["text"]) if row else "No briefing yet today."
        if cmd == "/bots":
            return self._bots_text()
        if cmd == "/pause":
            self.guard.pause("Telegram")
            return "Paused: no new trades until you send /resume. Open positions keep their stops."
        if cmd == "/resume":
            self.guard.resume("Telegram")
            return "Resumed: new trades allowed again."
        if cmd == "/evolve":
            if self._backfill is not None and self._backfill.poll() is None:
                return "History is still downloading; evolution starts by itself after that."
            if self.start_evolution():
                return "Evolution started. You get a summary here when it finishes."
            return "An evolution run is already in progress."
        if cmd == "/kill":
            if arg != "confirm":
                return ("This closes every live position, cancels all orders and locks live "
                        "mode until you run confirm-live. Send `/kill confirm` to do it.")
            self.guard.kill("owner command via Telegram")
            return "Kill switch pulled. Live positions closed, mode is PAPER."
        return self.HELP

    def _bots_text(self) -> str:
        rows = account_rows(self.conn, "live") + account_rows(self.conn, "paper")
        if not rows:
            return "No bots running yet."
        lines = []
        for a in rows:
            days = (now_ms() - a["started_at"]) / DAY_MS
            lines.append(f"{a['bot_id']} [{a['mode']}] {a['pair']} {a['pnl'] / a['allocation']:+.1%} "
                         f"over {days:.0f}d, {a['trades']} trades, "
                         f"{'position open' if a['qty'] else 'flat'}, {a['status']}")
        return for_telegram("\n".join(lines))

    def _due(self, name: str, hour: int) -> bool:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour < hour or self.state.get(f"last_{name}") == today:
            return False
        self.state.set(f"last_{name}", today)
        return True

    def _scheduled(self) -> None:
        self._check_market_refresh_process()
        w = self.cfg["worker"]
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
        if self.state.get("last_mood_update") != hour:
            self.state.set("last_mood_update", hour)
            try:
                update_fear_greed(self.conn)
            except Exception as e:  # noqa: BLE001 - stale mood blocks mood-filtered entries
                print(f"Fear & Greed update failed: {e}")
        if (self.cfg.get("telegram") or {}).get("hourly_report", False):
            last = self.state.get("last_hourly_report")
            if last is None:
                self.state.set("last_hourly_report", hour)  # no burst right after a restart
            elif last != hour:
                self.state.set("last_hourly_report", hour)
                try:
                    from bots.report.hourly import chunks, hourly_report_text
                    for part in chunks(hourly_report_text(self.conn, self.cfg, now_ms())):
                        self.alerter.alert(part, kind="report")
                except Exception as e:  # noqa: BLE001 - a report must never stop trading
                    print(f"hourly report failed: {e}", flush=True)
        acfg = self.cfg.get("analyst") or {}
        rcfg = self.cfg.get("research") or {}
        if rcfg.get("enabled") and self._due("research", int(rcfg.get("hour_utc", 6))):
            self._spawn("research")
        if acfg.get("enabled") and self._due("digest", acfg["hour_utc"]):
            self._spawn("digest")  # sends its own Telegram message
        if self._due("report", w["daily_report_hour_utc"]):
            self.alerter.alert(daily_report_text(self.conn, now_ms(), self.cfg), kind="report")
        if self._due("backup", w["backup_hour_utc"]):
            try:
                backup_database(self.db_path, resolve_path(w["backups_dir"]), w["backups_keep"])
                if w.get("offsite_backup", True):
                    send_offsite_backup(self.db_path, self.alerter)
            except Exception as e:  # noqa: BLE001
                self.alerter.alert(f"Database backup failed: {e}", kind="risk")
        if needs_backfill(self.conn, self.cfg) and (
                self._backfill is None or
                (self._backfill.poll() is not None and time.time() - self._backfill_at > 3600)):
            # History backfill takes hours and resumes where it stopped; retried hourly if it fails.
            self._backfill = self._spawn("fetch")
            self._backfill_at = time.time()
        backfilling = self._backfill is not None and self._backfill.poll() is None
        self._check_evolution_process()
        # Avoid launching another Python/pandas process during the memory-heavy
        # evolution or history backfill on the 512 MB Render instance.
        if not backfilling and self._evolution is None and self._market_refresh is None:
            interval = max(60, int(w.get("market_refresh_seconds", 300)))
            if time.time() - self._market_refresh_at >= interval:
                self._market_refresh_at = time.time()
                self._market_refresh = self._spawn("update")
        # Evolution must not train on stale history or a failed public feed.
        health = market_health(self.conn, self.cfg)
        if (not backfilling and self._evolution is None and self._market_refresh is None
                and health["healthy"] and not health["last_errors"]
                and not self.state.get("market_refresh_process_error")
                and (self._due("evolution", w["evolution_hour_utc"])
                     or self._evolution_retry_due())):
            self.start_evolution()

    def _enforce_market_health(self) -> None:
        """Block new entries when any whitelisted pair is stale or a refresh failed."""
        health = market_health(self.conn, self.cfg)
        problem = health["last_errors"] or self.state.get("market_refresh_process_error", "")
        if health["unhealthy_pairs"] or problem:
            details = ", ".join(health["unhealthy_pairs"])
            why = f"market data unhealthy ({details or problem})"
            self.guard.halt_entries(why[:500])

    def _check_market_refresh_process(self) -> None:
        if self._market_refresh is None:
            return
        code = self._market_refresh.poll()
        if code is None:
            return
        self._market_refresh = None
        if code != 0:
            self.state.set("market_refresh_process_error", f"refresh exited with code {code}")
            self.guard.halt_entries(f"market data refresh failed (exit code {code})")
            self.state.log("market_refresh_failed", f"process exited with code {code}")
        else:
            self.state.clear("market_refresh_process_error")

    def start_evolution(self) -> bool:
        """Start the nightly evolution in its own process, unless one is already running."""
        self._check_evolution_process()
        if self._evolution is not None:
            return False
        self._evolution = self._spawn("evolve")
        return True

    def _check_evolution_process(self) -> None:
        """Reap a finished evolution process; a bad exit is reported, not swallowed."""
        if self._evolution is None:
            return
        code = self._evolution.poll()
        if code is None:
            return
        self._evolution = None
        if code != 0:
            why = ("killed: the host ran out of memory" if code in (-9, 137)
                   else f"exit code {code}")
            self.alerter.alert(f"The evolution run stopped before finishing ({why}). Nothing "
                               "graduates from it. Check the Render logs; a memory kill means "
                               "the instance is too small.", kind="critical")
            self.state.log("evolution_failed", f"evolution process ended with {why}")

    def _evolution_retry_due(self) -> bool:
        """Today's run died unfinished and has not been retried yet: try once more."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("last_evolution") != today or self.state.get("evolution_retry") == today:
            return False
        row = self.conn.execute("SELECT run_id FROM runs WHERE finished_at IS NULL AND "
                                "started_at >= ? ORDER BY started_at DESC LIMIT 1",
                                (today,)).fetchone()
        if row is None:
            return False
        self.state.set("evolution_retry", today)
        self.state.log("evolution_retry", f"retrying today's unfinished evolution run {row[0]}")
        return True

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
