import json

import pytest

from bots.evolution import runner as runner_mod
from bots.evolution.diagnosis import diagnose
from bots.evolution.fitness import score_windows
from bots.evolution.graveyard import Graveyard, failure_summary
from bots.evolution.runner import EvolutionRun
from bots.evolution.summary import render
from bots.tests.test_strategy import base_genome


def window(**kw):
    w = dict(total_return=0.05, hold_return=0.0, max_drawdown=0.05, sharpe=1.0, calmar=1.0,
             n_trades=10, stop_outs=2, fees_paid=1.0, gross_pnl=5.0, rejected_orders=0)
    w.update(kw)
    return w


def test_fitness_penalties(cfg):
    f = cfg["fitness"]
    good = score_windows([window(), window()], f)
    assert score_windows([window(), window(max_drawdown=0.30)], f) < good
    assert score_windows([window(n_trades=2), window(n_trades=2)], f) < good
    assert score_windows([window(hold_return=0.5), window(hold_return=0.5)], f) < good
    assert score_windows([window(sharpe=3.0), window(sharpe=-1.0)], f) < \
        score_windows([window(sharpe=1.0), window(sharpe=1.0)], f)  # consistency


def test_diagnosis_reasons():
    assert "sideways" in diagnose([window(n_trades=30, stop_outs=25)], "sideways", 20)
    assert diagnose([window(n_trades=3)], "uptrend", 20) == "too few trades to trust"
    assert "minimum" in diagnose([window(n_trades=0, rejected_orders=4)], "uptrend", 20)
    assert "fees" in diagnose([window(n_trades=30, stop_outs=1, gross_pnl=2, fees_paid=3)],
                              "uptrend", 20)


def test_graveyard_penalizes_similar_genomes(conn, cfg):
    gy = Graveyard(conn, cfg["graveyard"])
    dead = base_genome()
    for _ in range(cfg["graveyard"]["saturation"]):
        gy.bury("r", 0, dead, "bottom 50%", "stopped out repeatedly in sideways market",
                "sideways", 0.5, -1.0, {})
    near, cause = gy.penalty(base_genome(stop_loss_pct=2.1))
    assert near == pytest.approx(cfg["graveyard"]["penalty"])
    assert cause == "stopped out repeatedly in sideways market"
    far, _ = gy.penalty(base_genome(pair="SOL/USD", timeframe="4h", entry_type="breakout",
                                    position_size_pct=100, take_profit_pct=15))
    assert far == 0
    # Being outcompeted is not a mistake to avoid.
    other = base_genome(pair="ETH/USD")
    gy.bury("r", 0, other, "bottom 50%", "outcompeted by stronger bots", "uptrend", 0.5, 0.0, {})
    assert gy.penalty(other)[0] == 0
    # The graveyard persists: a new instance reloads it from the database.
    assert Graveyard(conn, cfg["graveyard"]).penalty(dead)[0] > 0
    assert "stopped out" in failure_summary(3, ["stopped out repeatedly in sideways market"])


def small_cfg(cfg):
    cfg["evolution"].update(population_size=24, generations=4, workers=1, seed=42,
                            walk_forward_windows=3, finalists=3)
    cfg["evolution"]["gate"].update(neighbours=0, min_trade_tstat=0)  # tested on their own
    cfg["backtest"]["starting_balance_usd"] = 1000
    return cfg


def test_evolution_run_end_to_end(conn, cfg, synthetic_market):
    cfg = small_cfg(cfg)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    result = run.run()
    assert len(result.generations) == 4
    for g in result.generations:
        assert g["best"] >= g["median"] >= g["worst"]
        assert g["deaths"] >= 24 // 2  # bottom half always dies
    n_dead = conn.execute("SELECT COUNT(*) FROM graveyard").fetchone()[0]
    assert n_dead == result.deaths > 0
    row = conn.execute("SELECT cause, diagnosis, regime FROM graveyard LIMIT 1").fetchone()
    assert all(row)
    assert conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 4
    text = render(result, conn)
    assert "Graveyard" in text and "do not predict or guarantee" in text


