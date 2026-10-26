import numpy as np
import pytest

from bots.backtest.engine import CostModel, hold_return, run_backtest
from bots.tests.conftest import make_series
from bots.tests.test_strategy import base_genome

FEE, SLIP = 0.004, 0.0005
COST = CostModel(fee=FEE, slippage=SLIP, starting_balance=1000, min_qty=0.0, min_cost=5.0)


def flat(n, price=100.0):
    a = np.full(n, price)
    return a.copy(), a.copy(), a.copy(), a.copy()


def run(s, entries, exits, g, cost=COST, start=0, end=None):
    return run_backtest(s, np.array(entries, dtype=int), np.array(exits, dtype=int), g, start,
                        end if end is not None else len(s), cost)


def test_entry_fills_next_open_and_charges_fees_and_slippage():
    o, h, l, c = flat(10)
    o[3] = 101.0  # the signal candle is 2; entry must use candle 3's open
    s = make_series(o, h, l, c)
    g = base_genome(position_size_pct=50, stop_loss_pct=5, take_profit_pct=15)
    r = run(s, [2], [5], g)
    t = r.trades[0]
    assert t.entry_idx == 3 and t.exit_idx == 6 and t.reason == "signal"
    assert t.entry_price == pytest.approx(101.0 * (1 + SLIP))
    assert t.exit_price == pytest.approx(100.0 * (1 - SLIP))
    notional = 500.0
    qty = notional / t.entry_price
    expected_cash = 1000 - notional * (1 + FEE) + qty * t.exit_price * (1 - FEE)
    assert r.equity[-1] == pytest.approx(expected_cash)
    assert t.fees == pytest.approx(notional * FEE + qty * t.exit_price * FEE)


def test_stop_loss_hits_intrabar_and_gaps_fill_at_open():
    o, h, l, c = flat(10)
    l[5] = 97.0  # 2% stop at ~98 gets hit inside candle 5
    s = make_series(o, h, l, c)
    g = base_genome(stop_loss_pct=2, take_profit_pct=10)
    t = run(s, [2], [], g).trades[0]
    assert t.reason == "stop" and t.exit_idx == 5
    assert t.exit_price == pytest.approx(100 * (1 + SLIP) * 0.98 * (1 - SLIP))

    o, h, l, c = flat(10)
    o[5] = h[5] = l[5] = c[5] = 90.0  # gap straight through the stop
    t = run(make_series(o, h, l, c), [2], [], g).trades[0]
    assert t.reason == "stop" and t.exit_price == pytest.approx(90 * (1 - SLIP))


def test_stop_assumed_first_when_stop_and_target_share_a_candle():
    o, h, l, c = flat(10)
    h[4], l[4] = 120.0, 80.0
    g = base_genome(stop_loss_pct=2, take_profit_pct=5)
    t = run(make_series(o, h, l, c), [2], [], g).trades[0]
    assert t.reason == "stop"


def test_take_profit():
    o, h, l, c = flat(10)
    h[6] = 110.0
    g = base_genome(stop_loss_pct=2, take_profit_pct=5)
    t = run(make_series(o, h, l, c), [2], [], g).trades[0]
    assert t.reason == "target" and t.exit_idx == 6
    assert t.exit_price == pytest.approx(100 * (1 + SLIP) * 1.05 * (1 - SLIP))


def test_orders_below_exchange_minimum_are_refused():
    o, h, l, c = flat(10)
    s = make_series(o, h, l, c)
    tiny = CostModel(FEE, SLIP, starting_balance=20, min_qty=0.0, min_cost=5.0)
    r = run(s, [2], [], base_genome(position_size_pct=20), cost=tiny)  # $4 order
    assert r.trades == [] and r.rejected_orders == 1
    qty_min = CostModel(FEE, SLIP, starting_balance=20, min_qty=1.0, min_cost=5.0)
    r = run(s, [2], [], base_genome(position_size_pct=100), cost=qty_min)  # ~0.2 units
    assert r.trades == [] and r.rejected_orders == 1


