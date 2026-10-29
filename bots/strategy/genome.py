"""The bot genome: a small set of explainable strategy parameters.

Spot only, long only. There are deliberately no genes for shorting, margin,
leverage or futures.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, fields

PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "LINK/USD", "ADA/USD", "DOGE/USD",
         "NEAR/USD", "LTC/USD", "SUI/USD", "AAVE/USD", "UNI/USD", "AVAX/USD", "XLM/USD")
TIMEFRAMES = ("15m", "1h", "4h")
ENTRY_TYPES = ("ema_cross", "rsi_reversion", "breakout", "macd", "volume_spike",
               "squeeze_breakout")
TREND_FILTERS = ("none", "own_daily", "btc_daily")
STOP_MODES = ("fixed", "atr")  # fixed: stop_loss_pct below entry; atr: atr_mult x ATR(atr_period)

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
    "vol_mult": (1.5, 5.0, False),       # volume_spike: volume vs its average
    "trend_period": (20, 200, True),     # trend filter: days in the daily moving average
    "mood_min": (0, 90, True),           # Fear & Greed window for entries;
    "mood_max": (10, 100, True),         # 0..100 means the mood filter is off
    "atr_period": (7, 30, True),         # candles in the Average True Range
    "atr_mult": (1.0, 4.0, False),       # stop distance = atr_mult x ATR, when stop_mode is atr
    "trail_pct": (0.0, 5.0, False),      # trailing stop below the highest price since entry; 0 = off
    "max_hold_candles": (0, 200, True),  # exit after this many candles in a trade; 0 = off
}
CATEGORICAL_GENES: dict[str, tuple[str, ...]] = {
    "pair": PAIRS,
    "timeframe": TIMEFRAMES,
    "entry_type": ENTRY_TYPES,
    "trend_filter": TREND_FILTERS,
    "stop_mode": STOP_MODES,
}
# Where evolution searches. Narrower than NUMERIC_GENES, which stays wide so stored
# genomes load unchanged. A round trip costs about 1.8% at Kraken's Tier 1 taker
# fee plus spread and slippage, so targets under ~3% or stops under ~1.5% lose
# by construction; and below ~30% of a $20 allocation an order misses the $5
# exchange minimum after the first small loss.
SEARCH_FLOORS = {"stop_loss_pct": 1.5, "take_profit_pct": 3.0, "position_size_pct": 30.0}
# Newcomers and timeframe mutations favour slower candles, where moves are large
# enough to clear fees. 15m stays possible, just rarer.
TIMEFRAME_WEIGHTS = {"15m": 1.0, "1h": 2.0, "4h": 2.0}
MIN_FAST_SLOW_GAP = 5
MIN_MOOD_SPAN = 10
# Values for genes added after the first release, so stored genomes still load.
DEFAULTS = {"vol_mult": 2.0, "trend_period": 50, "mood_min": 0, "mood_max": 100,
            "trend_filter": "none", "stop_mode": "fixed", "atr_period": 14, "atr_mult": 2.0,
            "trail_pct": 0.0, "max_hold_candles": 0}


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
    vol_mult: float = 2.0
    trend_period: int = 50
    mood_min: int = 0
    mood_max: int = 100
    trend_filter: str = "none"
    stop_mode: str = "fixed"
    atr_period: int = 14
    atr_mult: float = 2.0
    trail_pct: float = 0.0
    max_hold_candles: int = 0

    @property
    def mood_filter_on(self) -> bool:
        return self.mood_min > 0 or self.mood_max < 100

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Genome":
        return repair({f.name: d.get(f.name, DEFAULTS.get(f.name)) for f in fields(cls)})

    def hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    def describe(self) -> str:
        """One readable line, e.g. for the dashboard and reports."""
        rule = {
            "ema_cross": f"EMA {self.fast_period}/{self.slow_period} cross",
            "rsi_reversion": f"RSI rebound above {self.rsi_low}, exit above {self.rsi_high}",
            "breakout": f"{self.breakout_window}-candle breakout",
            "macd": f"MACD {self.fast_period}/{self.slow_period}/9 cross",
            "volume_spike": f"volume {self.vol_mult:.1f}x spike + {self.fast_period}-candle high",
            "squeeze_breakout": f"{self.breakout_window}-candle volatility squeeze breakout",
        }[self.entry_type]
        filters = ""
        if self.trend_filter != "none":
            ref = self.pair.split("/")[0] if self.trend_filter == "own_daily" else "BTC"
            filters += f"; only when {ref} > {self.trend_period}-day average"
        if self.mood_filter_on:
            filters += f"; only when Fear & Greed is {self.mood_min}-{self.mood_max}"
        if self.stop_mode == "atr":
            lo, hi, _ = NUMERIC_GENES["stop_loss_pct"]
            stop = f"stop {self.atr_mult:.1f}x ATR({self.atr_period}), {lo:g}-{hi:g}%"
        else:
            stop = f"stop {self.stop_loss_pct:.2f}%"
        if self.trail_pct > 0:
            stop += f", trailing {self.trail_pct:.1f}%"
        hold = f", exit after {self.max_hold_candles} candles" if self.max_hold_candles else ""
        return (f"{self.pair} {self.timeframe} {rule}{filters}; {stop}, "
                f"target {self.take_profit_pct:.2f}%{hold}, size {self.position_size_pct:.0f}%, "
                f"max {self.max_trades_per_day}/day")


def repair(values: dict) -> Genome:
    """Clip every gene into its allowed range and enforce fast < slow."""
    v = {**DEFAULTS, **values}
    for name, options in CATEGORICAL_GENES.items():
        if v[name] not in options:
            raise ValueError(f"{name}={v[name]!r} is not one of {options}")
    for name, (lo, hi, is_int) in NUMERIC_GENES.items():
        x = min(max(float(v[name]), lo), hi)
        v[name] = int(round(x)) if is_int else round(x, 3)
    if v["mood_max"] < v["mood_min"] + MIN_MOOD_SPAN:
        v["mood_max"] = min(100, v["mood_min"] + MIN_MOOD_SPAN)
        v["mood_min"] = min(v["mood_min"], v["mood_max"] - MIN_MOOD_SPAN)
    if v["slow_period"] < v["fast_period"] + MIN_FAST_SLOW_GAP:
        v["slow_period"] = min(NUMERIC_GENES["slow_period"][1],
                               v["fast_period"] + MIN_FAST_SLOW_GAP)
        if v["slow_period"] < v["fast_period"] + MIN_FAST_SLOW_GAP:
            v["fast_period"] = v["slow_period"] - MIN_FAST_SLOW_GAP
    return Genome(**v)


def _pick_timeframe(rng: random.Random, timeframes) -> str:
    options = tuple(timeframes)
    return rng.choices(options, [TIMEFRAME_WEIGHTS.get(tf, 1.0) for tf in options])[0]


def _apply_search_floors(v: dict, size_floors: dict[str, float] | None = None) -> dict:
    """Raise genes to their search floors. `size_floors` maps a pair to the
    position size (% of the allocation) its exchange minimum order needs."""
    for name, floor in SEARCH_FLOORS.items():
        v[name] = max(v[name], floor)
    if size_floors:
        v["position_size_pct"] = max(v["position_size_pct"], size_floors.get(v["pair"], 0.0))
    return v


def random_genome(rng: random.Random, pairs=PAIRS, timeframes=TIMEFRAMES,
                  size_floors: dict[str, float] | None = None) -> Genome:
    v: dict = {
        "pair": rng.choice(tuple(pairs)),
        "timeframe": _pick_timeframe(rng, timeframes),
        "entry_type": rng.choice(ENTRY_TYPES),
        "trend_filter": rng.choice(TREND_FILTERS),
        "stop_mode": rng.choice(STOP_MODES),
    }
    for name, (lo, hi, is_int) in NUMERIC_GENES.items():
        lo = SEARCH_FLOORS.get(name, lo)
        v[name] = rng.randint(int(lo), int(hi)) if is_int else rng.uniform(lo, hi)
    if rng.random() < 0.5:  # half the newcomers ignore market mood
        v["mood_min"], v["mood_max"] = 0, 100
    if rng.random() < 0.5:  # half have no trailing stop
        v["trail_pct"] = 0.0
    if rng.random() < 0.5:  # half hold until stop, target or exit signal
        v["max_hold_candles"] = 0
    return repair(_apply_search_floors(v, size_floors))


def mutate(g: Genome, rng: random.Random, rate: float = 0.3, scale: float = 0.10,
           categorical_rate: float = 0.05, pairs=PAIRS, timeframes=TIMEFRAMES,
           size_floors: dict[str, float] | None = None) -> Genome:
    """Small random changes: numeric genes move by about ±scale of their value."""
    v = g.to_dict()
    options = {"pair": tuple(pairs), "timeframe": tuple(timeframes), "entry_type": ENTRY_TYPES,
               "trend_filter": TREND_FILTERS, "stop_mode": STOP_MODES}
    for name in CATEGORICAL_GENES:
        if rng.random() < categorical_rate:
            v[name] = (_pick_timeframe(rng, timeframes) if name == "timeframe"
                       else rng.choice(options[name]))
    for name, (lo, hi, is_int) in NUMERIC_GENES.items():
        if rng.random() < rate:
            step = v[name] * rng.uniform(-scale, scale)
            if v[name] == 0:  # a switched-off gene can switch on by a small amount
                step = rng.uniform(0, scale) * (hi - lo)
            if is_int and abs(step) < 1:
                step = rng.choice((-1, 1))
            v[name] = v[name] + step
    return repair(_apply_search_floors(v, size_floors))


def crossover(a: Genome, b: Genome, rng: random.Random) -> Genome:
    """Uniform crossover: each gene comes from one parent at random."""
    da, db = a.to_dict(), b.to_dict()
    return repair({k: (da[k] if rng.random() < 0.5 else db[k]) for k in da})


ATR_GENES = ("atr_period", "atr_mult")   # only meaningful when stop_mode is "atr"
CATEGORICAL_WEIGHT = 2.0  # a different coin, timeframe, rule or stop type is a different bot


def distance(a: Genome, b: Genome) -> float:
    """0 = identical, 1 = nothing in common.

    Categorical genes count double, and the ATR genes are ignored while both
    bots use a fixed stop, so unused genes cannot make two bots look alike.
    """
    da, db = a.to_dict(), b.to_dict()
    total = CATEGORICAL_WEIGHT * sum(da[k] != db[k] for k in CATEGORICAL_GENES)
    numeric = [n for n in NUMERIC_GENES
               if n not in ATR_GENES or "atr" in (a.stop_mode, b.stop_mode)]
    for name in numeric:
        lo, hi, _ = NUMERIC_GENES[name]
        total += abs(da[name] - db[name]) / (hi - lo)
    return total / (CATEGORICAL_WEIGHT * len(CATEGORICAL_GENES) + len(numeric))
