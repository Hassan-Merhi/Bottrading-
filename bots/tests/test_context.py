import json

import numpy as np
import pytest

from bots.data.mood import load_fear_greed, store_fear_greed, update_fear_greed
from bots.strategy.context import (DAY_MS, MarketContext, build_context, daily_from_series,
                                   mood_values, trend_mask)
from bots.strategy.genome import Genome
from bots.strategy.signals import compute_signals, signal_key
from bots.tests.conftest import make_series
from bots.tests.test_strategy import base_genome

H = 3_600_000
D0 = 1_700_000_000_000 // DAY_MS * DAY_MS


def hourly(closes_per_day):
    """Hourly candles, one constant price per day."""
    c = np.repeat(np.asarray(closes_per_day, float), 24)
    return make_series(c, c, c, c, start_ms=D0)


def test_daily_from_series_keeps_complete_days():
    s = hourly([1, 2, 3])
    d = daily_from_series(s)
    assert list(d.close) == [1, 2, 3] and list(d.ts) == [D0, D0 + DAY_MS, D0 + 2 * DAY_MS]


def test_trend_mask_only_uses_finished_days():
    # Price falls for 5 days, then jumps on day 5.
    s = hourly([10, 9, 8, 7, 6, 20, 20])
    d = daily_from_series(s)
    mask = trend_mask(s, d, 3)
    day5 = slice(5 * 24, 6 * 24)
    # During day 5 (except at its final close) the jump is not yet a finished daily close.
    assert not mask[day5][:-1].any()
    # Once day 5 has closed, its close (20) is above the 3-day average.
    assert mask[day5][-1] and mask[6 * 24:].all()


def test_trend_mask_without_reference_blocks():
    s = hourly([1, 2, 3])
    assert not trend_mask(s, None, 2).any()


def test_mood_values_are_causal_and_expire():
    s = hourly([1] * 8)
    fg = (np.array([D0, D0 + DAY_MS]), np.array([20.0, 80.0]))
    v = mood_values(s, fg)
    assert np.isnan(v[: 23]).all()            # day 0's value is not known until day 0 ends
    assert v[23] == 20 and v[24 + 22] == 20   # known from the close of day 0
    assert v[24 + 23] == 80                   # day 1's value once day 1 ends
    assert np.isnan(v[-1])                    # more than 3 days old: treated as missing
    assert np.isnan(mood_values(s, None)).all()


def test_filters_gate_entries_and_block_when_data_missing():
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, 24 * 300)))
    s = make_series(c, c * 1.002, c * 0.998, c, start_ms=D0)
    plain = base_genome()
    e_plain, x_plain = compute_signals(s, plain)
    assert e_plain.any()
    days = D0 + np.arange(300) * DAY_MS
    calm = MarketContext(daily={"BTC/USD": daily_from_series(s)},
                         fear_greed=(days, np.full(300, 30.0)))
    fearful = base_genome(mood_min=0, mood_max=40)
    greedy = base_genome(mood_min=60, mood_max=100)
    e_fear, x_fear = compute_signals(s, fearful, calm)
    # Mood 30 is inside 0-40, so entries match the unfiltered bot once day 0's mood is known.
    assert not e_fear[:23].any() and (e_fear[23:] == e_plain[23:]).all()
    assert not compute_signals(s, greedy, calm)[0].any()
    assert not compute_signals(s, fearful, None)[0].any()        # no mood data -> no entries
    trend = base_genome(trend_filter="btc_daily", trend_period=20)
    e_trend, x_trend = compute_signals(s, trend, calm)
    assert e_trend.sum() <= e_plain.sum() and (x_trend == x_plain).all()  # exits unaffected
    assert not compute_signals(s, trend, MarketContext())[0].any()


def test_new_entry_types_fire():
    n = 400
    c = np.full(n, 100.0)
    c[300:] = np.linspace(101, 110, n - 300)
    o = np.r_[c[0], c[:-1]]
    vol = np.ones(n)
    vol[300] = 10
    s = make_series(o, np.maximum(o, c) + 0.01, np.minimum(o, c) - 0.01, c, volume=vol)
    e, _ = compute_signals(s, base_genome(entry_type="volume_spike", fast_period=5,
                                          breakout_window=20, vol_mult=3))
    assert e[300] and e.sum() == 1
    rng = np.random.default_rng(1)
    # Volatility contracts steadily, then price breaks out of the tight range.
    c = np.r_[100 + rng.normal(0, np.linspace(2, 0.02, 300)), [105] * 5]
    o = np.r_[c[0], c[:-1]]
    s = make_series(o, np.maximum(o, c), np.minimum(o, c), c)
    e, x = compute_signals(s, base_genome(entry_type="squeeze_breakout", breakout_window=20,
                                          fast_period=5, slow_period=60))
    assert e[300]
    assert x[300:].sum() == 0  # still above the middle band


def test_signal_key_separates_filters():
    a, b = base_genome(), base_genome(mood_min=20, mood_max=50)
    assert signal_key(a) != signal_key(b)
    assert signal_key(base_genome(trend_period=30)) == signal_key(base_genome(trend_period=90))
    assert signal_key(base_genome(trend_filter="btc_daily", trend_period=30)) != \
        signal_key(base_genome(trend_filter="btc_daily", trend_period=90))


def test_old_genomes_without_new_genes_still_load():
    old = {k: v for k, v in base_genome().to_dict().items()
           if k not in ("vol_mult", "trend_period", "mood_min", "mood_max", "trend_filter")}
    g = Genome.from_dict(json.loads(json.dumps(old)))
    assert g.trend_filter == "none" and not g.mood_filter_on
    assert "Fear" in base_genome(mood_min=10, mood_max=40).describe()
    assert "BTC > 50-day" in base_genome(trend_filter="btc_daily").describe()


def test_fear_greed_download_and_storage(conn):
    class Session:
        def get(self, url, params=None, timeout=None):
            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"data": [{"value": "25", "timestamp": str(D0 // 1000 + DAY_MS // 1000)},
                                     {"value": "71", "timestamp": str(D0 // 1000)},
                                     {"value": "999", "timestamp": "1"}]}
            return R()

    assert update_fear_greed(conn, Session()) == 2
    ts, values = load_fear_greed(conn)
    assert list(ts) == [D0, D0 + DAY_MS] and list(values) == [71, 25]
    store_fear_greed(conn, [(D0, 50)])
    assert list(load_fear_greed(conn)[1]) == [50, 25]


def test_build_context_from_market(synthetic_market):
    ctx = build_context(synthetic_market, None)
    assert set(ctx.daily) == {"BTC/USD", "ETH/USD"}
    assert ctx.daily["BTC/USD"].timeframe == "1d" and len(ctx.daily["BTC/USD"]) > 100
