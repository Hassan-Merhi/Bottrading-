"""Command-line entry point.

    python -m bots.main fetch                 # backfill history from Kraken public trades
    python -m bots.main import-csv FILE --pair BTC/USD --timeframe 15m
    python -m bots.main update                # top up the latest candles
    python -m bots.main fetch-mood            # Fear & Greed Index history
    python -m bots.main status                # what data is stored
    python -m bots.main evolve                # run evolution and write a report
    python -m bots.main run                   # always-on worker + dashboard (PAPER by default)
    python -m bots.main dashboard             # dashboard only
    python -m bots.main report                # print today's report
    python -m bots.main backup                # copy the database to backups/
    python -m bots.main digest                # AI analyst briefing (information only)
    python -m bots.main watchdog              # alert if the worker stopped reporting
    python -m bots.main confirm-live          # owner: re-confirm after a crash or kill switch

Only RiskGuard (bots/risk/guard.py) can place orders.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

import pandas as pd

from bots.config import load_config, resolve_path
from bots.db import connect


def cmd_fetch(cfg, conn, args) -> None:
    from bots.data.fetch import backfill_all
    from bots.data.mood import update_fear_greed

    print(f"Fear & Greed: {update_fear_greed(conn)} days stored")
    backfill_all(conn, cfg)


def cmd_fetch_mood(cfg, conn, args) -> None:
    from bots.data.mood import update_fear_greed

    print(f"Fear & Greed: {update_fear_greed(conn)} days stored")


def cmd_import_csv(cfg, conn, args) -> None:
    from bots.data.fetch import import_kraken_csv

    n = import_kraken_csv(conn, args.file, args.pair, args.timeframe)
    print(f"Imported {n} candles for {args.pair} {args.timeframe}")


def cmd_update(cfg, conn, args) -> None:
    """Independent public-data refresh, including when there are zero active bots."""
    from bots.data.fetch import make_exchange
    from bots.data.health import refresh_market_pairs

    exchange = make_exchange(cfg["data"]["exchange"])
    result = refresh_market_pairs(conn, exchange, cfg)
    for pair, n in result["updated_candles"].items():
        health = result["pairs"][pair]
        print(f"{pair}: {n} candles, {health['status']}, "
              f"closed candle {health['age_minutes']} min ago", flush=True)
    if result["errors"]:
        print("Market data refresh failed: " + "; ".join(result["errors"]), file=sys.stderr,
              flush=True)
        raise SystemExit(1)


def cmd_status(cfg, conn, args) -> None:
    from bots.data.store import candle_range

    for pair in cfg["data"]["pairs"]:
        r = candle_range(conn, pair, cfg["data"]["base_timeframe"])
        if r is None:
            print(f"{pair}: no data")
        else:
            a, b = (pd.to_datetime(x, unit="ms") for x in r[:2])
            print(f"{pair}: {r[2]} candles, {a} → {b}")


def cmd_evolve(cfg, conn, args) -> None:
    from bots.data.loader import load_market, series_from_df, resample
    from bots.evolution.runner import EvolutionRun
    from bots.evolution.summary import render

    ecfg = cfg["evolution"]
    if args.generations:
        ecfg["generations"] = args.generations
    if args.population:
        ecfg["population_size"] = args.population
    if args.seed is not None:
        ecfg["seed"] = args.seed
    dcfg = cfg["data"]
    if args.synthetic:
        from bots.data.synthetic import random_walk_candles

        label = "SYNTHETIC random walk (pipeline check only, says nothing about real markets)"
        market = {}
        for k, pair in enumerate(dcfg["pairs"]):
            base = random_walk_candles(4 * 24 * 365 * 2, dcfg["base_timeframe"],
                                       start_price=[30_000, 2_000, 50][k % 3], seed=k)
            for tf in dcfg["timeframes"]:
                market[(pair, tf)] = series_from_df(pair, tf, resample(base, dcfg["base_timeframe"], tf))
    else:
        source = dcfg.get("history_source", "kraken_trades")
        label = ("Kraken historical candles" if source == "kraken_trades" else
                 f"{source} USD candle history, recent candles from Kraken")
        market = load_market(conn, dcfg["pairs"], dcfg["timeframes"], dcfg["base_timeframe"])
        if not market:
            sys.exit("No candles stored. Run `python -m bots.main fetch` (or import-csv) first.")
        missing = [p for p in dcfg["pairs"] if (p, dcfg["timeframes"][0]) not in market]
        if missing:
            print(f"Warning: no data for {missing}; evolving on the other pairs only.")
    ctx = None
    if args.synthetic:
        from bots.data.synthetic import synthetic_fear_greed
        from bots.strategy.context import build_context

        first = min(int(sr.ts[0]) for sr in market.values())
        ctx = build_context(market, synthetic_fear_greed(first, 2 * 366))
    result = EvolutionRun(conn, cfg, market, label, ctx=ctx).run()
    report = render(result, conn)
    out = resolve_path("runs") / f"{result.run_id}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(report)
    print()
    print(report)
    print(f"Report written to {out}")
    from bots.analyst.digest import for_telegram
    from bots.evolution.summary import short_summary
    from bots.report.telegram import make_alerter

    alerter = make_alerter(cfg)
    alerter.alert(for_telegram(short_summary(result)), kind="report")
    send = getattr(alerter, "send_file", None)
    if send:
        send(out, caption=f"Full evolution report {result.run_id}")


def _start_dashboard(cfg, db_path) -> None:
    import uvicorn

    from bots.report.dashboard import create_app

    port = int(os.environ.get("PORT") or cfg["dashboard"]["port"])
    uvicorn.run(create_app(db_path, cfg), host=cfg["dashboard"]["host"], port=port,
                log_level="warning")


def cmd_run(cfg, conn, args) -> None:
    from bots.worker import Worker

    db_path = resolve_path(cfg["database_path"])
    conn.close()
    if not args.no_dashboard:
        threading.Thread(target=_start_dashboard, args=(cfg, db_path), daemon=True).start()
    Worker(cfg, dict(os.environ), db_path).run_forever()


def cmd_dashboard(cfg, conn, args) -> None:
    conn.close()
    _start_dashboard(cfg, resolve_path(cfg["database_path"]))


def cmd_report(cfg, conn, args) -> None:
    from bots.report.daily import daily_report_text
    from bots.risk.state import now_ms

    print(daily_report_text(conn, now_ms()))


def cmd_digest(cfg, conn, args) -> None:
    """Write today's market briefing and send it to Telegram. Information only."""
    from bots.analyst.digest import for_telegram, run_digest
    from bots.report.telegram import make_alerter

    text = run_digest(conn, cfg)
    print(text)
    if not args.no_send:
        make_alerter(cfg).alert(for_telegram(text), kind="report")


