"""Read-only reconciliation of Kraken orders before allocating any live capital.

Fail closed if unknown exchange orders exist or the local ledger does not match
resting orders. This cannot prove fill/position accuracy without full trade
and asset reconciliation; that additional external check remains a Phase 7
pilot prerequisite. Never cancel or create orders from this module.
"""
from __future__ import annotations

import math

OPEN_STATUSES = {"open", "pending", "new", "partially_filled"}
CLOSED_STATUSES = {"closed", "canceled", "expired", "rejected"}


class ReconciliationError(RuntimeError):
    """Exchange state cannot be proved safe for new live exposure."""


def reconcile_live_orders(exchange, ledger, allowed_pairs: list[str]) -> dict:
    """Validate known live stop/entry IDs vs *all* remote open orders.

    An order that did not originate with our persisted live account is treated
    as unknown. It may be a manual Kraken order or a record lost after a crash;
    in either case it must be resolved before placing additional live buys.
    """
    if exchange is None:
        raise ReconciliationError("no authenticated exchange")
    try:
        raw = exchange.fetch_open_orders()
        if not isinstance(raw, list):
            raise ReconciliationError("exchange open-order response is not a list")
        remote = {}
        for order in raw:
            oid = str(order.get("id") or "")
            symbol = order.get("symbol")
            if not oid or not symbol or oid in remote:
                raise ReconciliationError("ambiguous or duplicate exchange open order")
            remote[oid] = order
        known = {}
        for acc in ledger.active("live"):
            if acc["qty"] < 0 or acc["qty"] and not acc["stop_order_id"]:
                raise ReconciliationError(f"{acc['bot_id']}: live position lacks a protective stop")
            for field in ("pending_order_id", "stop_order_id"):
                oid = acc[field]
                if oid:
                    if oid in known:
                        raise ReconciliationError(f"duplicate recorded live order {oid}")
                    known[oid] = (acc, field)
        unknown = set(remote) - set(known)
        if unknown:
            raise ReconciliationError(
                f"{len(unknown)} unrecognized open Kraken order(s); manual reconciliation required")
        for oid, (acc, field) in known.items():
            symbol = remote.get(oid, {}).get("symbol")
            if oid not in remote:
                # A completed stop is legitimately absent from open orders;
                # do NOT authorize new orders until worker settles that fill.
                raise ReconciliationError(f"{acc['bot_id']}: recorded {field} {oid} "
                                          "is absent from Kraken open orders")
            if symbol not in allowed_pairs:
                raise ReconciliationError(f"order {oid} uses non-whitelisted pair")
            order = remote[oid]
            if order.get("status") not in OPEN_STATUSES:
                raise ReconciliationError(f"{oid}: exchange order is not open")
            if field == "stop_order_id":
                if order.get("side") != "sell":
                    raise ReconciliationError(f"{oid}: protective stop is not a sell")
                amount = float(order.get("remaining") if order.get("remaining") is not None
                               else order.get("amount") or 0.0)
                if not math.isfinite(amount) or amount + 1e-10 < float(acc["qty"]):
                    raise ReconciliationError(f"{oid}: stop covers less than the live holding")
        # Require balance query to work before new capital is ever allocated.
        balance = exchange.fetch_balance()
        free = float((balance.get("free") or {}).get("USD") or 0.0)
        if not math.isfinite(free) or free < 0:
            raise ReconciliationError("invalid live available USD balance")
        return {"open_orders": len(remote), "known_orders": len(known),
                "active_live_accounts": len(ledger.active("live")), "free_usd": free}
    except ReconciliationError:
        raise
    except Exception as exc:
        raise ReconciliationError(f"Kraken reconciliation query failed: {type(exc).__name__}") from exc