def test_population_size_is_constant_and_newcomers_arrive(conn, cfg, synthetic_market):
    cfg = small_cfg(cfg)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    run._serial_evaluator = runner_mod.Evaluator(run.market, run.windows_ms, cfg, run.ctx)
    pop = []
    run._fill(pop, 24, run._newcomer)
    bots = sorted(run._evaluate(pop, None), key=lambda b: b.fitness, reverse=True)
    from bots.evolution.runner import RunResult
    nxt = run._next_generation(0, bots, RunResult("x", "s", run.splits), last=False)
    assert len(nxt) == 24 and len(set(nxt)) == 24
    elites = {b.genome for b in bots[:4]}
    assert elites & set(nxt)


def test_test_set_is_used_once_per_genome(conn, cfg, synthetic_market, monkeypatch):
    cfg = small_cfg(cfg)
    cfg["evolution"]["gate"]["every_window_profitable"] = False  # the gate has its own test
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    run._serial_evaluator = runner_mod.Evaluator(run.market, run.windows_ms, cfg, run.ctx)
    test_calls = []
    real = run._evaluate_period

    def spy(g, period):
        if period == run.splits.test:
            test_calls.append(g)
        m = real(g, period)
        if period == run.splits.validation:
            m = dict(m, total_return=0.01)  # force finalists so the test gate is exercised
        return m

    monkeypatch.setattr(run, "_evaluate_period", spy)
    pop = []
    run._fill(pop, 10, run._newcomer)
    bots = run._evaluate(pop, None)
    first = run._final_checks(bots)
    assert first and len(test_calls) == len(first)
    second = run._final_checks(bots)
    assert len(test_calls) == len(first)  # no second look at the test set
    assert all(f.previously_tested for f in second)
    rows = conn.execute("SELECT test_json, stage FROM candidates").fetchall()
    assert len(rows) == len(first)
    assert all(json.loads(r[0]) and r[1] in ("awaiting_paper", "test_failed") for r in rows)


def test_rule_breakers_die(conn, cfg, synthetic_market):
    cfg = small_cfg(cfg)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    from bots.evolution.fitness import Evaluation
    from bots.evolution.runner import Bot
    ev = Evaluation(base_genome(), [window(max_drawdown=0.40)], 0.0, 0, "sideways", 0.5)
    assert "drawdown" in run._death_cause(Bot(ev.genome, ev, 0, None), 0, 10)
    ev = Evaluation(base_genome(), [window(total_return=-0.1, n_trades=25)], 0.0, 0, "x", 0.5)
    assert "below starting balance" in run._death_cause(Bot(ev.genome, ev, 0, None), 0, 10)
    ev = Evaluation(base_genome(), [window()], 0.0, 0, "x", 0.5)
    assert run._death_cause(Bot(ev.genome, ev, 0, None), 0, 10) is None
    assert "bottom" in run._death_cause(Bot(ev.genome, ev, 0, None), 6, 10)


def test_market_regime_on_intraday_candles():
    import numpy as np

    from bots.evolution.diagnosis import market_regime
    from bots.tests.conftest import make_series

    rng = np.random.default_rng(0)
    n = 24 * 4 * 150  # 150 days of 15m candles
    up = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.004, n)))
    flat = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)) * 0.05)
    down = up[::-1].copy()
    for c, want in ((up, "uptrend"), (down, "downtrend"), (flat, "sideways")):
        s = make_series(c, c, c, c, timeframe="15m")
        assert market_regime(s, 0, n)[0] == want


def test_gate_holds_back_bots_that_lost_in_a_train_window(conn, cfg, synthetic_market, monkeypatch):
    cfg = small_cfg(cfg)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    run._serial_evaluator = runner_mod.Evaluator(run.market, run.windows_ms, cfg, run.ctx)
    pop = []
    run._fill(pop, 10, run._newcomer)  # the finalist pool is the top 20%: two bots
    bots = sorted(run._evaluate(pop, None), key=lambda b: b.fitness, reverse=True)
    # Make every bot profitable in every train window except the first bot's window 2.
    for b in bots:
        for w in b.evaluation.windows:
            w["total_return"] = 0.05
    bots[0].evaluation.windows[1]["total_return"] = -0.01
    assert run._train_gate(bots[0]) == ["lost money in train window 2 of 3"]
    assert run._train_gate(bots[1]) == []
    real = run._evaluate_period
    tested = []

    def spy(g, period):
        m = dict(real(g, period), total_return=0.02, n_trades=10, max_drawdown=0.05)
        if period == run.splits.test:
            tested.append(g)
        return m

    monkeypatch.setattr(run, "_evaluate_period", spy)
    from bots.evolution.runner import RunResult
    result = RunResult("x", "s", run.splits)
    finalists = run._final_checks(bots, result)
    assert bots[0].genome not in tested and result.gate_rejected == 1
    assert finalists and all(f.passed_test and f.reasons == [] for f in finalists)
    # The unseen-data verdict names every rule it failed.
    reasons = run._test_verdict({"total_return": -0.1, "n_trades": 2, "max_drawdown": 0.4})
    assert reasons == ["lost money on unseen data", "only 2 trades on unseen data (need 5)",
                       "drawdown 40% on unseen data (limit 25%)"]
    assert run._test_verdict({"total_return": 0.1, "n_trades": 5, "max_drawdown": 0.2}) == []


