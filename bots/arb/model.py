"""Pure arbitrage arithmetic. No I/O, no exchange calls, fully unit-tested.

Conventions: prices in quote currency (USD), fees and edges as fractions
(0.008 = 0.80%), quantities in base coin.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

HOURS_PER_YEAR = 365.25 * 24


def _finite_positive(*xs: float) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in xs)


def walk_book(levels: list, qty: float) -> float | None:
    """Average price paid/received for `qty` taken from `levels` [[price, size], ...].

    Levels must be in the order the exchange shows them: asks ascending for a
    buy, bids descending for a sell. Returns None when the book is too thin,
    rather than inventing liquidity that was not there.
    """
    if not _finite_positive(qty):
        raise ValueError("qty must be positive and finite")
    remaining, cost = qty, 0.0
    for level in levels:
        try:
            price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not _finite_positive(price) or not math.isfinite(size) or size <= 0:
            continue
        take = min(size, remaining)
        cost += take * price
        remaining -= take
        if remaining <= 1e-12:
            return cost / qty
    return None


@dataclass(frozen=True)
class CrossEdge:
    """One cross-venue opportunity for a fixed quantity, after all costs."""
    qty: float
    buy_price: float        # average ask walked on the buy venue, before slippage
    sell_price: float       # average bid walked on the sell venue, before slippage
    gross_pct: float        # (sell - buy) / buy, before fees and slippage
    net_usd: float          # what the two legs leave after fees and slippage
    net_pct: float          # net_usd / cost of the buy leg
    buy_fee_usd: float
    sell_fee_usd: float

    @property
    def profitable(self) -> bool:
        return self.net_usd > 0


def cross_edge(buy_price: float, sell_price: float, qty: float, buy_fee: float,
               sell_fee: float, slippage: float = 0.0) -> CrossEdge:
    """Buy `qty` at `buy_price` on venue A and sell it at `sell_price` on venue B.

    Assumes the coin is already held on B and the cash on A, so no transfer
    happens and both legs are taker market orders fired at once. Slippage is
    charged against both legs on top of the walked book price.
    """
    if not _finite_positive(buy_price, sell_price, qty):
        raise ValueError("prices and qty must be positive and finite")
    if not all(math.isfinite(f) and 0 <= f < 0.5 for f in (buy_fee, sell_fee, slippage)):
        raise ValueError("fees and slippage must be fractions in [0, 0.5)")
    paid = qty * buy_price * (1 + slippage)
    received = qty * sell_price * (1 - slippage)
    buy_fee_usd = paid * buy_fee
    sell_fee_usd = received * sell_fee
    net = received - sell_fee_usd - paid - buy_fee_usd
    return CrossEdge(qty=qty, buy_price=buy_price, sell_price=sell_price,
                     gross_pct=sell_price / buy_price - 1, net_usd=net,
                     net_pct=net / paid, buy_fee_usd=buy_fee_usd, sell_fee_usd=sell_fee_usd)


def breakeven_gross_pct(buy_fee: float, sell_fee: float, slippage: float = 0.0) -> float:
    """Smallest raw price gap between venues that leaves zero after costs.

    At Kraken Tier 1 (0.80%) against Coinbase (0.60%) with 0.05% slippage per
    leg this is about 1.5%; BTC and ETH almost never trade that far apart
    between two large USD venues, which is the whole story of why cross-venue
    arbitrage is dead at retail fees.
    """
    return (1 + slippage) * (1 + buy_fee) / ((1 - slippage) * (1 - sell_fee)) - 1


@dataclass(frozen=True)
class FundingCarry:
    rate: float                 # funding rate per interval as a fraction (+ = longs pay shorts)
    interval_hours: float
    annual_gross_pct: float     # rate compounded simply over a year
    round_trip_cost_pct: float  # open + close both legs, as a fraction of notional
    annual_net_pct: float       # gross minus the round trip amortised over hold_days
    hold_days: float
    breakeven_days: float | None  # days the position must be held to pay its own costs

    @property
    def worthwhile(self) -> bool:
        return self.annual_net_pct > 0


def funding_carry(rate: float, interval_hours: float, spot_fee: float, futures_fee: float,
                  slippage: float = 0.0, hold_days: float = 30.0) -> FundingCarry:
    """Long spot + short perpetual: collect `rate` every `interval_hours`.

    The hedge is delta-neutral so price moves cancel; the income is funding and
    the costs are four taker fills (open and close on each leg) plus slippage.
    Negative funding (shorts pay longs) makes the carry negative: the position
    would pay to exist.
    """
    if not _finite_positive(interval_hours, hold_days) or not math.isfinite(rate):
        raise ValueError("interval, hold_days must be positive; rate finite")
    if not all(math.isfinite(f) and 0 <= f < 0.5 for f in (spot_fee, futures_fee, slippage)):
        raise ValueError("fees and slippage must be fractions in [0, 0.5)")
    periods_per_year = HOURS_PER_YEAR / interval_hours
    annual_gross = rate * periods_per_year
    round_trip = 2 * (spot_fee + futures_fee) + 4 * slippage
    annual_net = annual_gross - round_trip * (365.25 / hold_days)
    daily_gross = annual_gross / 365.25
    breakeven = round_trip / daily_gross if daily_gross > 0 else None
    return FundingCarry(rate=rate, interval_hours=interval_hours, annual_gross_pct=annual_gross,
                        round_trip_cost_pct=round_trip, annual_net_pct=annual_net,
                        hold_days=hold_days, breakeven_days=breakeven)
