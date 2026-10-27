import dataclasses
import random

import numpy as np
import pytest

from bots.strategy import indicators as ind
from bots.strategy.genome import (ENTRY_TYPES, NUMERIC_GENES, Genome, crossover, distance,
                                  mutate, random_genome, repair)
from bots.strategy.signals import SignalCache, compute_signals
from bots.tests.conftest import make_series


def base_genome(**kw) -> Genome:
    v = dict(pair="BTC/USD", timeframe="1h", entry_type="ema_cross", fast_period=5,
             slow_period=20, rsi_low=30, rsi_high=70, breakout_window=10, stop_loss_pct=2.0,
             take_profit_pct=4.0, position_size_pct=50.0, max_trades_per_day=5)
    v.update(kw)
    return repair(v)


def in_range(g: Genome) -> bool:
    d = g.to_dict()
    return all(lo <= d[k] <= hi for k, (lo, hi, _) in NUMERIC_GENES.items()) and \
        g.fast_period < g.slow_period


def test_genome_has_no_short_margin_or_leverage_genes():
    names = {f.name for f in dataclasses.fields(Genome)}
    for banned in ("short", "side", "leverage", "margin", "futures"):
        assert not any(banned in n for n in names)


def test_random_mutated_and_crossed_genomes_stay_in_range():
    rng = random.Random(0)
    for _ in range(500):
        a, b = random_genome(rng), random_genome(rng)
        assert in_range(a)
        assert in_range(mutate(a, rng, rate=1.0, scale=0.5))
        child = crossover(a, b, rng)
        assert in_range(child)
        for k, v in child.to_dict().items():
            if k not in ("fast_period", "slow_period", "mood_min", "mood_max"):  # may be repaired
                assert v in (getattr(a, k), getattr(b, k))


def test_repair_clips_and_orders_periods():
    g = base_genome(fast_period=50, slow_period=20, stop_loss_pct=99, position_size_pct=1)
    assert g.slow_period >= g.fast_period + 5
    assert g.stop_loss_pct == 5.0 and g.position_size_pct == 10.0
    with pytest.raises(ValueError):
        base_genome(pair="SHIB/USD")


def test_mutation_is_small():
    rng = random.Random(1)
    g = base_genome(fast_period=40, slow_period=100)
    m = mutate(g, rng, rate=1.0, scale=0.10, categorical_rate=0.0)
    assert abs(m.slow_period - 100) <= 10
    assert abs(m.stop_loss_pct - 2.0) <= 0.2 + 1e-9


def test_distance_and_json_roundtrip():
    g = base_genome()
    assert distance(g, g) == 0
    assert distance(g, base_genome(pair="ETH/USD")) > 0
    assert Genome.from_dict(g.to_dict()) == g
    assert g.hash() == Genome.from_dict(g.to_dict()).hash()


def test_rsi_bounds_and_ema():
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1, 500))
    r = ind.rsi(c)
    assert np.nanmin(r) >= 0 and np.nanmax(r) <= 100
    assert np.isnan(r[:14]).all()
    e = ind.ema(np.full(10, 5.0), 3)
    assert np.allclose(e, 5.0)


def test_ema_cross_fires_on_turn():
    c = np.r_[np.linspace(100, 80, 60), np.linspace(80, 120, 60)]
    s = make_series(c, c, c, c)
    entry, exit_ = compute_signals(s, base_genome())
    first = np.flatnonzero(entry)
    assert len(first) and 60 <= first[0] < 80
    assert not exit_[first[0]:].any()


