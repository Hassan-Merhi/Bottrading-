"""Evaluate a genome across walk-forward windows and score it."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from bots.backtest.engine import CostModel, run_backtest
from bots.data.loader import Series
from bots.evolution.diagnosis import market_regime
from bots.strategy.context import MarketContext
from bots.strategy.genome import Genome
from bots.strategy.signals import SignalCache


@dataclass
class Evaluation:
    genome: Genome
    windows: list[dict]          # backtest metrics per window
    score: float                 # fitness before the graveyard penalty
    worst_window: int
    regime: str                  # market conditions in the worst window
    volatility: float

    @property
    def total_trades(self) -> int:
        return sum(w["n_trades"] for w in self.windows)

    @property
    def compounded_return(self) -> float:
        return float(np.prod([1 + w["total_return"] for w in self.windows]) - 1)

    @property
    def max_drawdown(self) -> float:
        return max(w["max_drawdown"] for w in self.windows)

    def summary(self) -> dict:
        return {
            "fitness": self.score,
            "return": self.compounded_return,
            "hold_return": float(np.prod([1 + w["hold_return"] for w in self.windows]) - 1),
            "max_drawdown": self.max_drawdown,
            "trades": self.total_trades,
            "sharpe_by_window": [round(w["sharpe"], 3) for w in self.windows],
            "regime": self.regime,
        }


def score_windows(windows: list[dict], fcfg: dict) -> float:
    """Risk-adjusted return across windows, minus penalties.

    Penalties: drawdown above the limit, too few trades, failing to beat holding.
    The consistency term rewards strategies that work in every window, not one
    lucky stretch.
    """
    metric = fcfg["metric"]
    per_window = np.array([w[metric] for w in windows], dtype=float)
    per_window = np.clip(per_window, -10, 10)
    score = per_window.mean() - fcfg["consistency_weight"] * per_window.std()

    dd = max(w["max_drawdown"] for w in windows)
    dd_limit = fcfg["max_drawdown_pct"] / 100
    if dd > dd_limit:
        score -= fcfg["drawdown_penalty"] * (dd - dd_limit)

    trades = sum(w["n_trades"] for w in windows)
    if trades < fcfg["min_trades"]:
        score -= fcfg["trades_penalty"] * (1 - trades / fcfg["min_trades"])

    failed_hold = np.mean([w["total_return"] <= w["hold_return"] for w in windows])
    score -= fcfg["hold_penalty"] * failed_hold
    return float(score)


class Evaluator:
    """Runs backtests for genomes over a fixed list of time windows (ms)."""

    def __init__(self, market: dict[tuple[str, str], Series], windows_ms: list[tuple[int, int]],
                 cfg: dict, ctx: MarketContext | None = None):
        self.market = market
        self.windows_ms = windows_ms
        self.cfg = cfg
        self.cache = SignalCache(market, ctx)

    def evaluate(self, g: Genome) -> Evaluation:
        s, entries, exits = self.cache.get(g)
        stops = self.cache.stops(g)
        cost = CostModel.from_config(self.cfg["backtest"], g.pair)
        windows = []
        bounds = []
        for start_ms, end_ms in self.windows_ms:
            a, b = s.index_of(start_ms), s.index_of(end_ms)
            windows.append(run_backtest(s, entries, exits, g, a, b, cost, stops).metrics)
            bounds.append((a, b))
        worst = int(np.argmin([w["total_return"] for w in windows]))
        regime, vol = market_regime(s, *bounds[worst])
        return Evaluation(g, windows, score_windows(windows, self.cfg["fitness"]), worst, regime, vol)