def cmd_backup(cfg, conn, args) -> None:
    from bots.worker import backup_database, send_offsite_backup

    w = cfg["worker"]
    db_path = resolve_path(cfg["database_path"])
    print(backup_database(db_path, resolve_path(w["backups_dir"]), w["backups_keep"]))
    if args.send:
        from bots.report.telegram import make_alerter

        print(send_offsite_backup(db_path, make_alerter(cfg)) or "no Telegram configured")


def cmd_watchdog(cfg, conn, args) -> None:
    from bots.report.telegram import make_alerter
    from bots.risk.state import HEARTBEAT, RiskState, now_ms

    hb = RiskState(conn).get(HEARTBEAT)
    age_h = (now_ms() - int(hb)) / 3_600_000 if hb else float("inf")
    if age_h > cfg["risk"]["heartbeat_hours"]:
        make_alerter(cfg).alert(f"System has not reported in {age_h:.1f} hours.",
                                kind="critical")
        sys.exit(1)
    print(f"OK: last report {age_h * 60:.0f} min ago")


def cmd_audit_live(cfg, conn, args) -> None:
    """Owner-run READ-ONLY Kraken reconciliation, never places a trade."""
    from bots.live.exchange import (has_api_keys, make_live_exchange,
                                    check_cannot_withdraw, check_can_query)
    from bots.live.reconcile import ReconciliationError, reconcile_live_orders
    from bots.risk.ledger import Ledger
    from bots.risk.state import LIVE_RECON_HALT, RiskState
    env = dict(os.environ)
    if not has_api_keys(env):
        sys.exit("No authenticated Kraken key. No live order audit can be performed.")
    exchange = make_live_exchange(env)
    try:
        check_cannot_withdraw(exchange)
        check_can_query(exchange)
        result = reconcile_live_orders(exchange, Ledger(conn), cfg["data"]["pairs"])
    except Exception:
        RiskState(conn).set(LIVE_RECON_HALT, "read-only Kraken audit failed; manual review required")
        sys.exit("Live audit FAILED. Reconciliation lock retained; inspect live Kraken orders.")
    print(f"Read-only Kraken audit passed: {result['open_orders']} known orders, "
          f"{result['active_live_accounts']} live accounts. No orders submitted.")
    if getattr(args, "acknowledge", False):
        RiskState(conn).clear(LIVE_RECON_HALT)
        RiskState(conn).log("reconciliation_approved",
                            "owner acknowledged verified Kraken order reconciliation")
        print("Reconciliation lock cleared; other live confirmation and safety gates remain.")


