"""Long-only spot backtester with fees, slippage and exchange minimums.

Rules that keep it honest:

* No look-ahead. A signal at candle i is known only after that close. Market
  entries execute at the next open. Limit buys use a prior-close bid proxy;
  a later OHLC low does not prove a fill during a two-minute timeout. An
  opening gap through the bid is charged as taker, and short-lived orders
  with no time-resolved evidence are treated as unfilled.
* Target and trailing exits are software-driven, not exchange-resident orders:
  they require a closed-candle signal and execute at the following open.
  Protective hard stops can execute intrabar and take priority when ambiguous.
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
from bots.execution.model import limit_fill_kind, market_fill_price
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
    spread_bps: float = 0.0           # assumed full bid/ask spread in basis points
    limit_timeout_s: float = 120.0
    limit_bid_offset_bps: float = 0.0  # prior-close proxy for a resting bid
    trade_through_bps: float = 0.0    # require strict low-through for observable maker fill

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
                   entry_order=execution.get("entry_order", "market"),
                   spread_bps=float(bt_cfg.get("spread_bps", 0)),
                   limit_timeout_s=float(execution.get("limit_timeout_s", 120)),
                   limit_bid_offset_bps=float(execution.get("limit_bid_offset_bps",
                                                          bt_cfg.get("spread_bps", 0) / 2)),
                   trade_through_bps=float(execution.get("limit_trade_through_bps", 0)))


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
    unfilled_limits: int = 0


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
    unfilled_limits = 0
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
            bid = c[i] * (1 - cost.limit_bid_offset_bps / 10_000.0)
            kind = limit_fill_kind(o[j], l[j], bid, TIMEFRAME_MS[s.timeframe],
                                   int(cost.limit_timeout_s * 1000),
                                   cost.trade_through_bps)
            if kind is None:
                unfilled_limits += 1
                ptr += 1
                continue
            # Limit orders never execute at a price worse than their limit.
            # A gap-open through a bid is conservatively charged as taker.
            fill = bid
            fee_rate = cost.fee if kind == "taker" else cost.entry_fee
        else:
            fill = market_fill_price(o[j], "buy", cost.slippage, cost.spread_bps)
            fee_rate = cost.fee
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
        k_time = j + hold_max if hold_max else None

        # Stops are the only exchange-resident exit simulated intrabar. All
        # software exits (target, trailing, signal, time) execute only after
        # their triggering information is actually available to the worker.
        stop_end = min(end, k_time) if k_time is not None else end
        if sig is not None:
            stop_end = min(stop_end, sig + 1)
        lows = l[j:stop_end]
        k_stop_offset = _first_true(lows <= stop)
        candidates: list[tuple[int, int, float, str]] = [
            (end - 1, 9, float(c[end - 1]), "end")]
        if k_stop_offset >= 0:
            ks = j + k_stop_offset
            level = min(stop, o[ks]) if ks > j else stop
            candidates.append((ks, 2, float(level), "stop"))
        if k_time is not None and k_time < end:
            candidates.append((k_time, 0, float(o[k_time]), "time"))
        if sig is not None and sig + 1 < end:
            candidates.append((sig + 1, 0, float(o[sig + 1]), "signal"))

        # An observed candle HIGH cannot trigger a software target; the
        # worker only observes quotes periodically. Require close >= target
        # and execute at the following open (including a possible gap).
        scan_end = min(end - 1, stop_end)
        if scan_end > j:
            close_segment = c[j:scan_end]
            target_offset = _first_true(close_segment >= target)
            if target_offset >= 0:
                kt = j + target_offset + 1
                candidates.append((kt, 0, float(o[kt]), "target"))

            if trail > 0 and len(close_segment) > 1:
                # Use only known closes, not an intrabar high whose timing
                # relative to a low is unknowable from OHLC.
                highs_seen = np.maximum.accumulate(close_segment)
                trail_levels = np.maximum(stop, highs_seen[:-1] * (1 - trail))
                trail_offset = _first_true(close_segment[1:] <= trail_levels)
                if trail_offset >= 0:
                    kt = j + trail_offset + 2
                    candidates.append((kt, 0, float(o[kt]), "trail"))
        k, _, px, reason = min(candidates, key=lambda item: (item[0], item[1]))
        exit_fill = market_fill_price(px, "sell", cost.slippage, cost.spread_bps)
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
    return _finish(Result(start, end, trades, equity, rejected, unfilled_limits=unfilled_limits),
                   s, cost)


def hold_return(s: Series, start: int, end: int, cost: CostModel) -> float:
    """Return of buying at the first open and selling at the last close, after costs."""
    buy = market_fill_price(s.open[start], "buy", cost.slippage, cost.spread_bps) * (1 + cost.fee)
    sell = market_fill_price(s.close[end - 1], "sell", cost.slippage,
                             cost.spread_bps) * (1 - cost.fee)
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
    gains = sum(max(tr.pnl, 0.0) for tr in t)
    losses = -sum(min(tr.pnl, 0.0) for tr in t)
    # Keep the metric JSON-safe and finite, even if every observed trade won.
    # Zero trades must never count as an infinite profit factor.
    profit_factor = min(gains / losses, 100.0) if losses > 0 else (
        100.0 if gains > 0 else 0.0)
    r.metrics = {
        "total_return": float(total),
        "annual_return": float(annual),
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "calmar": float(calmar),
        "n_trades": len(t),
        "win_rate": wins / len(t) if t else 0.0,
        "profit_factor": float(profit_factor),
        "stop_outs": sum(tr.reason == "stop" for tr in t),
        "trail_outs": sum(tr.reason == "trail" for tr in t),
        "time_outs": sum(tr.reason == "time" for tr in t),
        "targets": sum(tr.reason == "target" for tr in t),
        "fees_paid": float(sum(tr.fees for tr in t)),
        "gross_pnl": float(sum(tr.pnl + tr.fees for tr in t)),
        "rejected_orders": r.rejected_orders,
        "unfilled_limit_orders": r.unfilled_limits,
        "exposure": float(sum(tr.exit_idx - tr.entry_idx + 1 for tr in t) / max(len(eq), 1)),
        "hold_return": float(hold_return(s, r.start, r.end, cost)) if r.end - r.start >= 2 else 0.0,
    }
    return r