@pytest.mark.parametrize("entry_type", ENTRY_TYPES)
def test_signals_never_use_future_candles(entry_type):
    """Signals up to candle k must not change when later candles change."""
    rng = np.random.default_rng(3)
    n = 600
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[c[0], c[:-1]]
    h, lo = np.maximum(o, c) * 1.005, np.minimum(o, c) * 0.995
    vol = rng.lognormal(0, 0.8, n)
    g = base_genome(entry_type=entry_type, fast_period=8, slow_period=30, breakout_window=20,
                    rsi_low=35, rsi_high=65, vol_mult=2.0)
    full_e, full_x = compute_signals(make_series(o, h, lo, c, volume=vol), g)
    k = 400
    c2 = c.copy()
    c2[k:] = c2[k:] * rng.uniform(0.5, 1.5, n - k)
    o2 = np.r_[c2[0], c2[:-1]]
    vol2 = vol.copy()
    vol2[k:] *= 7
    cut_e, cut_x = compute_signals(make_series(o2, np.maximum(o2, c2) * 1.005,
                                               np.minimum(o2, c2) * 0.995, c2, volume=vol2), g)
    assert (full_e[:k] == cut_e[:k]).all() and (full_x[:k] == cut_x[:k]).all()
    assert full_e.any()


def test_signal_cache_reuses_results():
    c = np.linspace(100, 200, 300)
    s = make_series(c, c, c, c)
    cache = SignalCache({("BTC/USD", "1h"): s})
    g1 = base_genome(stop_loss_pct=1.0)
    g2 = base_genome(stop_loss_pct=3.0)  # same signal genes
    _, e1, _ = cache.get(g1)
    _, e2, _ = cache.get(g2)
    assert e1 is e2


def test_atr_is_causal_and_positive():
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1, 300))
    h, l = c + rng.uniform(0.1, 1.0, 300), c - rng.uniform(0.1, 1.0, 300)
    a = ind.atr(h, l, c, 14)
    assert np.isnan(a[:13]).all() and np.isfinite(a[14:]).all() and (a[14:] > 0).all()
    h2, l2, c2 = h.copy(), l.copy(), c.copy()
    h2[200:] += 50  # the future changes; values before it must not
    np.testing.assert_array_equal(a[:200], ind.atr(h2, l2, c2, 14)[:200])


def test_atr_stop_is_clipped_into_the_fixed_stop_range():
    from bots.strategy.signals import stop_fractions, stop_pct_now
    o, h, l, c = (np.full(100, 100.0) for _ in range(4))
    h, l = h + 0.1, l - 0.1  # a calm market: ATR 0.2 -> 2x = 0.4%, below the 0.5% floor
    s = make_series(o, h, l, c)
    g = base_genome(stop_mode="atr", atr_period=14, atr_mult=2.0, stop_loss_pct=3.0)
    frac = stop_fractions(s, g)
    assert frac[:13] == pytest.approx(0.03)  # unsettled ATR falls back to the fixed stop
    assert frac[50:] == pytest.approx(0.005)
    assert stop_pct_now(s, g) == pytest.approx(0.5)
    wild = make_series(o, h + 30, l - 30, c)  # ATR 60 -> 120%, capped at 5%
    assert stop_fractions(wild, g)[-1] == pytest.approx(0.05)
    assert stop_fractions(s, base_genome()) is None
    assert "ATR(14)" in g.describe() and "ATR" not in base_genome().describe()


def test_signal_cache_serves_stops():
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0, 1, 200))
    s = make_series(c, c + 1, c - 1, c)
    cache = SignalCache({("BTC/USD", "1h"): s})
    g = base_genome(stop_mode="atr", atr_mult=3.0)
    a = cache.stops(g)
    assert a is not None and len(a) == 200 and cache.stops(base_genome()) is None
    np.testing.assert_array_equal(a, cache.stops(g))


def test_signal_cache_memory_is_bounded():
    rng = np.random.default_rng(2)
    c = 100 + np.cumsum(rng.normal(0, 1, 500))
    s = make_series(c, c + 1, c - 1, c)
    cache = SignalCache({("BTC/USD", "1h"): s}, max_size=5)
    for i in range(8):
        cache.get(base_genome(fast_period=5 + i, slow_period=40))
        cache.stops(base_genome(stop_mode="atr", atr_period=7 + i))
    assert len(cache._cache) == 5 and len(cache._atr) <= SignalCache.MAX_ATR
    _, entries, _ = cache.get(base_genome(fast_period=12, slow_period=40))
    assert entries.dtype == np.int32
    assert next(iter(cache._atr.values())).dtype == np.float32
