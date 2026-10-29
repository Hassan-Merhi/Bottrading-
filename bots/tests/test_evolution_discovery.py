"""Phase 3 regression checks for diverse and honest candidate discovery."""
from collections import Counter
from types import SimpleNamespace

import pytest

from bots.evolution.diagnosis import diagnose
from bots.evolution.diversity import coverage, diverse_top
from bots.evolution.fitness import score_windows
from bots.evolution.graveyard import Graveyard
from bots.evolution.runner import EvolutionRun, RunResult
from bots.tests.test_evolution import small_cfg, window
from bots.tests.test_strategy import base_genome


def _ranked(pairs):
    """Already fit-ranked objects where the earlier item is the better one."""
    return [SimpleNamespace(genome=base_genome(pair=p, entry_type=t), rank=i)
            for i, (p, t) in enumerate(pairs)]


def test_diverse_top_preserves_best_and_searches_beyond_dominant_coin():
    items = _ranked([("SOL/USD", "breakout")] * 8 + [
        ("BTC/USD", "ema_cross"), ("ETH/USD", "rsi_reversion"),
        ("ADA/USD", "macd"), ("LINK/USD", "squeeze_breakout")])
    chosen = diverse_top(items, 5, lambda b: (b.genome.pair, b.genome.entry_type),
                         max_pair_share=0.4, max_family_share=0.2)
    assert len(chosen) == 5
    assert chosen[0] is items[0]
    assert len({x.genome.pair for x in chosen}) >= 4
    assert sum(x.genome.pair == "SOL/USD" for x in chosen) <= 2
    assert chosen == diverse_top(items, 5, lambda b: (b.genome.pair, b.genome.entry_type),
                                 max_pair_share=0.4, max_family_share=0.2)


def test_diversity_falls_back_when_only_one_family_is_available():
    items = _ranked([("SOL/USD", "ema_cross")] * 10)
    chosen = diverse_top(items, 6, lambda b: (b.genome.pair, b.genome.entry_type))
    assert [x.rank for x in chosen] == list(range(6))
    assert coverage([x.genome for x in chosen])["pairs"] == {"SOL/USD": 6}
    assert diverse_top(items, 0, lambda b: (b.genome.pair, b.genome.entry_type)) == []
    with pytest.raises(ValueError):
        diverse_top(items, 2, lambda b: (b.genome.pair, b.genome.entry_type),
                    max_pair_share=0)


def test_fitness_prefers_cross_window_net_profits_and_verifiable_fills(cfg):
    f = cfg["fitness"]
    good = [window(), window()]
    strong = score_windows(good, f)
    weak = score_windows([window(), window(total_return=-0.01)], f)
    hard_to_fill = score_windows([window(unfilled_limit_orders=50),
                                 window(unfilled_limit_orders=50)], f)
    assert weak < strong
    assert hard_to_fill < strong


def test_no_fill_is_unknown_not_a_proven_defective_strategy(conn, cfg):
    uncertain = [window(n_trades=0, unfilled_limit_orders=30, gross_pnl=0)]
    cause = diagnose(uncertain, "sideways", 20)
    assert cause == "limit fills unverified at candle resolution"
    g = base_genome()
    gy = Graveyard(conn, cfg["graveyard"])
    gy.bury("run", 1, g, "bottom 50%", cause, "sideways", 0.2, -1.0, {})
    assert gy.penalty(g)[0] == 0  # log it, but don't learn the wrong lesson


def test_validation_failures_never_consume_untouched_test_set(
        conn, cfg, synthetic_market, monkeypatch):
    cfg = small_cfg(cfg)
    run = EvolutionRun(conn, cfg, synthetic_market, "synthetic", log=lambda _: None)
    run._serial_evaluator = None  # overridden evaluation paths below
    run._train_gate = lambda b: []
    run._robustness_reasons = lambda g: []
    evaluated = []
    def fake_eval(g, period):
        evaluated.append(period)
        assert period != run.splits.test
        return window(total_return=-0.1, n_trades=2)

    run._evaluate_period = fake_eval
    from bots.evolution.fitness import Evaluation
    from bots.evolution.runner import Bot
    bots = []
    for i, p in enumerate(("SOL/USD", "BTC/USD", "ETH/USD", "ADA/USD", "LINK/USD",
                           "DOGE/USD", "XRP/USD", "SOL/USD", "BTC/USD", "ETH/USD")):
        g = base_genome(pair=p)
        bots.append(Bot(g, Evaluation(g, [window()], 10-i, 0, "sideways", 0.1), 0, None))
    rr = RunResult("run", "synthetic", run.splits)
    assert run._final_checks(bots, rr) == []
    assert rr.validation_rejected > 0
    assert rr.validation_notes
    assert rr.candidate_coverage["pairs"]
    assert evaluated and all(p == run.splits.validation for p in evaluated)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
