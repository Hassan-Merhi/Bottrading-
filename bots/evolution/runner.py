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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from bots.backtest.engine import CostModel, run_backtest
from bots.data.loader import Series, Splits, make_splits
from bots.evolution.diagnosis import diagnose
from bots.evolution.fitness import Evaluation, Evaluator, score_windows
from bots.evolution.graveyard import Graveyard, failure_summary
from bots.data.mood import load_fear_greed
from bots.strategy.context import MarketContext, build_context
from bots.strategy.genome import Genome, crossover, mutate, random_genome

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


@dataclass
class RunResult:
    run_id: str
    data_label: str
    splits: Splits
    generations: list[dict] = field(default_factory=list)
    finalists: list[Finalist] = field(default_factory=list)
    deaths: int = 0


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
            self._save_population(bots)
            result.finalists = self._final_checks(bots)
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
        stats = {"generation": gen, "best": float(fit.max()), "median": float(np.median(fit)),
                 "worst": float(fit.min()), "deaths": len(diagnoses),
                 "best_genome": bots[0].genome.describe(), "failure_summary": summary}
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

        n_elite = max(2, int(round(size * self.ecfg["elite_fraction"])))
        elites = [b.genome for b in survivors[:n_elite]] or [b.genome for b in bots[:2]]
        population = list(dict.fromkeys(elites))
        n_new = int(round(size * self.ecfg["newcomer_fraction"]))
        self._fill(population, size - n_new, lambda: self._offspring(elites))
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
        cost = CostModel.from_config(self.cfg["backtest"], g.pair)
        return run_backtest(s, entries, exits, g, s.index_of(period[0]), s.index_of(period[1]),
                            cost).metrics

    def _final_checks(self, bots: list[Bot]) -> list[Finalist]:
        pool = [b.genome for b in bots[: max(1, int(len(bots) * 0.2))]]
        scored = []
        for g in pool:
            val = self._evaluate_period(g, self.splits.validation)
            scored.append((score_windows([val], self.cfg["fitness"]), g, val))
        scored.sort(key=lambda t: t[0], reverse=True)
        chosen = [t for t in scored if t[2]["total_return"] > 0][: self.ecfg["finalists"]]
        finalists = []
        for vfit, g, val in chosen:
            row = self.conn.execute("SELECT test_json, passed_test FROM candidates "
                                    "WHERE genome_hash=?", (g.hash(),)).fetchone()
            if row is not None:
                finalists.append(Finalist(g, vfit, val, json.loads(row[0]), bool(row[1]), True))
                continue
            test = self._evaluate_period(g, self.splits.test)
            passed = test["total_return"] > 0
            self.conn.execute(
                "INSERT INTO candidates (genome_hash, run_id, genome_json, validation_fitness, "
                "validation_json, test_json, passed_test, stage, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (g.hash(), self.run_id, g.to_json(), vfit, json.dumps(val), json.dumps(test),
                 int(passed), "awaiting_paper" if passed else "test_failed",
                 datetime.now(timezone.utc).isoformat()),
            )
            finalists.append(Finalist(g, vfit, val, test, passed, False))
        self.conn.commit()
        return finalists
