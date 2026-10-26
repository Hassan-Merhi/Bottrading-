"""Readable report of an evolution run."""
from __future__ import annotations

import sqlite3
from collections import Counter

import pandas as pd

from bots.evolution.runner import RunResult


def _date(ms: int) -> str:
    return pd.to_datetime(ms, unit="ms").strftime("%Y-%m-%d")


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def render(result: RunResult, conn: sqlite3.Connection) -> str:
    sp = result.splits
    lines = [
        f"# Evolution run {result.run_id}",
        "",
        f"Data: **{result.data_label}**",
        "",
        "Backtest results are simulations. They do not predict or guarantee future profits.",
        "",
        "## Data split",
        f"- Train (evolution, walk-forward): {_date(sp.start)} → {_date(sp.train_end)}",
        f"- Validation (survivor selection): {_date(sp.train_end)} → {_date(sp.validation_end)}",
        f"- Test (one check per finalist): {_date(sp.validation_end)} → {_date(sp.end)}",
        "",
        "## Generations (fitness)",
        "",
        "| gen | best | median | worst | deaths |",
        "|---:|---:|---:|---:|---:|",
    ]
    gens = result.generations
    step = max(1, len(gens) // 20)
    shown = gens[::step] + ([gens[-1]] if gens and (len(gens) - 1) % step else [])
    for g in shown:
        lines.append(f"| {g['generation']} | {g['best']:.3f} | {g['median']:.3f} | "
                     f"{g['worst']:.3f} | {g['deaths']} |")
    if gens:
        lines += ["", f"Best bot in the final generation: {gens[-1]['best_genome']}"]

    lines += ["", "## Finalists (validation → one-time test)", ""]
    lines.append(f"Gate: a bot must have made money in every walk-forward train window on its "
                 f"own ({result.falling_windows} of those windows were falling markets, where "
                 f"holding the coin lost money). {result.gate_rejected} top bots were held back "
                 "by that rule and kept their one-time test for a later run.")
    lines.append("")
    if not result.finalists:
        lines.append("No bot passed the gate and was profitable on validation data, so none was "
                     "sent to the test set. Nothing graduates from this run.")
    else:
        lines += ["| strategy | val return | val hold | test return | test hold | test trades | "
                  "test max DD | result |",
                  "|---|---:|---:|---:|---:|---:|---:|---|"]
        for f in result.finalists:
            verdict = "passed → paper trading" if f.passed_test else "failed → retired"
            if f.reasons:
                verdict += " (" + "; ".join(f.reasons) + ")"
            if f.previously_tested:
                verdict += " (test result from an earlier run)"
            lines.append(
                f"| {f.genome.describe()} | {_pct(f.validation['total_return'])} | "
                f"{_pct(f.validation['hold_return'])} | {_pct(f.test['total_return'])} | "
                f"{_pct(f.test['hold_return'])} | {f.test['n_trades']} | "
                f"{f.test['max_drawdown'] * 100:.1f}% | {verdict} |")

    rows = conn.execute("SELECT diagnosis, COUNT(*) FROM graveyard WHERE run_id=? "
                        "GROUP BY diagnosis ORDER BY COUNT(*) DESC", (result.run_id,)).fetchall()
    lines += ["", f"## Graveyard ({result.deaths} bots retired this run)", ""]
    lines += [f"- {r[0]}: {r[1]}" for r in rows]
    regimes = Counter(r[0] for r in conn.execute(
        "SELECT regime FROM graveyard WHERE run_id=?", (result.run_id,)))
    if regimes:
        lines += ["", "Market conditions in each dead bot's worst window: " +
                  ", ".join(f"{k} {v}" for k, v in regimes.most_common())]
    if gens:
        lines += ["", "Last generation: " + gens[-1]["failure_summary"]]
    return "\n".join(lines) + "\n"