def test_robustness_gate_rejects_fragile_and_lucky_bots(conn, cfg, synthetic_market, monkeypatch):
    import numpy as np
    from bots.evolution.fitness import Evaluation
    from bots.strategy.genome import NUMERIC_GENES
    cfg = small_cfg(cfg)
    cfg["evolution"]["gate"].update(neighbours=6, neighbour_min_profitable=0.5,
                                    min_trade_tstat=2.0, min_tstat_trades=10)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    run._serial_evaluator = runner_mod.Evaluator(run.market, run.windows_ms, cfg, run.ctx)
    g = run._newcomer()
    variants = run._neighbours(g, 6, 0.10)
    assert 1 <= len(variants) <= 6 and all(v != g for v in variants)
    assert variants == run._neighbours(g, 6, 0.10)  # deterministic per genome
    for v in variants:
        d, base = v.to_dict(), g.to_dict()
        for name, (lo, hi, _) in NUMERIC_GENES.items():
            assert lo <= d[name] <= hi
            if base[name] != 0 and name not in ("fast_period", "slow_period", "mood_max"):
                assert abs(d[name] - base[name]) <= max(abs(base[name]) * 0.10 + 1e-9, 1)

    def fake_eval(returns):
        def evaluate(genome):
            r = returns.pop(0)
            return Evaluation(genome, [{"total_return": r, "max_drawdown": 0.0, "n_trades": 5,
                                        "hold_return": 0.0}], 0.0, 0, "flat", 0.1)
        return evaluate

    # Fragile: only 2 of 6 variants make money.
    monkeypatch.setattr(run._serial_evaluator, "evaluate", fake_eval([0.1, 0.1, -0.1, -0.1, -0.1, -0.1][:len(variants)] + [0.1] * 6))
    monkeypatch.setattr(run, "_trade_returns", lambda g, p: np.array([0.02] * 12 + [-0.01] * 8))
    reasons = run._robustness_reasons(g)
    assert any("fragile" in r for r in reasons)
    # Robust variants but trades indistinguishable from chance.
    monkeypatch.setattr(run._serial_evaluator, "evaluate", fake_eval([0.1] * 12))
    monkeypatch.setattr(run, "_trade_returns", lambda g, p: np.array([0.05, -0.05] * 10))
    reasons = run._robustness_reasons(g)
    assert len(reasons) == 1 and "chance" in reasons[0]
    # Too few trades to judge.
    monkeypatch.setattr(run._serial_evaluator, "evaluate", fake_eval([0.1] * 12))
    monkeypatch.setattr(run, "_trade_returns", lambda g, p: np.array([0.05] * 4))
    assert "need 10" in run._robustness_reasons(g)[0]
    # Passes: variants profitable and a clear edge.
    monkeypatch.setattr(run._serial_evaluator, "evaluate", fake_eval([0.1] * 12))
    monkeypatch.setattr(run, "_trade_returns", lambda g, p: np.array([0.02] * 25 + [-0.005] * 5))
    assert run._robustness_reasons(g) == []


def test_short_summary_names_the_outcome(conn, cfg, synthetic_market):
    from bots.evolution.summary import short_summary
    cfg = small_cfg(cfg)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    result = run.run()
    text = short_summary(result)
    assert "Evolution run finished: 4 generations" in text and "Gate held back" in text
    assert ("paper trading" in text) or ("failed" in text)
    assert all(f"held back: " in render(result, conn) for _ in result.gate_notes[:1])
