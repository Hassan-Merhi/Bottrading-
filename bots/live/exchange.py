"""Authenticated Kraken connection for live trading. Locked by default.

The client built here is handed straight to RiskGuard; no other module gets
it. Before that, `check_cannot_withdraw` proves the API key has no withdrawal
permission (section 6.6). If the key can withdraw, or the check is
inconclusive, the system refuses to run.

Create the Kraken API key with exactly these permissions:
  Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades,
  Create & Modify Orders, Cancel/Close Orders.
Leave every "Withdraw Funds" and "Deposit" permission OFF.
"""
from __future__ import annotations

import os

PERMISSION_DENIED_MARKERS = ("permission denied", "egeneral:permission", "invalid permissions")


class WithdrawalPermissionError(RuntimeError):
    """The API key can withdraw, or we could not prove it cannot."""


def has_api_keys(env: dict | None = None) -> bool:
    env = env if env is not None else os.environ
    return bool(env.get("KRAKEN_API_KEY") and env.get("KRAKEN_API_SECRET"))


def make_live_exchange(env: dict | None = None):
    import ccxt

    env = env if env is not None else os.environ
    return ccxt.kraken({"apiKey": env["KRAKEN_API_KEY"], "secret": env["KRAKEN_API_SECRET"],
                        "enableRateLimit": True})


def check_cannot_withdraw(exchange) -> None:
    """Raise unless Kraken refuses a withdrawal-permission query with this key.

    `WithdrawMethods` only lists withdrawal methods; it moves no money, but Kraken
    answers it only for keys holding the withdraw permission. A permission
    error is the answer we need. Success, or any other error, is a refusal to run.
    """
    try:
        exchange.privatePostWithdrawMethods({"asset": "USD"})
    except Exception as e:  # noqa: BLE001
        if any(m in str(e).lower() for m in PERMISSION_DENIED_MARKERS):
            return
        raise WithdrawalPermissionError(
            f"could not confirm the API key lacks withdrawal permission: {e}") from e
    raise WithdrawalPermissionError(
        "the Kraken API key HAS withdrawal permission. Create a key with query + trade "
        "permissions only.")


def check_can_query(exchange) -> None:
    """The key must at least read balances, or live trading cannot be checked."""
    exchange.fetch_balance()
