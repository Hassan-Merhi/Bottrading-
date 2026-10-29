"""Simulated exchange for paper trading: real prices, fake money.

It mirrors the slice of the ccxt interface RiskGuard uses, so paper and live
orders take exactly the same path through the guard. Market orders fill at the
latest price with slippage and the taker fee. Stop-losses rest until a price
update trades through them, like a stop-loss order resting on Kraken. Limit
buys rest until a price update trades at or below their price and then fill
there at the maker fee; a limit already below the market fills at once as a
taker, as it would on the exchange.
"""
from __future__ import annotations

import itertools
import uuid

from bots.risk.state import now_ms
from bots.execution.model import market_fill_price


class PaperExchange:
    id = "paper"

    def __init__(self, fee: float, slippage: float, clock=now_ms, maker_fee: float | None = None,
                 spread_bps: float = 0.0, limit_timeout_s: float = 120.0):
        self.fee = fee
        self.maker_fee = fee if maker_fee is None else maker_fee
        self.slippage = slippage
        self.spread_bps = spread_bps
        self.limit_timeout_ms = int(limit_timeout_s * 1000)
        self.clock = clock
        self.prices: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._session = uuid.uuid4().hex[:6]  # ids stay unique across restarts

    # -- market data -------------------------------------------------------

    def update_price(self, pair: str, price: float, low: float | None = None,
                     open_: float | None = None) -> None:
        """New price info. `low` is the lowest trade since the previous update."""
        self.prices[pair] = price
        low = price if low is None else min(low, price)
        for order in self.orders.values():
            if order["symbol"] != pair or order["status"] != "open":
                continue
            if order["type"] == "limit":
                # An expired order cannot fill when a delayed worker next polls.
                if self.clock() - order["timestamp"] >= self.limit_timeout_ms:
                    order["status"] = "canceled"
                    continue
                # OHLC lows may predate this two-minute order. A crossing
                # executable quote (after spread/slippage) is required.
                ask = market_fill_price(price, "buy", self.slippage, self.spread_bps)
                if ask <= order["price"]:
                    self._fill(order, order["price"], maker=True)
                continue
            stop = order["stopLossPrice"]
            if low <= stop:
                # A gap through the stop fills at the open, not at the stop.
                px = min(stop, open_) if open_ is not None else stop
                self._fill(order, px)

    def milliseconds(self) -> int:
        return self.clock()

    # -- orders (called only by RiskGuard) ---------------------------------

    def create_order(self, symbol: str, type: str, side: str, amount: float,
                     price: float | None = None, params: dict | None = None) -> dict:
        params = params or {}
        if type not in ("market", "limit") or side not in ("buy", "sell") \
                or set(params) - {"stopLossPrice"}:
            raise ValueError("paper exchange supports spot market/limit orders and stop-losses only")
        if type == "limit" and (side != "buy" or not price or price <= 0 or params):
            raise ValueError("paper exchange supports plain limit buys only")
        if symbol not in self.prices:
            raise RuntimeError(f"no price for {symbol} yet")
        oid = f"paper-{self._session}-{next(self._ids)}"
        order = {"id": oid, "symbol": symbol, "type": type, "side": side, "amount": amount,
                 "price": price, "status": "open", "filled": 0.0, "average": None, "fee": None,
                 "timestamp": self.clock()}
        self.orders[oid] = order
        if type == "limit":
            ask = market_fill_price(self.prices[symbol], "buy", self.slippage,
                                    self.spread_bps)
            if ask <= price:  # already marketable; otherwise rests on the book
                self._fill(order, self.prices[symbol])
        elif "stopLossPrice" in params:
            order["stopLossPrice"] = float(params["stopLossPrice"])
            if self.prices[symbol] <= order["stopLossPrice"]:
                self._fill(order, self.prices[symbol])
        else:
            self._fill(order, self.prices[symbol])
        return dict(order)

    def _fill(self, order: dict, px: float, maker: bool = False) -> None:
        if not maker:  # takers pay assumed half-spread and execution slippage
            px = market_fill_price(px, order["side"], self.slippage, self.spread_bps)
            if order["type"] == "limit" and px > order["price"]:
                raise ValueError("limit order cannot fill worse than its buy price")
        rate = self.maker_fee if maker else self.fee
        order.update(status="closed", filled=order["amount"], average=px,
                     fee={"cost": order["amount"] * px * rate, "currency": "USD"})

    def fetch_order(self, id: str, symbol: str | None = None) -> dict:
        return dict(self.orders[id])

    def cancel_order(self, id: str, symbol: str | None = None) -> dict:
        order = self.orders[id]
        if order["status"] == "open":
            order["status"] = "canceled"
        return dict(order)

    def cancel_all_orders(self, symbol: str | None = None) -> list:
        return [self.cancel_order(oid) for oid, o in self.orders.items()
                if o["status"] == "open" and (symbol is None or o["symbol"] == symbol)]
