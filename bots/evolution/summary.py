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


STARVED_WARNING = 0.5


def starvation_note(result: RunResult) -> str | None:
    """A warning when most of the final generation barely traded.

    Bots that cannot fill trades all score alike, so selection turns random and
    nobody improves. That points at execution or sizing settings, not at luck.
    """
    if not result.generations:
        return None
    last = result.generations[-1]
    starved = last.get("starved")
    if starved is None or starved < STARVED_WARNING:
        return None
    return (f"WARNING: {starved:.0%} of the final generation made fewer than the minimum "
            f"trades (median {last['median_trades']:.0f}). Evolution cannot rank bots "
            "that do not trade; check execution.entry_order, fees, position sizes and the "
            "starting balance against the exchange minimums.")


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
        if "median_trades" in gens[-1]:
            lines.append(f"Final generation: median {gens[-1]['median_trades']:.0f} trades "
                         f"per bot over the train windows.")
        note = starvation_note(result)
        if note:
            lines += ["", f"**{note}**"]
        ec = gens[-1].get("elite_coverage") or {}
        if ec:
            lines += ["", "## Elite search coverage (final generation)", "",
                      "- Pairs: " + ", ".join(f"{p} {n}" for p, n in
                                            ec.get("pairs", {}).items()),
                      "- Entry types: " + ", ".join(f"{t} {n}" for t, n in
                                                  ec.get("entry_types", {}).items())]

    lines += ["", "## Finalists (validation → one-time test)", ""]
    lines.append(f"Gate: a bot must have made money in every walk-forward train window on its "
                 f"own ({result.falling_windows} of those windows were falling markets, where "
                 "holding the coin lost money), most of its nearby variants must make money too, "
                 f"and its trade results must beat chance. {result.gate_rejected} top bots were "
                 "held back and kept their one-time test for a later run.")
    for note in result.gate_notes[:10]:
        lines.append(f"- held back: {note}")
    lines.append("")
    coverage = result.candidate_coverage
    if coverage:
        lines.append("Candidate pool coverage: " +
                     ", ".join(f"{p} {n}" for p, n in coverage["pairs"].items()) +
                     "; families: " +
                     ", ".join(f"{t} {n}" for t, n in coverage["entry_types"].items()))
    lines.append(f"Validation rejected {result.validation_rejected} strategies "
                 "before they could access the untouched test period.")
    lines.append(f"Holdout test budget exhausted for {result.test_budget_blocked} finalists; "
                 "their test was NOT run. No historical verdict is proof of future profits.")
    for note in result.validation_notes[:10]:
        lines.append(f"- failed validation: {note}")
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
                verdict += " (historical test result reused, NEVER recomputed)"
            if f.test.get("_audit"):
                verdict += (" (audited protocol " +
                            str(f.test["_audit"].get("protocol", "?")) + ")")
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


def short_summary(result: RunResult) -> str:
    """A few lines for Telegram: what the night's run produced."""
    gens = result.generations
    lines = [f"Evolution run finished: {len(gens)} generations, {result.deaths} bots retired."]
    if gens:
        lines.append(f"Best of the final generation: {gens[-1]['best_genome']}")
    note = starvation_note(result)
    if note:
        lines.append(note)
    lines.append(f"Gate held back {result.gate_rejected} of the top bots.")
    lines.append(f"Validation rejected another {result.validation_rejected}; "
                 "none of these used the untouched test period.")
    if result.validation_notes:
        lines.append("Main validation rejection: " + result.validation_notes[0][:250])
    if result.test_budget_blocked:
        lines.append(f"Protected holdout: {result.test_budget_blocked} attempts withheld "
                     "because the monthly research budget was exhausted.")
    if not result.finalists:
        lines.append("No finalist reached the unseen-data test. Nothing new goes to paper trading.")
    for f in result.finalists:
        if f.passed_test:
            lines.append(f"PASSED → paper trading: {f.genome.describe()} "
                         f"(test {_pct(f.test['total_return'])} vs hold {_pct(f.test['hold_return'])})")
        else:
            lines.append(f"failed: {f.genome.describe()} ({'; '.join(f.reasons) or 'lost on unseen data'})")
    return "\n".join(lines)
