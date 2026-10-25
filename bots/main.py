"""Command-line entry point.

    python -m bots.main fetch                 # backfill history from Kraken public trades
    python -m bots.main import-csv FILE --pair BTC/USD --timeframe 15m
    python -m bots.main update                # top up the latest candles
    python -m bots.main status                # what data is stored
    python -m bots.main evolve                # run evolution and write a report

Phases 5–9 (RiskGuard, paper trader, reporting, deployment, live) are not
built yet. Nothing here places orders.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from bots.config import load_config, resolve_path
from bots.db import connect


def cmd_fetch(cfg, conn, args) -> None:
    from bots.data.fetch import backfill_all

    backfill_all(conn, cfg)


def cmd_import_csv(cfg, conn, args) -> None:
    from bots.data.fetch import import_kraken_csv

    n = import_kraken_csv(conn, args.file, args.pair, args.timeframe)
    print(f"Imported {n} candles for {args.pair} {args.timeframe}")


def cmd_update(cfg, conn, args) -> None:
    from bots.data.fetch import make_exchange, update_recent

    ex = make_exchange(cfg["data"]["exchange"])
    for pair in cfg["data"]["pairs"]:
        n = update_recent(conn, ex, pair, cfg["data"]["base_timeframe"])
        print(f"{pair}: {n} candles")


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
        label = "Kraken historical candles"
        market = load_market(conn, dcfg["pairs"], dcfg["timeframes"], dcfg["base_timeframe"])
        if not market:
            sys.exit("No candles stored. Run `python -m bots.main fetch` (or import-csv) first.")
        missing = [p for p in dcfg["pairs"] if (p, dcfg["timeframes"][0]) not in market]
        if missing:
            print(f"Warning: no data for {missing}; evolving on the other pairs only.")
    result = EvolutionRun(conn, cfg, market, label).run()
    report = render(result, conn)
    out = resolve_path("runs") / f"{result.run_id}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(report)
    print()
    print(report)
    print(f"Report written to {out}")


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
    sub.add_parser("status", help="show stored data")
    ev = sub.add_parser("evolve", help="run evolution")
    ev.add_argument("--generations", type=int)
    ev.add_argument("--population", type=int)
    ev.add_argument("--seed", type=int)
    ev.add_argument("--synthetic", action="store_true",
                    help="use random-walk data to check the pipeline (results are meaningless)")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    db_path = resolve_path(cfg["database_path"])
    if getattr(args, "synthetic", False):
        # Keep synthetic deaths out of the real graveyard.
        db_path = db_path.with_name("synthetic.db")
    conn = connect(db_path)
    {"fetch": cmd_fetch, "import-csv": cmd_import_csv, "update": cmd_update,
     "status": cmd_status, "evolve": cmd_evolve}[args.cmd](cfg, conn, args)


if __name__ == "__main__":
    main()
