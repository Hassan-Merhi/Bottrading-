"""The bot genome: a small set of explainable strategy parameters.

Spot only, long only. There are deliberately no genes for shorting, margin,
leverage or futures.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, fields

PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD")
TIMEFRAMES = ("15m", "1h", "4h")
ENTRY_TYPES = ("ema_cross", "rsi_reversion", "breakout", "macd")

# name -> (low, high, is_integer)
NUMERIC_GENES: dict[str, tuple[float, float, bool]] = {
    "fast_period": (5, 50, True),
    "slow_period": (20, 200, True),
    "rsi_low": (15, 40, True),
    "rsi_high": (60, 85, True),
    "breakout_window": (10, 100, True),
    "stop_loss_pct": (0.5, 5.0, False),
    "take_profit_pct": (1.0, 15.0, False),
    "position_size_pct": (10.0, 100.0, False),
    "max_trades_per_day": (1, 10, True),
}
CATEGORICAL_GENES: dict[str, tuple[str, ...]] = {
    "pair": PAIRS,
    "timeframe": TIMEFRAMES,
    "entry_type": ENTRY_TYPES,
}
MIN_FAST_SLOW_GAP = 5


@dataclass(frozen=True)
class Genome:
    pair: str
    timeframe: str
    entry_type: str
    fast_period: int
    slow_period: int
    rsi_low: int
    rsi_high: int
    breakout_window: int
    stop_loss_pct: float
    take_profit_pct: float
    position_size_pct: float
    max_trades_per_day: int

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Genome":
        return repair({f.name: d[f.name] for f in fields(cls)})

    def hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    def describe(self) -> str:
        """One readable line, e.g. for the dashboard and reports."""
        rule = {
            "ema_cross": f"EMA {self.fast_period}/{self.slow_period} cross",
            "rsi_reversion": f"RSI rebound above {self.rsi_low}, exit above {self.rsi_high}",
            "breakout": f"{self.breakout_window}-candle breakout",
            "macd": f"MACD {self.fast_period}/{self.slow_period}/9 cross",
        }[self.entry_type]
        return (f"{self.pair} {self.timeframe} {rule}; stop {self.stop_loss_pct:.2f}%, "
                f"target {self.take_profit_pct:.2f}%, size {self.position_size_pct:.0f}%, "
                f"max {self.max_trades_per_day}/day")


def repair(values: dict) -> Genome:
    """Clip every gene into its allowed range and enforce fast < slow."""
    v = dict(values)
    for name, options in CATEGORICAL_GENES.items():
        if v[name] not in options:
            raise ValueError(f"{name}={v[name]!r} is not one of {options}")
    for name, (lo, hi, is_int) in NUMERIC_GENES.items():
        x = min(max(float(v[name]), lo), hi)
        v[name] = int(round(x)) if is_int else round(x, 3)
    if v["slow_period"] < v["fast_period"] + MIN_FAST_SLOW_GAP:
        v["slow_period"] = min(NUMERIC_GENES["slow_period"][1],
                               v["fast_period"] + MIN_FAST_SLOW_GAP)
        if v["slow_period"] < v["fast_period"] + MIN_FAST_SLOW_GAP:
            v["fast_period"] = v["slow_period"] - MIN_FAST_SLOW_GAP
    return Genome(**v)


def random_genome(rng: random.Random, pairs=PAIRS, timeframes=TIMEFRAMES) -> Genome:
    v: dict = {
        "pair": rng.choice(tuple(pairs)),
        "timeframe": rng.choice(tuple(timeframes)),
        "entry_type": rng.choice(ENTRY_TYPES),
    }
    for name, (lo, hi, is_int) in NUMERIC_GENES.items():
        v[name] = rng.randint(int(lo), int(hi)) if is_int else rng.uniform(lo, hi)
    return repair(v)


def mutate(g: Genome, rng: random.Random, rate: float = 0.3, scale: float = 0.10,
           categorical_rate: float = 0.05, pairs=PAIRS, timeframes=TIMEFRAMES) -> Genome:
    """Small random changes: numeric genes move by about ±scale of their value."""
    v = g.to_dict()
    options = {"pair": tuple(pairs), "timeframe": tuple(timeframes), "entry_type": ENTRY_TYPES}
    for name in CATEGORICAL_GENES:
        if rng.random() < categorical_rate:
            v[name] = rng.choice(options[name])
    for name, (lo, hi, is_int) in NUMERIC_GENES.items():
        if rng.random() < rate:
            step = v[name] * rng.uniform(-scale, scale)
            if is_int and abs(step) < 1:
                step = rng.choice((-1, 1))
            v[name] = v[name] + step
    return repair(v)


def crossover(a: Genome, b: Genome, rng: random.Random) -> Genome:
    """Uniform crossover: each gene comes from one parent at random."""
    da, db = a.to_dict(), b.to_dict()
    return repair({k: (da[k] if rng.random() < 0.5 else db[k]) for k in da})


def distance(a: Genome, b: Genome) -> float:
    """0 = identical, 1 = nothing in common."""
    da, db = a.to_dict(), b.to_dict()
    total = sum(da[k] != db[k] for k in CATEGORICAL_GENES)
    for name, (lo, hi, _) in NUMERIC_GENES.items():
        total += abs(da[name] - db[name]) / (hi - lo)
    return total / (len(CATEGORICAL_GENES) + len(NUMERIC_GENES))
