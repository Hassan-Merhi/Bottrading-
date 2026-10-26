"""Long-only spot backtester with fees, slippage and exchange minimums.

Rules that keep it honest:

* No look-ahead. A signal at candle i (computed at its close) fills at the
  open of candle i + 1. With limit entries (CostModel.entry_order), the buy
  rests at that open and counts as filled only if the candle later trades
  below it (low < open); it then pays the maker fee and no slippage. A candle
  that never dips misses the entry, as a resting bid would.
* Every trade has a stop-loss and a take-profit. The stop is a fixed percent
  or, for ATR-mode genomes, a per-candle distance decided at the signal candle
  (`stop_pct`). Both are checked inside
  each candle using its low and high. If both could have hit in the same
  candle, the stop is assumed to have hit first. A stop gapped through at the
  open fills at the open, not at the stop price.
* Fees are charged on both fills, slippage moves both fills against the bot.
* Orders below the pair's minimum size or cost are refused, not resized.
* One open position at a time. No shorting, no leverage: a position never
  costs more than the cash the bot holds.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from bots.data.fetch import TIMEFRAME_MS
from bots.data.loader import Series
from bots.strategy.genome import Genome

YEAR_MS = 365.25 * 86_400_000


@dataclass(frozen=True)
class CostModel:
    fee: float              # taker fraction, e.g. 0.008
    slippage: float         # fraction per market fill
    starting_balance: float
    min_qty: float
    min_cost: float
    maker_fee: float | None = None   # fraction for a resting limit entry; None = same as fee
    entry_order: str = "market"      # market | limit

    @property
    def entry_fee(self) -> float:
        return self.maker_fee if (self.entry_order == "limit" and self.maker_fee is not None) \
            else self.fee

    @classmethod
    def from_config(cls, bt_cfg: dict, pair: str, execution: dict | None = None) -> "CostModel":
        mins = bt_cfg["min_order"][pair]
        execution = execution or {}
        return cls(fee=bt_cfg["taker_fee_pct"] / 100, slippage=bt_cfg["slippage_pct"] / 100,
                   starting_balance=float(bt_cfg["starting_balance_usd"]),
                   min_qty=float(mins["min_qty"]), min_cost=float(mins["min_cost_usd"]),
                   maker_fee=bt_cfg.get("maker_fee_pct", bt_cfg["taker_fee_pct"]) / 100,
                   entry_order=execution.get("entry_order", "market"))


@dataclass
class Trade:
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    qty: float
    fees: float
    pnl: float              # after fees and slippage
    reason: str             # stop | trail | target | time | signal | end


@dataclass
class Result:
    start: int
    end: int
    trades: list[Trade]
    equity: np.ndarray      # mark-to-market at each candle close, candles start..end-1
    rejected_orders: int
    metrics: dict = field(default_factory=dict)


def _first_true(mask: np.ndarray) -> int:
    """Index of the first True, or -1."""
    if mask.size == 0:
        return -1
    k = int(np.argmax(mask))
    return k if mask[k] else -1


def run_backtest(s: Series, entries: np.ndarray, exits: np.ndarray, g: Genome,
                 start: int, end: int, cost: CostModel,
                 stop_pct: np.ndarray | None = None) -> Result:
    """Simulate genome `g` on candles [start, end) of `s`.

    `entries` / `exits` are sorted candle indices where the signal fired.
    `stop_pct`, when given, is the stop distance (fraction of price) to use for a
    trade signalled at candle i; it is read at the signal candle, never later.
    """
    end = min(end, len(s))
    n = max(end - start, 0)
    equity = np.empty(n)
    trades: list[Trade] = []
    rejected = 0
    if n < 2:
        equity[:] = cost.starting_balance
        return _finish(Result(start, end, trades, equity, rejected), s, cost)

    o, h, l, c = s.open, s.high, s.low, s.close
    cash = cost.starting_balance
    size = g.position_size_pct / 100
    sl, tp = g.stop_loss_pct / 100, g.take_profit_pct / 100
    trail = g.trail_pct / 100
    hold_max = g.max_hold_candles  # 0 = no time exit
    per_day: dict[int, int] = {}
    filled_to = start          # equity is written for candles before this index
    earliest_fill = start + 1  # next candle an entry may fill on
    ptr = int(np.searchsorted(entries, start))

    while ptr < len(entries):
        i = int(entries[ptr])
        j = i + 1
        if j < earliest_fill:
            ptr = int(np.searchsorted(entries, earliest_fill - 1))
            continue
        if j >= end:
            break
        day = int(s.day[j])
        if per_day.get(day, 0) >= g.max_trades_per_day:
            ptr += 1
            continue

        if cost.entry_order == "limit":
            if not l[j] < o[j]:  # the bid resting at the open was never hit
                ptr += 1
                continue
            fill = o[j]
        else:
            fill = o[j] * (1 + cost.slippage)
        fee_rate = cost.entry_fee
        notional = min(cash * size, cash / (1 + fee_rate))
        qty = notional / fill
        if notional < cost.min_cost or qty < cost.min_qty:
            rejected += 1
            ptr += 1
            continue
        fee_in = notional * fee_rate
        equity[filled_to - start:j - start] = cash
        cash_before = cash
        cash -= notional + fee_in
        sl_i = float(stop_pct[i]) if stop_pct is not None else sl
        stop, target = fill * (1 - sl_i), fill * (1 + tp)

        e = int(np.searchsorted(exits, j))
        sig = int(exits[e]) if e < len(exits) else None
        k_time = j + hold_max if hold_max else None  # time exit fills at this candle's open
        scan_end = min(sig + 1, end) if sig is not None else end
        if k_time is not None:
            scan_end = min(scan_end, k_time)
        lows, highs = l[j:scan_end], h[j:scan_end]
        if trail > 0 and len(highs) > 1:
            # The trailing level at candle k uses highs up to k - 1: inside a candle the
            # order of high and low is unknown, so the stop may not chase the same candle.
            hh = np.maximum.accumulate(highs)
            level = np.empty_like(lows)
            level[0] = stop
            level[1:] = np.maximum(stop, hh[:-1] * (1 - trail))
        else:
            level = None
        k_sl = _first_true(lows <= (level if level is not None else stop))
        k_tp = _first_true(highs >= target)
        if k_sl >= 0 and (k_tp < 0 or k_sl <= k_tp):
            k = j + k_sl
            lvl = float(level[k_sl]) if level is not None else stop
            px = min(lvl, o[k]) if k > j else lvl
            reason = "trail" if lvl > stop else "stop"
        elif k_tp >= 0:
            k = j + k_tp
            px, reason = target, "target"
        elif k_time is not None and k_time < end and (sig is None or k_time <= sig + 1):
            k = k_time
            px, reason = o[k], "time"
        elif sig is not None and sig + 1 < end:
            k = sig + 1
            px, reason = o[k], "signal"
        else:
            k = end - 1
            px, reason = c[k], "end"

        exit_fill = px * (1 - cost.slippage)
        proceeds = qty * exit_fill
        fee_out = proceeds * cost.fee
        equity[j - start:k - start] = cash + qty * c[j:k]
        cash += proceeds - fee_out
        equity[k - start] = cash
        trades.append(Trade(j, k, fill, exit_fill, qty, fee_in + fee_out, cash - cash_before, reason))
        per_day[day] = per_day.get(day, 0) + 1
        filled_to = k + 1
        earliest_fill = k + 1
        ptr = int(np.searchsorted(entries, earliest_fill - 1))

    equity[filled_to - start:] = cash
    return _finish(Result(start, end, trades, equity, rejected), s, cost)


def hold_return(s: Series, start: int, end: int, cost: CostModel) -> float:
    """Return of buying at the first open and selling at the last close, after costs."""
    buy = s.open[start] * (1 + cost.slippage) * (1 + cost.fee)
    sell = s.close[end - 1] * (1 - cost.slippage) * (1 - cost.fee)
    return sell / buy - 1


def _finish(r: Result, s: Series, cost: CostModel) -> Result:
    eq = r.equity
    bars_per_year = YEAR_MS / TIMEFRAME_MS[s.timeframe]
    years = max(len(eq), 1) / bars_per_year
    total = eq[-1] / cost.starting_balance - 1 if len(eq) else 0.0
    if len(eq) > 1:
        rets = np.diff(eq) / eq[:-1]
        sd = rets.std()
        sharpe = float(rets.mean() / sd * np.sqrt(bars_per_year)) if sd > 0 else 0.0
        peak = np.maximum.accumulate(eq)
        max_dd = float(np.max(1 - eq / peak))
    else:
        sharpe, max_dd = 0.0, 0.0
    annual = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0
    calmar = annual / max(max_dd, 0.01)
    t = r.trades
    wins = sum(tr.pnl > 0 for tr in t)
    r.metrics = {
        "total_return": float(total),
        "annual_return": float(annual),
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "calmar": float(calmar),
        "n_trades": len(t),
        "win_rate": wins / len(t) if t else 0.0,
        "stop_outs": sum(tr.reason == "stop" for tr in t),
        "trail_outs": sum(tr.reason == "trail" for tr in t),
        "time_outs": sum(tr.reason == "time" for tr in t),
        "targets": sum(tr.reason == "target" for tr in t),
        "fees_paid": float(sum(tr.fees for tr in t)),
        "gross_pnl": float(sum(tr.pnl + tr.fees for tr in t)),
        "rejected_orders": r.rejected_orders,
        "exposure": float(sum(tr.exit_idx - tr.entry_idx + 1 for tr in t) / max(len(eq), 1)),
        "hold_return": float(hold_return(s, r.start, r.end, cost)) if r.end - r.start >= 2 else 0.0,
    }
    return r
