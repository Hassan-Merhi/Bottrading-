"""Run evolution: birth, evaluation, death, breeding, and the final candidate checks.

Data use, to avoid fooling ourselves:

* Train (oldest part): every generation is scored on walk-forward windows here.
* Validation (next part): only the final generation's best bots are scored
  here, to pick survivors. Evolution never sees it, so it can't overfit to it.
* Test (newest part): each finalist is checked exactly once, ever. The result
  is stored per genome and never recomputed.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from bots.backtest.engine import CostModel, run_backtest
from bots.data.loader import Series, Splits, make_splits
from bots.evolution.diagnosis import diagnose, market_regime
from bots.evolution.diversity import coverage, diverse_top
from bots.evolution.fitness import Evaluation, Evaluator, score_windows
from bots.evolution.verification import (DAY_MS, PROTOCOL_VERSION, holdout_used,
                                         rules_hash, seal, validate_performance)
from bots.evolution.graveyard import Graveyard, failure_summary
from bots.data.mood import load_fear_greed
from bots.strategy.context import MarketContext, build_context
from bots.strategy.genome import (ATR_GENES, NUMERIC_GENES, Genome, crossover, mutate,
                                  random_genome, repair)

_worker_evaluator: Evaluator | None = None


def _init_worker(market, windows_ms, cfg, ctx) -> None:
    global _worker_evaluator
    _worker_evaluator = Evaluator(market, windows_ms, cfg, ctx)


def _evaluate_in_worker(g: Genome) -> Evaluation:
    assert _worker_evaluator is not None
    return _worker_evaluator.evaluate(g)


@dataclass
class Bot:
    genome: Genome
    evaluation: Evaluation
    penalty: float
    penalty_cause: str | None

    @property
    def fitness(self) -> float:
        return self.evaluation.score - self.penalty


@dataclass
class Finalist:
    genome: Genome
    validation_fitness: float
    validation: dict
    test: dict
    passed_test: bool
    previously_tested: bool
    reasons: list[str] = field(default_factory=list)   # why the test check failed, if it did


@dataclass
class RunResult:
    run_id: str
    data_label: str
    splits: Splits
    generations: list[dict] = field(default_factory=list)
    finalists: list[Finalist] = field(default_factory=list)
    deaths: int = 0
    gate_rejected: int = 0       # top bots kept from the test by the gate (see gate_notes)
    gate_notes: list[str] = field(default_factory=list)   # "strategy: reason" per held-back bot
    falling_windows: int = 0     # train windows where simply holding the coin lost money
    validation_rejected: int = 0  # passed training gate but did not pass validation
    validation_notes: list[str] = field(default_factory=list)
    # Distribution across pairs and strategies in the final candidate pool.
    candidate_coverage: dict = field(default_factory=dict)
    test_budget_blocked: int = 0  # strategy passes selection but holdout attempts are exhausted


class EvolutionRun:
    def __init__(self, conn: sqlite3.Connection, cfg: dict,
                 market: dict[tuple[str, str], Series], data_label: str,
                 log: Callable[[str], None] = print, ctx: MarketContext | None = None):
        self.conn = conn
        self.cfg = cfg
        self.ecfg = cfg["evolution"]
        self.market = market
        # Daily trend data and Fear & Greed history for the filter genes.
        self.ctx = ctx or build_context(market, load_fear_greed(conn))
        self.data_label = data_label
        self.log = log
        self.rng = random.Random(self.ecfg.get("seed"))
        self.pairs = sorted({p for p, _ in market})
        self.timeframes = sorted({tf for _, tf in market})
        split = self.ecfg["split"]
        self.splits = make_splits(market, split["train"], split["validation"])
        self.windows_ms = self.splits.windows(self.ecfg["walk_forward_windows"])
        self.graveyard = Graveyard(conn, cfg["graveyard"])
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self._evaluated: dict[Genome, Evaluation] = {}

    # -- birth -------------------------------------------------------------

    def _newcomer(self) -> Genome:
        return self._born(lambda: random_genome(self.rng, self.pairs, self.timeframes))

    def _offspring(self, parents: list[Genome]) -> Genome:
        def make() -> Genome:
            a, b = self.rng.sample(parents, 2) if len(parents) > 1 else (parents[0], parents[0])
            return mutate(crossover(a, b, self.rng), self.rng, self.ecfg["mutation_rate"],
                          self.ecfg["mutation_scale"], pairs=self.pairs, timeframes=self.timeframes)
        return self._born(make)

    def _born(self, make: Callable[[], Genome], tries: int = 10) -> Genome:
        """Avoid birthing genomes squarely inside a known failure cluster."""
        g = make()
        for _ in range(tries):
            penalty, _ = self.graveyard.penalty(g)
            if penalty < self.graveyard.max_penalty:
                break
            g = make()
        return g

    def _fill(self, population: list[Genome], target: int, make: Callable[[], Genome]) -> None:
        seen = set(population)
        attempts = 0
        while len(population) < target and attempts < target * 50:
            g = make()
            attempts += 1
            if g not in seen:
                population.append(g)
                seen.add(g)

    # -- evaluation --------------------------------------------------------

    def _evaluate(self, genomes: list[Genome], pool) -> list[Bot]:
        todo = [g for g in genomes if g not in self._evaluated]
        if todo:
            if pool is None:
                evaluator = self._serial_evaluator
                results = [evaluator.evaluate(g) for g in todo]
            else:
                results = list(pool.map(_evaluate_in_worker, todo, chunksize=4))
            self._evaluated.update(zip(todo, results))
        bots = []
        for g in genomes:
            penalty, cause = self.graveyard.penalty(g)
            bots.append(Bot(g, self._evaluated[g], penalty, cause))
        return bots

    # -- death -------------------------------------------------------------

    def _death_cause(self, bot: Bot, rank: int, population: int) -> str | None:
        ev = bot.evaluation
        dd_limit = self.ecfg["bot_max_drawdown_pct"] / 100
        if ev.max_drawdown > dd_limit:
            return f"drawdown {ev.max_drawdown:.0%} exceeded {dd_limit:.0%} limit"
        if ev.total_trades >= self.ecfg["death_min_trades"] and ev.compounded_return < 0:
            return f"below starting balance after {ev.total_trades} trades"
        if rank >= population * (1 - self.ecfg["death_fraction"]):
            return f"bottom {self.ecfg['death_fraction']:.0%} of generation"
        return None

    # -- main loop ---------------------------------------------------------

    def run(self) -> RunResult:
        result = RunResult(self.run_id, self.data_label, self.splits)
        self.conn.execute(
            "INSERT INTO runs (run_id, started_at, config_json, data_label) VALUES (?,?,?,?)",
            (self.run_id, datetime.now(timezone.utc).isoformat(), json.dumps(self.cfg),
             self.data_label),
        )
        self.conn.commit()
        size = self.ecfg["population_size"]
        workers = self.ecfg.get("workers") or os.cpu_count() or 1
        self.log(f"evolution {self.run_id}: {size} bots x {self.ecfg['generations']} generations, "
                 f"{workers} worker process(es), {len(self.market)} series")
        self._serial_evaluator = Evaluator(self.market, self.windows_ms, self.cfg, self.ctx)
        pool = None
        if workers > 1:
            pool = ProcessPoolExecutor(workers, initializer=_init_worker,
                                       initargs=(self.market, self.windows_ms, self.cfg,
                                                 self.ctx))
        try:
            population: list[Genome] = []
            self._fill(population, size, self._newcomer)
            bots: list[Bot] = []
            for gen in range(self.ecfg["generations"]):
                bots = sorted(self._evaluate(population, pool), key=lambda b: b.fitness,
                              reverse=True)
                population = self._next_generation(gen, bots, result,
                                                   last=gen == self.ecfg["generations"] - 1)
                # Keep only what the next generation can reuse; the rest is dead weight.
                keep = set(population)
                self._evaluated = {g: e for g, e in self._evaluated.items() if g in keep}
            self._save_population(bots)
            result.finalists = self._final_checks(bots, result)
        finally:
            if pool is not None:
                pool.shutdown()
        self.conn.execute("UPDATE runs SET finished_at=? WHERE run_id=?",
                          (datetime.now(timezone.utc).isoformat(), self.run_id))
        self.conn.commit()
        return result

    def _next_generation(self, gen: int, bots: list[Bot], result: RunResult,
                         last: bool) -> list[Genome]:
        size = self.ecfg["population_size"]
        n = len(bots)
        survivors: list[Bot] = []
        diagnoses: list[str] = []
        for rank, bot in enumerate(bots):
            cause = self._death_cause(bot, rank, n)
            if cause is None:
                survivors.append(bot)
                continue
            ev = bot.evaluation
            diagnosis = diagnose(ev.windows, ev.regime, self.cfg["fitness"]["min_trades"])
            diagnoses.append(diagnosis)
            self.graveyard.bury(self.run_id, gen, bot.genome, cause, diagnosis, ev.regime,
                                ev.volatility, bot.fitness, ev.summary())

        fit = np.array([b.fitness for b in bots])
        summary = failure_summary(gen, diagnoses)
        n_elite = max(2, int(round(size * self.ecfg["elite_fraction"])))
        eligible = [b for b in survivors] or bots[:2]
        elites = diverse_top(eligible, n_elite, lambda b: (b.genome.pair, b.genome.entry_type),
                             max_pair_share=self.ecfg.get("max_elite_pair_share", 0.4),
                             max_family_share=self.ecfg.get("max_elite_family_share", 0.2))
        stats = {"generation": gen, "best": float(fit.max()), "median": float(np.median(fit)),
                 "worst": float(fit.min()), "deaths": len(diagnoses),
                 "best_genome": bots[0].genome.describe(), "failure_summary": summary,
                 "elite_coverage": coverage([b.genome for b in elites])}
        result.generations.append(stats)
        result.deaths += len(diagnoses)
        self.conn.execute(
            "INSERT INTO generations (run_id, generation, best, median, worst, deaths, "
            "best_genome_json, failure_summary) VALUES (?,?,?,?,?,?,?,?)",
            (self.run_id, gen, stats["best"], stats["median"], stats["worst"], len(diagnoses),
             bots[0].genome.to_json(), summary),
        )
        self.conn.commit()
        self.log(f"gen {gen:3d}  best {stats['best']:7.3f}  median {stats['median']:7.3f}  "
                 f"worst {stats['worst']:7.3f}  deaths {len(diagnoses):3d}  "
                 f"| {bots[0].genome.describe()}")
        if last:
            return [b.genome for b in bots]

        elite_genomes = [b.genome for b in elites]
        population = list(dict.fromkeys(elite_genomes))
        n_new = int(round(size * self.ecfg["newcomer_fraction"]))
        self._fill(population, size - n_new, lambda: self._offspring(elite_genomes))
        self._fill(population, size, self._newcomer)
        return population

    def _save_population(self, bots: list[Bot]) -> None:
        self.conn.executemany(
            "INSERT INTO population (run_id, rank, genome_json, fitness, summary_json) "
            "VALUES (?,?,?,?,?)",
            [(self.run_id, i, b.genome.to_json(), b.fitness, json.dumps(b.evaluation.summary()))
             for i, b in enumerate(bots)])
        self.conn.commit()

    # -- validation and the one-time test check ---------------------------

    def _evaluate_period(self, g: Genome, period: tuple[int, int]) -> dict:
        s = self.market[(g.pair, g.timeframe)]
        _, entries, exits = self._serial_evaluator.cache.get(g)
        stops = self._serial_evaluator.cache.stops(g)
        cost = CostModel.from_config(self.cfg["backtest"], g.pair, self.cfg.get("execution"))
        # Purge the first few candles after each research boundary. This
        # ensures orders are not simulated immediately at a split transition.
        gap = int(self.ecfg.get("gate", {}).get("embargo_hours", 0) * 3_600_000)
        beginning = period[0] + (gap if period in (self.splits.validation, self.splits.test)
                                 else 0)
        return run_backtest(s, entries, exits, g, s.index_of(beginning),
                            s.index_of(period[1]), cost, stops).metrics

    def _train_gate(self, bot: Bot) -> list[str]:
        """Why a top bot may not spend its test check: a train window that lost money."""
        gate = self.ecfg.get("gate") or {}
        if not gate.get("every_window_profitable", False):
            return []
        losing = [i for i, w in enumerate(bot.evaluation.windows) if w["total_return"] <= 0]
        if losing:
            return [f"lost money in train window {', '.join(str(i + 1) for i in losing)} "
                    f"of {len(bot.evaluation.windows)}"]
        min_regimes = int(gate.get("min_train_regimes", 1))
        if min_regimes > 1:
            s = self.market[(bot.genome.pair, bot.genome.timeframe)]
            regimes = {
                market_regime(s, s.index_of(a), s.index_of(b))[0]
                for a, b in self.windows_ms
            } - {"unknown"}
            if len(regimes) < min_regimes:
                return [f"only {len(regimes)} distinct training market regimes "
                        f"(need {min_regimes})"]
        return []

    def _neighbours(self, g: Genome, n: int, scale: float) -> list[Genome]:
        """Variants of g with every active numeric gene nudged by up to +-scale."""
        rng = random.Random(g.hash())  # deterministic per genome, so results are repeatable
        out = []
        for _ in range(n):
            v = g.to_dict()
            for name, (lo, hi, is_int) in NUMERIC_GENES.items():
                if name in ATR_GENES and g.stop_mode != "atr":
                    continue
                if v[name] == 0:
                    continue  # a switched-off gene stays off
                step = v[name] * rng.uniform(-scale, scale)
                if is_int and abs(step) < 1:
                    step = rng.choice((-1, 1))
                v[name] = v[name] + step
            ng = repair(v)
            if ng != g:
                out.append(ng)
        return out

    def _trade_returns(self, g: Genome, period: tuple[int, int]) -> np.ndarray:
        """Per-trade return (P&L over the cash put in) across a period."""
        s = self.market[(g.pair, g.timeframe)]
        _, entries, exits = self._serial_evaluator.cache.get(g)
        stops = self._serial_evaluator.cache.stops(g)
        cost = CostModel.from_config(self.cfg["backtest"], g.pair, self.cfg.get("execution"))
        r = run_backtest(s, entries, exits, g, s.index_of(period[0]), s.index_of(period[1]),
                         cost, stops)
        return np.array([t.pnl / (t.qty * t.entry_price) for t in r.trades], dtype=float)

    def _robustness_reasons(self, g: Genome) -> list[str]:
        """Why a bot may be a lucky point: fragile to small changes, or trades that look like chance."""
        gate = self.ecfg.get("gate") or {}
        reasons = []
        # Cheapest hard failure first: avoid running up to eight expensive
        # neighbouring backtests for a strategy that lacks enough real fills
        # or whose net trade returns already fail the statistical gate.
        t_min = float(gate.get("min_trade_tstat", 0))
        if t_min > 0:
            rets = self._trade_returns(g, (self.splits.start, self.splits.train_end))
            need = int(gate.get("min_tstat_trades", 10))
            if len(rets) < need:
                return [f"only {len(rets)} trades over the train period (need {need} to judge)"]
            sd = float(rets.std(ddof=1))
            t = float(rets.mean() / sd * np.sqrt(len(rets))) if sd > 0 else 0.0
            if not np.isfinite(t) or t < t_min:
                return [f"trade results not distinguishable from chance "
                        f"(t={t:.1f} over {len(rets)} trades, need {t_min:g})"]
        n = int(gate.get("neighbours", 0))
        if n > 0:
            variants = self._neighbours(g, n, float(gate.get("neighbour_scale", 0.1)))
            if variants:
                ok = sum(self._serial_evaluator.evaluate(v).compounded_return > 0 for v in variants)
                share = ok / len(variants)
                if share < float(gate.get("neighbour_min_profitable", 0)):
                    reasons.append(f"fragile: only {ok} of {len(variants)} nearby variants made "
                                   "money (a lucky setting, not an edge)")
        return reasons

    def _test_verdict(self, test: dict) -> list[str]:
        """One-time holdout gate after actual fees, limits, slippage and spread."""
        return validate_performance(test, self.ecfg.get("gate") or {}, "unseen data")

    def _validation_reasons(self, g: Genome, val: dict) -> list[str]:
        """Only validation data can be used for robustness/parameter decisions."""
        gate = self.ecfg.get("gate") or {}
        reasons = validate_performance(val, gate, "validation")
        if reasons:
            return reasons
        s = self.market[(g.pair, g.timeframe)]
        a = self.splits.validation[0]
        b = self.splits.validation[1]
        embargo = int(gate.get("embargo_hours", 0) * 3_600_000)
        a += embargo
        parts = int(gate.get("validation_subwindows", 0))
        required = int(gate.get("min_positive_validation_windows", 0))
        if parts and required:
            edges = np.linspace(a, b, parts + 1).astype("int64")
            sub = [self._evaluate_period(g, (int(edges[i]), int(edges[i + 1])))
                   for i in range(parts)]
            profitable = sum(m["total_return"] > 0 and m["n_trades"] > 0 for m in sub)
            val["positive_subwindows"] = profitable
            val["subwindow_count"] = parts
            if profitable < required:
                reasons.append(f"only {profitable} of {parts} validation periods profitable "
                               f"(need {required})")
        if reasons:
            return reasons
        if gate.get("require_cost_stress", False):
            _, entries, exits = self._serial_evaluator.cache.get(g)
            stops = self._serial_evaluator.cache.stops(g)
            original = CostModel.from_config(self.cfg["backtest"], g.pair,
                                             self.cfg.get("execution"))
            stressed = replace(
                original,
                fee=original.fee * float(gate.get("stress_fee_multiplier", 1.25)),
                maker_fee=(original.maker_fee * float(gate.get("stress_fee_multiplier", 1.25))
                           if original.maker_fee is not None else None),
                slippage=original.slippage * float(gate.get("stress_slippage_multiplier", 2)),
                spread_bps=original.spread_bps * float(gate.get("stress_spread_multiplier", 2)),
            )
            m = run_backtest(s, entries, exits, g, s.index_of(a), s.index_of(b),
                             stressed, stops).metrics
            val["stress_return"] = m["total_return"]
            val["stress_trades"] = m["n_trades"]
            if m["total_return"] <= 0 or m["n_trades"] == 0:
                reasons.append("no profit under higher fees, spreads and slippage on validation")
        return reasons

    def _final_checks(self, bots: list[Bot], result: RunResult | None = None) -> list[Finalist]:
        count = max(1, int(len(bots) * self.ecfg.get("candidate_pool_fraction", 0.2)))
        pool = diverse_top(bots, count, lambda b: (b.genome.pair, b.genome.entry_type),
                           max_pair_share=self.ecfg.get("max_candidate_pair_share", 0.4),
                           max_family_share=self.ecfg.get("max_candidate_family_share", 0.2))
        scored = []
        rejected = 0
        notes: list[str] = []
        val_rejected = 0
        val_notes: list[str] = []
        for b in pool:
            reasons = self._train_gate(b) or self._robustness_reasons(b.genome)
            if reasons:
                rejected += 1
                notes.append(f"{b.genome.describe()}: {'; '.join(reasons)}")
                continue
            g = b.genome
            val = self._evaluate_period(g, self.splits.validation)
            val_reasons = self._validation_reasons(g, val)
            if val_reasons:
                val_rejected += 1
                val_notes.append(f"{g.describe()}: {'; '.join(val_reasons)}")
                continue
            scored.append((score_windows([val], self.cfg["fitness"]), g, val))
        scored.sort(key=lambda t: t[0], reverse=True)
        chosen = diverse_top(scored, self.ecfg["finalists"],
                             lambda t: (t[1].pair, t[1].entry_type),
                             max_pair_share=self.ecfg.get("max_candidate_pair_share", 0.4),
                             max_family_share=self.ecfg.get("max_candidate_family_share", 0.2))
        if result is not None:
            result.gate_rejected = rejected
            result.gate_notes = notes
            result.validation_rejected = val_rejected
            result.validation_notes = val_notes
            result.candidate_coverage = coverage([b.genome for b in pool])
            if bots:
                result.falling_windows = sum(
                    w["hold_return"] < 0 for w in bots[0].evaluation.windows)
        finalists = []
        budget_blocked = 0
        epoch = self.splits.validation_end // (30 * DAY_MS)
        max_tests = int(self.ecfg.get("gate", {}).get("max_new_test_attempts_per_epoch", 5))
        consumed = holdout_used(self.conn, epoch)
        for vfit, g, val in chosen:
            row = self.conn.execute("SELECT test_json, passed_test FROM candidates "
                                    "WHERE genome_hash=?", (g.hash(),)).fetchone()
            if row is not None:
                test = json.loads(row[0])
                audit = test.get("_audit", {})
                previous_reasons = list(test.get("gate_failures", []))
                # A repeated result must NOT be re-scored under a changed
                # data period, risk model or verification protocol.
                if audit.get("protocol") != PROTOCOL_VERSION or (
                        audit.get("rules_sha256") != rules_hash(self.cfg)):
                    previous_reasons.append("previously tested with legacy or different rules; "
                                            "requires genuinely new future data")
                passed_previous = bool(row[1]) and not previous_reasons
                finalists.append(Finalist(g, vfit, val, test, passed_previous, True,
                                          previous_reasons))
                continue
            if consumed >= max_tests:
                budget_blocked += 1
                continue
            test = self._evaluate_period(g, self.splits.test)
            # The audit seal is written after the test is run, never used to
            # select or train a strategy.
            test["_audit"] = seal(self.cfg, self.splits,
                                  self.market[(g.pair, g.timeframe)], g)
            reasons = self._test_verdict(test)
            test["gate_failures"] = reasons
            consumed += 1
            passed = not reasons
            self.conn.execute(
                "INSERT INTO candidates (genome_hash, run_id, genome_json, validation_fitness, "
                "validation_json, test_json, passed_test, stage, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (g.hash(), self.run_id, g.to_json(), vfit, json.dumps(val), json.dumps(test),
                 int(passed), "awaiting_paper" if passed else "test_failed",
                 datetime.now(timezone.utc).isoformat()),
            )
            finalists.append(Finalist(g, vfit, val, test, passed, False, reasons))
        if result is not None:
            result.test_budget_blocked = budget_blocked
        self.conn.commit()
        return finalists