def cmd_confirm_live(cfg, conn, args) -> None:
    """The owner's confirmation. Clears the crash lock and, with a fresh
    LIVE_CONFIRMED.txt, the kill switch. Never raises any limit."""
    from bots.risk.guard import confirm_file_path, read_confirmed_capital, risk_config_problems
    from bots.risk.state import CRASH_LOCK, KILL_SWITCH, LIVE_EPOCH, RiskState

    state = RiskState(conn)
    path = confirm_file_path(dict(os.environ))
    problems = risk_config_problems(cfg)
    confirmed = read_confirmed_capital(path)
    cap = cfg["risk"].get("max_total_capital_usd")
    if state.is_set(KILL_SWITCH):
        if problems or confirmed is None or confirmed != cap or not cap:
            sys.exit(f"Kill switch stays on. Write the capital limit ({cap}) into a NEW {path} "
                     "first; the old file was revoked by the kill switch.")
        state.clear(KILL_SWITCH)
        state.set(LIVE_EPOCH, int(state.get(LIVE_EPOCH, "1")) + 1)
        state.log("owner_confirmed", f"kill switch cleared by owner; capital {confirmed}")
        print("Kill switch cleared. Live losses are counted from zero again.")
    if state.is_set(CRASH_LOCK):
        state.clear(CRASH_LOCK)
        state.log("owner_confirmed", "crash lock cleared by owner")
        print("Crash lock cleared.")
    print("LIVE still also needs: MODE=LIVE env, live_enabled: true, LIVE_CONFIRMED.txt matching "
          "max_total_capital_usd, and a verified Kraken key. Anything missing keeps PAPER.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bots")
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="backfill history from Kraken public trades")
    imp = sub.add_parser("import-csv", help="import a Kraken OHLCVT CSV file")
    imp.add_argument("file")
    imp.add_argument("--pair", required=True)
    imp.add_argument("--timeframe", default="15m")
    sub.add_parser("update", help="top up the latest candles")
    sub.add_parser("fetch-mood", help="download the Fear & Greed Index history")
    sub.add_parser("status", help="show stored data")
    ev = sub.add_parser("evolve", help="run evolution")
    ev.add_argument("--generations", type=int)
    ev.add_argument("--population", type=int)
    ev.add_argument("--seed", type=int)
    ev.add_argument("--synthetic", action="store_true",
                    help="use random-walk data to check the pipeline (results are meaningless)")
    run = sub.add_parser("run", help="always-on worker (+ dashboard)")
    run.add_argument("--no-dashboard", action="store_true")
    sub.add_parser("dashboard", help="serve the dashboard only")
    sub.add_parser("report", help="print today's report")
    bk = sub.add_parser("backup", help="back up the database")
    bk.add_argument("--send", action="store_true",
                    help="also send a copy without price candles to your Telegram chat")
    dg = sub.add_parser("digest", help="AI analyst: write and send today's market briefing")
    dg.add_argument("--no-send", action="store_true", help="print only, no Telegram message")
    sub.add_parser("watchdog", help="alert if the worker has stopped reporting")
    audit = sub.add_parser("audit-live", help="read-only verification of live Kraken orders")
    audit.add_argument("--acknowledge", action="store_true",
                       help="clear reconciliation lock only after a successful authenticated audit")
    sub.add_parser("confirm-live", help="owner confirmation after a crash or kill switch")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    db_path = resolve_path(cfg["database_path"])
    if getattr(args, "synthetic", False):
        # Keep synthetic deaths out of the real graveyard.
        db_path = db_path.with_name("synthetic.db")
    conn = connect(db_path)
    {"fetch": cmd_fetch, "import-csv": cmd_import_csv, "update": cmd_update, "fetch-mood": cmd_fetch_mood,
     "status": cmd_status, "evolve": cmd_evolve, "run": cmd_run, "dashboard": cmd_dashboard,
     "report": cmd_report, "backup": cmd_backup, "digest": cmd_digest, "watchdog": cmd_watchdog,
     "confirm-live": cmd_confirm_live, "audit-live": cmd_audit_live}[args.cmd](cfg, conn, args)


if __name__ == "__main__":
    main()