def test_one_position_at_a_time_and_daily_trade_limit():
    n = 24 * 3
    o, h, l, c = flat(n)
    s = make_series(o, h, l, c)
    entries = list(range(0, n - 1))
    exits = list(range(0, n - 1))
    r = run(s, entries, exits, base_genome(max_trades_per_day=2))
    days = [s.day[t.entry_idx] for t in r.trades]
    assert max(days.count(d) for d in set(days)) <= 2
    for a, b in zip(r.trades, r.trades[1:]):
        assert b.entry_idx > a.exit_idx


def test_never_spends_more_than_cash_and_equity_stays_positive():
    rng = np.random.default_rng(0)
    n = 2000
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    o = np.r_[100, c[:-1]]
    s = make_series(o, np.maximum(o, c) * 1.01, np.minimum(o, c) * 0.99, c)
    entries = np.flatnonzero(rng.random(n) < 0.1)
    exits = np.flatnonzero(rng.random(n) < 0.1)
    r = run(s, entries, exits, base_genome(position_size_pct=100, stop_loss_pct=5,
                                           take_profit_pct=15, max_trades_per_day=10))
    assert len(r.trades) > 20
    assert (r.equity > 0).all()
    for t in r.trades:
        assert t.entry_price * t.qty <= 1000 * 1e6  # sanity
    cash = 1000.0
    for t in r.trades:
        assert t.qty * t.entry_price * (1 + FEE) <= cash + 1e-6
        cash += t.pnl


def test_backtest_does_not_look_ahead():
    """Trades closed before candle k are unchanged when candles after k change."""
    rng = np.random.default_rng(1)
    n = 1500
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[100, c[:-1]]
    entries = np.flatnonzero(rng.random(n) < 0.05)
    exits = np.flatnonzero(rng.random(n) < 0.05)
    g = base_genome(stop_loss_pct=1.5, take_profit_pct=3)
    a = run(make_series(o, o * 1.01, o * 0.99, c), entries, exits, g)
    k = 1000
    c2 = c.copy()
    c2[k:] *= 0.5
    o2 = np.r_[100, c2[:-1]]
    b = run(make_series(o2, o2 * 1.01, o2 * 0.99, c2), entries, exits, g)
    before_a = [t for t in a.trades if t.exit_idx < k]
    before_b = [t for t in b.trades if t.exit_idx < k]
    assert before_a == before_b and len(before_a) > 5
    assert np.allclose(a.equity[:k - 1], b.equity[:k - 1])


def test_open_position_closed_at_window_end_and_metrics():
    o, h, l, c = flat(10)
    c[-1] = 101
    s = make_series(o, h, l, c)
    r = run(s, [1], [], base_genome(stop_loss_pct=5, take_profit_pct=15))
    assert r.trades[-1].reason == "end" and r.trades[-1].exit_idx == 9
    m = r.metrics
    assert m["n_trades"] == 1 and 0 <= m["max_drawdown"] < 1
    assert m["hold_return"] == pytest.approx(hold_return(s, 0, 10, COST))


def test_random_strategies_have_no_edge_on_a_random_walk():
    """With costs, random entries on a driftless walk should lose money on average."""
    rng = np.random.default_rng(7)
    n = 5000
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, n)))
    o = np.r_[100, c[:-1]]
    s = make_series(o, np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998, c)
    rets = []
    for seed in range(30):
        r2 = np.random.default_rng(seed)
        rets.append(run(s, np.flatnonzero(r2.random(n) < 0.02), np.flatnonzero(r2.random(n) < 0.02),
                        base_genome(stop_loss_pct=2, take_profit_pct=4)).metrics["total_return"])
    assert np.mean(rets) < 0


def test_per_candle_stop_is_read_at_the_signal_candle():
    o, h, l, c = flat(12)
    l[5] = 97.5  # a 2% dip after entry at candle 4
    s = make_series(o, h, l, c)
    g = base_genome(stop_loss_pct=5.0, take_profit_pct=15)
    stops = np.full(12, 0.05)
    stops[3] = 0.01  # the signal candle says: tight stop for this trade
    stops[4] = 0.05  # the fill candle's value must be ignored
    r = run_backtest(s, np.array([3]), np.array([], dtype=int), g, 0, 12, COST, stops)
    assert r.trades[0].reason == "stop" and r.trades[0].exit_idx == 5
    fixed = run(s, [3], [], g)
    assert fixed.trades[0].reason == "end"  # the 5% fixed stop survives the dip
