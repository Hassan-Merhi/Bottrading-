"""Simulated exchange for paper trading: real prices, fake money.

It mirrors the slice of the ccxt interface RiskGuard uses, so paper and live
orders take exactly the same path through the guard. Market orders fill at the
latest price with slippage and the taker fee. Stop-losses rest until a price
update trades through them, like a stop-loss order resting on Kraken.
"""
from __future__ import annotations

import itertools
import uuid

from bots.risk.state import now_ms


class PaperExchange:
    id = "paper"

    def __init__(self, fee: float, slippage: float, clock=now_ms):
        self.fee = fee
        self.slippage = slippage
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
        if type != "market" or side not in ("buy", "sell") or set(params) - {"stopLossPrice"}:
            raise ValueError("paper exchange supports spot market orders and stop-losses only")
        if symbol not in self.prices:
            raise RuntimeError(f"no price for {symbol} yet")
        oid = f"paper-{self._session}-{next(self._ids)}"
        order = {"id": oid, "symbol": symbol, "type": type, "side": side, "amount": amount,
                 "status": "open", "filled": 0.0, "average": None, "fee": None,
                 "timestamp": self.clock()}
        self.orders[oid] = order
        if "stopLossPrice" in params:
            order["stopLossPrice"] = float(params["stopLossPrice"])
            if self.prices[symbol] <= order["stopLossPrice"]:
                self._fill(order, self.prices[symbol])
        else:
            self._fill(order, self.prices[symbol])
        return dict(order)

    def _fill(self, order: dict, px: float) -> None:
        px = px * (1 + self.slippage) if order["side"] == "buy" else px * (1 - self.slippage)
        order.update(status="closed", filled=order["amount"], average=px,
                     fee={"cost": order["amount"] * px * self.fee, "currency": "USD"})

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
