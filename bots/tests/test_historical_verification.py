"""Phase 4: immutable holdout audit, realistic cost stress and explicit risk gates."""
import copy
from dataclasses import replace

import pytest

from bots.evolution import runner as runner_mod
from bots.evolution.fitness import Evaluation, Evaluator
from bots.evolution.runner import Bot, EvolutionRun, RunResult
from bots.evolution.verification import (
    PROTOCOL_VERSION, holdout_used, rules_hash, seal, validate_performance)
from bots.tests.test_evolution import small_cfg, window
from bots.tests.test_strategy import base_genome


def test_unseen_verdict_requires_evidence_and_beats_passive_after_fees(cfg):
    gate = cfg["evolution"]["gate"]
    ok = window(total_return=0.15, hold_return=0.10, max_drawdown=0.04,
                n_trades=30, sharpe=1.25, profit_factor=1.6)
    assert validate_performance(ok, gate, "unseen data") == []
    r = validate_performance(dict(ok, total_return=0.07), gate, "unseen data")
    assert "buy-and-hold" in "; ".join(r)
    r = validate_performance(dict(ok, profit_factor=0.9, n_trades=5,
                                  max_drawdown=0.20), gate, "unseen data")
    assert any("profit factor" in x for x in r)
    assert any("trades" in x for x in r)
    assert any("drawdown" in x for x in r)
    assert validate_performance(dict(ok, sharpe=float("nan")), gate,
                                "unseen data")[0].startswith("unseen data: missing")


def test_historical_costs_and_financial_metrics_are_recorded(cfg):
    from bots.backtest.engine import run_backtest, CostModel
    from bots.tests.conftest import make_series
    import numpy as np
    s = make_series(np.full(14, 100.), np.full(14, 101.),
                    np.full(14, 99.), np.full(14, 100.))
    g = base_genome()
    cost = CostModel.from_config(cfg["backtest"], "BTC/USD",
                                 {"entry_order": "market"})
    r = run_backtest(s, np.array([2, 7]), np.array([5, 10]), g, 0, len(s), cost)
    assert "profit_factor" in r.metrics
    assert r.metrics["profit_factor"] >= 0
    assert r.metrics["fees_paid"] >= 0


def test_sealed_holdout_records_rules_data_period_and_changes_on_new_data(
        cfg, synthetic_market):
    from bots.data.loader import Series
    cfg = small_cfg(cfg)
    run = EvolutionRun.__new__(EvolutionRun)
    from bots.data.loader import make_splits
    sp = make_splits(synthetic_market, 0.6, 0.2)
    s = synthetic_market[("BTC/USD", "1h")]
    g = base_genome()
    a = seal(cfg, sp, s, g)
    b = seal(cfg, sp, s, g)
    assert a == b
    assert a["protocol"] == PROTOCOL_VERSION
    assert a["test_candles"] > 0
    assert len(a["test_data_sha256"]) == 64
    assert a["rules_sha256"] == rules_hash(cfg)
    close = s.close.copy()
    close[s.index_of(sp.validation_end) + 2] *= 1.005
    changed = Series(s.pair, s.timeframe, s.ts.copy(), s.open.copy(),
                     s.high.copy(), s.low.copy(), close, s.volume.copy())
    assert seal(cfg, sp, changed, g)["test_data_sha256"] != a["test_data_sha256"]
    altered = copy.deepcopy(cfg)
    altered["backtest"]["slippage_pct"] *= 2
    assert rules_hash(altered) != rules_hash(cfg)


def test_validation_subwindows_must_be_consistent_without_touching_test(
        conn, cfg, synthetic_market, monkeypatch):
    cfg = small_cfg(cfg)
    gate = cfg["evolution"]["gate"]
    gate.update(validation_subwindows=3, min_positive_validation_windows=2)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    expected = [window(total_return=.05), window(total_return=-.03),
                window(total_return=.02)]
    def subwindow(g, period):
        assert period != run.splits.test
        return expected.pop(0)

    monkeypatch.setattr(run, "_evaluate_period", subwindow)
    v = window()
    assert run._validation_reasons(base_genome(), v) == []
    assert v["positive_subwindows"] == 2
    expected[:] = [window(total_return=.04), window(total_return=-.03),
                   window(total_return=-.02)]
    assert "only 1 of 3" in run._validation_reasons(base_genome(), window())[0]


def test_failed_cost_stress_blocks_holdout_before_any_test_access(
        conn, cfg, synthetic_market, monkeypatch):
    cfg = small_cfg(cfg)
    cfg["evolution"]["gate"]["require_cost_stress"] = True
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    run._serial_evaluator = Evaluator(run.market, run.windows_ms, cfg, run.ctx)
    g = base_genome()
    # This low-frequency genome cannot fill enough conservative limit bids in
    # the synthetic data, so it must NOT be waved through by optimistic scores.
    v = window()
    reasons = run._validation_reasons(g, v)
    assert any("higher fees" in x for x in reasons)
    assert "stress_return" in v


def test_one_time_test_attempt_budget_and_no_rerun(
        conn, cfg, synthetic_market, monkeypatch):
    cfg = small_cfg(cfg)
    cfg["evolution"]["gate"]["max_new_test_attempts_per_epoch"] = 1
    cfg["evolution"]["candidate_pool_fraction"] = 1
    cfg["evolution"]["finalists"] = 2
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    run._train_gate = lambda bot: []
    run._robustness_reasons = lambda g: []
    genomes = [base_genome(pair="BTC/USD"), base_genome(pair="ETH/USD")]
    bots = [Bot(g, Evaluation(g, [window()], 5-i, 0, "sideways", .1), 0, None)
            for i, g in enumerate(genomes)]
    test_calls = []
    def evaluate(g, period):
        if period == run.splits.test:
            test_calls.append(g)
        return window(n_trades=30, total_return=.12, hold_return=0.05)
    monkeypatch.setattr(run, "_evaluate_period", evaluate)
    first_stats = RunResult("r", "synthetic", run.splits)
    first = run._final_checks(bots, first_stats)
    assert len(first) == 1 and len(test_calls) == 1
    assert first_stats.test_budget_blocked == 1
    audit = first[0].test["_audit"]
    assert audit["protocol"] == 4
    assert holdout_used(conn, audit["holdout_epoch"]) == 1
    second = run._final_checks(bots)
    assert second[0].previously_tested
    assert len(test_calls) == 1


def test_old_protocol_test_never_reuses_or_retests_as_current(
        conn, cfg, synthetic_market, monkeypatch):
    import json
    cfg = small_cfg(cfg)
    cfg["evolution"]["candidate_pool_fraction"] = 1
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    g = base_genome(pair="BTC/USD")
    conn.execute("INSERT INTO candidates VALUES (?,'old',?,0.1,'{}',?,1,'awaiting_paper','t')",
                 (g.hash(), g.to_json(), json.dumps(window(total_return=.5))))
    run._train_gate = lambda bot: []
    run._robustness_reasons = lambda g: []
    def evaluate(genome, period):
        if period == run.splits.test:
            raise AssertionError("old holdout may not be consumed again")
        return window()
    monkeypatch.setattr(run, "_evaluate_period", evaluate)
    b = Bot(g, Evaluation(g, [window()], 2, 0, "sideways", .1), 0, None)
    results = run._final_checks([b])
    assert len(results) == 1 and results[0].previously_tested
    assert not results[0].passed_test
    assert "legacy" in results[0].reasons[0]
