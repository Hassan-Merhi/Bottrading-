"""RiskGuard: the only code allowed to place, cancel or change exchange orders.

Section 6 of the blueprint, enforced here:

* 6.1 Every order goes through `RiskGuard.place_order()`, which checks every rule
  first. Refusals are logged and alerted. Other modules only ever get a
  `ReadOnlyExchange`, which cannot place orders. A test scans the code base
  for order calls anywhere else.
* 6.2 PAPER unless MODE=LIVE env + `live_enabled: true` + LIVE_CONFIRMED.txt
  holding the owner's typed capital limit. Anything missing or unclear: PAPER.
* 6.3 Money limits, with a hard ceiling of $100 that config cannot raise.
  Daily loss limit halts live trading until the next UTC day. The total
  drawdown kill closes everything, cancels orders, revokes the confirmation
  and never restarts by itself.
* 6.4 Spot, long only, whitelisted pairs, one position per bot, and a stop-loss
  on the exchange right after every live entry (or the position is closed).
* 6.5 House money: handled by the ledger on every sell.
* 6.7 Exchange errors halt new entries. Exits are always allowed: a lock never
  stops the system from reducing risk.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bots.config import PROJECT_DIR
from bots.live.reconcile import ReconciliationError, reconcile_live_orders
from bots.risk.ledger import DAY_MS, Ledger
from bots.risk.state import (CRASH_LOCK, DAILY_HALT_UNTIL, ENTRY_HALT, KILL_SWITCH, LIVE_EPOCH,
                             LIVE_RECON_HALT, OWNER_PAUSE,
                             RiskState, now_ms)

HARD_CAPITAL_CEILING_USD = 100.0
CONFIRM_FILE = "LIVE_CONFIRMED.txt"
ORDER_TYPES = ("market", "stop_loss", "limit")  # limit: resting buy entries only
SIDES = ("buy", "sell")
REQUIRED_RISK_KEYS = ("max_total_capital_usd", "max_per_bot_usd", "max_live_bots",
                      "daily_loss_limit_usd", "total_drawdown_kill_usd")
# Exchange methods a read-only view may use. Everything else is refused.
READ_ONLY_PREFIXES = ("fetch_", "load_markets")
READ_ONLY_NAMES = {"id", "markets", "market", "milliseconds", "amount_to_precision",
                   "price_to_precision", "has", "timeframes", "symbols"}


class OrderRefused(Exception):
    """A risk rule said no. Nothing was sent to the exchange."""


class OrderFailed(Exception):
    """The exchange rejected or errored on an order that passed the rules."""


class ReadOnlyExchange:
    """Market-data view of an exchange. Order methods do not exist here."""

    def __init__(self, exchange):
        object.__setattr__(self, "_exchange", exchange)

    def __getattr__(self, name: str):
        if name.startswith(READ_ONLY_PREFIXES) or name in READ_ONLY_NAMES:
            return getattr(self._exchange, name)
        raise PermissionError(f"{name} is not available outside RiskGuard")

    def __setattr__(self, name, value):
        raise PermissionError("read-only exchange")


@dataclass(frozen=True)
class OrderRequest:
    bot_id: str
    mode: str                   # paper | live
    pair: str
    side: str                   # buy | sell
    amount: float               # base currency quantity
    order_type: str = "market"  # market | stop_loss | limit
    price: float | None = None  # limit price (limit orders only)
    stop_price: float | None = None
    ref_price: float | None = None  # expected price, used for cost checks on buys
    reason: str = ""
    params: dict = field(default_factory=dict)  # any exchange flag here is refused


@dataclass
class ModeDecision:
    mode: str                   # PAPER | LIVE
    reasons: list[str]          # why it is not LIVE (empty when LIVE)


def risk_config_problems(cfg: dict) -> list[str]:
    risk = cfg.get("risk") or {}
    problems = []
    for key in REQUIRED_RISK_KEYS:
        v = risk.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            problems.append(f"risk.{key} missing or invalid")
    if problems:
        return problems
    if risk["max_total_capital_usd"] > HARD_CAPITAL_CEILING_USD:
        problems.append(f"max_total_capital_usd above the ${HARD_CAPITAL_CEILING_USD:.0f} ceiling")
    if 0 < risk["max_total_capital_usd"] < risk["max_per_bot_usd"]:
        problems.append("max_per_bot_usd above max_total_capital_usd")
    return problems


def read_confirmed_capital(path: Path) -> float | None:
    """The number the owner typed into LIVE_CONFIRMED.txt, or None if unclear."""
    try:
        text = path.read_text().strip().replace("$", "").replace(",", "")
        value = float(text)
    except (OSError, ValueError):
        return None
    return value if math.isfinite(value) else None


def resolve_mode(cfg: dict, env: dict, confirm_path: Path, state: RiskState,
                 live_exchange_ready: bool) -> ModeDecision:
    reasons = []
    if str(env.get("MODE", "")).strip().upper() != "LIVE":
        reasons.append("MODE env is not LIVE")
    if cfg.get("live_enabled") is not True:
        reasons.append("live_enabled is not true in config")
    problems = risk_config_problems(cfg)
    reasons += problems
    confirmed = read_confirmed_capital(confirm_path)
    if confirmed is None:
        reasons.append(f"{CONFIRM_FILE} missing or unreadable")
    elif not problems:
        cap = float(cfg["risk"]["max_total_capital_usd"])
        if cap <= 0:
            reasons.append("max_total_capital_usd is 0 (paper only)")
        elif abs(confirmed - cap) > 1e-9:
            reasons.append(f"{CONFIRM_FILE} says {confirmed:g}, config says {cap:g}")
    if state.is_set(KILL_SWITCH):
        reasons.append(f"kill switch active: {state.get(KILL_SWITCH)}")
    if state.is_set(CRASH_LOCK):
        reasons.append(f"waiting for owner after unclean restart: {state.get(CRASH_LOCK)}")
    if state.is_set(LIVE_RECON_HALT):
        reasons.append(f"live exchange reconciliation locked: {state.get(LIVE_RECON_HALT)}")
    if not live_exchange_ready:
        reasons.append("no verified live exchange connection")
    return ModeDecision("LIVE" if not reasons else "PAPER", reasons)


def confirm_file_path(env: dict) -> Path:
    """LIVE_CONFIRMED.txt lives in BOTS_STATE_DIR (a persistent disk when deployed)."""
    return Path(env.get("BOTS_STATE_DIR") or PROJECT_DIR) / CONFIRM_FILE


class NullAlerter:
    def alert(self, text: str, kind: str = "info", buttons: list | None = None) -> None:
        print(f"[ALERT:{kind}] {text}")

    def send_file(self, path, caption: str = "") -> bool:
        print(f"[FILE] {path}: {caption}")
        return False


class RiskGuard:
    def __init__(self, cfg: dict, conn, paper_exchange, live_exchange=None, alerter=None,
                 env: dict | None = None, confirm_path: Path | None = None,
                 clock: Callable[[], int] = now_ms, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.conn = conn
        self.state = RiskState(conn)
        self.ledger = Ledger(conn, cfg["risk"].get("house_money_multiple", 2))
        self._paper = paper_exchange
        self._live = live_exchange
        self.alerter = alerter or NullAlerter()
        self.env = env if env is not None else dict(os.environ)
        self.confirm_path = confirm_path or confirm_file_path(self.env)
        self.clock = clock
        self.sleep = sleep
        self.marks: dict[str, float] = {}
        self._healthy_ticks = 0
        bt = cfg["backtest"]
        self.fee = bt["taker_fee_pct"] / 100
        self.maker_fee = bt.get("maker_fee_pct", bt["taker_fee_pct"]) / 100
        self.slippage = bt["slippage_pct"] / 100

    # -- views -------------------------------------------------------------

    def mode(self) -> ModeDecision:
        return resolve_mode(self.cfg, self.env, self.confirm_path, self.state,
                            self._live is not None)

    def read_only(self, mode: str) -> ReadOnlyExchange | None:
        ex = self._live if mode == "live" else self._paper
        return ReadOnlyExchange(ex) if ex is not None else None

    @property
    def has_live_exchange(self) -> bool:
        return self._live is not None

    def verify_live_orders(self) -> dict:
        """Live preflight: remote exposure must match local protected orders.

        A mismatch is sticky and owner-reviewed. A subsequent successful
        query alone MUST NOT silently clear a previous reconciliation lock.
        """
        try:
            result = reconcile_live_orders(self._live, self.ledger,
                                           self.cfg["data"]["pairs"])
        except ReconciliationError as exc:
            self.state.set(LIVE_RECON_HALT, str(exc))
            self._event("live_reconciliation_halt",
                        "Live entries locked: " + str(exc), kind="critical")
            raise OrderRefused("live exchange reconciliation failed: " + str(exc)) from exc
        return result

    def live_epoch(self) -> int:
        return int(self.state.get(LIVE_EPOCH, "1"))

    def update_marks(self, prices: dict[str, float]) -> None:
        self.marks.update(prices)

    # -- the gate ----------------------------------------------------------

    def place_order(self, req: OrderRequest) -> dict:
        """The only way to send an order. Raises OrderRefused or OrderFailed."""
        try:
            self._check(req)
        except OrderRefused as e:
            self._event("order_refused", f"{req.bot_id} {req.mode} {req.side} {req.pair}: {e}")
            raise
        exchange = self._exchange(req.mode)
        try:
            if req.order_type == "stop_loss":
                order = exchange.create_order(req.pair, "market", "sell", req.amount, None,
                                              {"stopLossPrice": req.stop_price})
            elif req.order_type == "limit":
                order = exchange.create_order(req.pair, "limit", "buy", req.amount, req.price)
            else:
                order = exchange.create_order(req.pair, "market", req.side, req.amount)
        except Exception as e:  # noqa: BLE001 - any exchange failure is a fail-safe event
            self.halt_entries(f"exchange error on {req.side} {req.pair}: {e}")
            raise OrderFailed(str(e)) from e
        return order

    def _exchange(self, mode: str):
        ex = self._live if mode == "live" else self._paper
        if ex is None:
            raise OrderRefused(f"no {mode} exchange")
        return ex

    def _check(self, req: OrderRequest) -> None:
        if req.params:
            raise OrderRefused(f"order flags are not allowed (spot, long only, no leverage or "
                               f"margin): {sorted(req.params)}")
        if req.side not in SIDES:
            raise OrderRefused(f"side {req.side!r} not allowed")
        if req.order_type not in ORDER_TYPES:
            raise OrderRefused(f"order type {req.order_type!r} not allowed")
        if req.mode not in ("paper", "live"):
            raise OrderRefused(f"mode {req.mode!r} not allowed")
        if not (isinstance(req.amount, (int, float)) and math.isfinite(req.amount)
                and req.amount > 0):
            raise OrderRefused("amount must be a positive number")
        if req.pair not in self.cfg["data"]["pairs"]:
            raise OrderRefused(f"{req.pair} is not on the liquid-pairs whitelist")
        acc = self.ledger.account(req.bot_id, req.mode)
        if acc is None:
            raise OrderRefused(f"no active {req.mode} account for {req.bot_id}")
        if req.mode == "live" and self._live is None:
            raise OrderRefused("no live exchange connection")

        if req.order_type == "stop_loss":
            if req.side != "sell" or not req.stop_price or req.stop_price <= 0:
                raise OrderRefused("a stop-loss must be a sell with a positive stop price")
            if acc["stop_order_id"]:
                raise OrderRefused("position already has a stop-loss")
        if req.order_type == "limit":
            if req.side != "buy" or not req.price or not math.isfinite(req.price) or req.price <= 0:
                raise OrderRefused("a limit order must be a buy with a positive price")
        if req.side == "sell":
            if req.amount > acc["qty"] * (1 + 1e-9) + 1e-12:
                raise OrderRefused(f"sell of {req.amount} exceeds holding {acc['qty']} "
                                   "(shorting is not allowed)")
            return  # reducing risk is always allowed
        self._check_buy(req, acc)

    def _check_buy(self, req: OrderRequest, acc) -> None:
        status = self._bot_status(req.bot_id)
        allowed = ("live",) if req.mode == "live" else ("paper", "ready_to_graduate", "approved")
        if status not in allowed:
            raise OrderRefused(f"bot status {status!r} may not open {req.mode} trades")
        if acc["qty"] > 0 or acc["stop_order_id"]:
            raise OrderRefused("bot already has an open position (one per bot)")
        if acc["pending_order_id"]:
            raise OrderRefused("bot already has an entry order resting (one per bot)")
        if self.state.is_set(ENTRY_HALT):
            raise OrderRefused(f"new entries halted: {self.state.get(ENTRY_HALT)}")
        if self.state.is_set(OWNER_PAUSE):
            raise OrderRefused(f"paused by the owner: {self.state.get(OWNER_PAUSE)}")
        if req.ref_price is None or not req.ref_price > 0:
            raise OrderRefused("buy needs a reference price for the cost check")
        # Reserve an executable ask rather than a mid-price for market buys.
        # Limit buys reserve the worst permitted limit price and taker fee.
        half_spread = float(self.cfg["backtest"].get("spread_bps", 0)) / 20_000
        execution_factor = (1.0 if req.order_type == "limit"
                            else 1.0 + self.slippage + half_spread)
        cost = req.amount * req.ref_price * execution_factor * (1 + self.fee)
        if cost > self.ledger.tradable_cash(acc) + 1e-9:
            raise OrderRefused(f"cost ${cost:.2f} exceeds tradable cash "
                               f"${self.ledger.tradable_cash(acc):.2f}")
        min_qty, min_cost = self._minimums(req.mode, req.pair)
        if req.amount < min_qty or cost < min_cost:
            raise OrderRefused(f"below exchange minimum ({min_qty} / ${min_cost})")
        if req.mode == "live":
            self._check_live_buy(acc, cost)

    def _check_live_buy(self, acc, cost: float) -> None:
        decision = self.mode()
        if decision.mode != "LIVE":
            raise OrderRefused("not in LIVE mode: " + "; ".join(decision.reasons))
        risk = self.cfg["risk"]
        halt = int(self.state.get(DAILY_HALT_UNTIL, "0"))
        if self.clock() < halt:
            raise OrderRefused("daily loss limit hit; live trading paused until next UTC day")
        if acc["epoch"] != self.live_epoch():
            raise OrderRefused("account belongs to an earlier live confirmation")
        if acc["allocation"] > risk["max_per_bot_usd"] + 1e-9:
            raise OrderRefused("bot allocation above max_per_bot_usd")
        live = self.ledger.active("live")
        if len(live) > risk["max_live_bots"]:
            raise OrderRefused("more live bots than max_live_bots")
        cap = min(float(risk["max_total_capital_usd"]), HARD_CAPITAL_CEILING_USD)
        if sum(a["allocation"] for a in live) > cap + 1e-9:
            raise OrderRefused("live allocations exceed max_total_capital_usd")
        daily, total = self.live_pnl()
        if daily <= -risk["daily_loss_limit_usd"]:
            raise OrderRefused("daily loss limit reached")
        if total <= -risk["total_drawdown_kill_usd"]:
            raise OrderRefused("total drawdown kill level reached")
        self.verify_live_orders()
        free = (self._live.fetch_balance().get("free") or {}).get("USD", 0.0) or 0.0
        if cost > free:
            raise OrderRefused(f"exchange free USD ${free:.2f} below cost ${cost:.2f}")

    def _minimums(self, mode: str, pair: str) -> tuple[float, float]:
        cfg_min = self.cfg["backtest"]["min_order"][pair]
        min_qty, min_cost = float(cfg_min["min_qty"]), float(cfg_min["min_cost_usd"])
        if mode == "live":
            try:
                limits = self._live.market(pair)["limits"]
                min_qty = max(min_qty, float(limits["amount"]["min"] or 0))
                min_cost = max(min_cost, float((limits.get("cost") or {}).get("min") or 0))
            except Exception:  # noqa: BLE001 - fall back to the stricter config values
                pass
        return min_qty, min_cost

    def _bot_status(self, bot_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM deployed_bots WHERE bot_id=?",
                                (bot_id,)).fetchone()
        return row[0] if row else None

    # -- composite actions -------------------------------------------------

    def enter_long(self, bot_id: str, mode: str, pair: str, ref_price: float,
                   size_pct: float, stop_pct: float, target_pct: float, reason: str,
                   limit_price: float | None = None) -> bool:
        """Open a long. Returns True when the position is open, False when a limit entry rests.

        With execution.entry_order = limit, the buy rests at `limit_price` (the bid, or
        ref_price) and pays the maker fee when it fills; sync_pending() finishes the
        entry on a later loop or cancels it after execution.limit_timeout_s. A market
        entry fills now. Either way the stop-loss goes on right after the fill, and a
        position whose stop cannot be placed is closed at once.
        """
        acc = self.ledger.account(bot_id, mode)
        if acc is None:
            raise OrderRefused(f"no active {mode} account for {bot_id}")
        execution = self.cfg.get("execution") or {}
        use_limit = execution.get("entry_order") == "limit"
        px = (limit_price or ref_price) if use_limit else ref_price
        budget = self.ledger.tradable_cash(acc) * size_pct / 100
        half_spread = float(self.cfg["backtest"].get("spread_bps", 0)) / 20_000
        execution_factor = 1.0 if use_limit else 1.0 + self.slippage + half_spread
        amount = budget / (px * execution_factor * (1 + self.fee))
        if mode == "live":
            amount = float(self._live.amount_to_precision(pair, amount))
        if use_limit:
            order = self.place_order(OrderRequest(bot_id, mode, pair, "buy", amount,
                                                  order_type="limit", price=px, ref_price=px,
                                                  reason=reason))
            if order.get("status") == "closed" and order.get("filled"):
                self._complete_entry(acc, bot_id, mode, pair, order, stop_pct, target_pct, reason)
                return True
            self.ledger.set_pending(acc["account_id"], str(order["id"]), px, amount, self.clock(),
                                    stop_pct, target_pct, reason)
            return False
        order = self.place_order(OrderRequest(bot_id, mode, pair, "buy", amount,
                                              ref_price=ref_price, reason=reason))
        self._settle(mode, order, pair, "buy")
        order = self._exchange(mode).fetch_order(order["id"], pair)
        self._complete_entry(acc, bot_id, mode, pair, order, stop_pct, target_pct, reason)
        return True

    def _complete_entry(self, acc, bot_id: str, mode: str, pair: str, order: dict,
                        stop_pct: float, target_pct: float, reason: str) -> None:
        """Record a filled buy and place its stop-loss. No stop = position closed at once."""
        qty, price = float(order["filled"]), float(order["average"])
        maker = order.get("type") == "limit"
        fee = self._fee_of(order, qty * price, self.maker_fee if maker else self.fee)
        stop, target = price * (1 - stop_pct / 100), price * (1 + target_pct / 100)
        acc = self.ledger.by_id(acc["account_id"])
        self.ledger.record_buy(acc, qty, price, fee, self.clock(), stop, target,
                               str(order.get("id")), reason)
        try:
            stop_order = self.place_order(OrderRequest(bot_id, mode, pair, "sell", qty,
                                                       order_type="stop_loss", stop_price=stop,
                                                       reason="protective stop"))
            self.ledger.set_stop_order(acc["account_id"], str(stop_order["id"]))
            if mode == "live":
                self.verify_protective_stop(bot_id)
        except (OrderRefused, OrderFailed) as e:
            self._event("stop_failed", f"{bot_id} {mode} {pair}: stop could not be placed ({e}); "
                                       "closing the position now", kind="critical")
            self.exit_long(bot_id, mode, "stop could not be placed")

    def verify_protective_stop(self, bot_id: str) -> bool:
        """Read-back check of independently resting Kraken stop, no trade actions."""
        acc = self.ledger.account(bot_id, "live")
        if acc is None or acc["qty"] <= 0:
            return True
        try:
            if not acc["stop_order_id"]:
                raise ValueError("missing stop order ID")
            order = self._live.fetch_order(acc["stop_order_id"], self._pair(bot_id))
            trigger = (order.get("stopLossPrice") or order.get("stopPrice")
                       or order.get("triggerPrice"))
            status = order.get("status")
            if (order.get("side") != "sell" or status not in ("open", "closed")
                    or not trigger or not math.isfinite(float(trigger))
                    or abs(float(trigger) - float(acc["stop_price"])) >
                    max(1e-6, abs(float(acc["stop_price"])) * 0.001)):
                raise ValueError("stop side, trigger or status cannot be verified")
            if status == "open":
                size = float(order.get("remaining") if order.get("remaining") is not None
                             else order.get("amount") or 0)
                if size + 1e-10 < acc["qty"]:
                    raise ValueError("protective stop covers less than current holding")
            return True
        except Exception as exc:
            self.state.set(LIVE_RECON_HALT, "protective stop not verified for " + bot_id)
            self._event("protective_stop_unverified",
                        f"{bot_id}: Kraken stop must be reconciled before live resumes",
                        kind="critical")
            return False

    def sync_pending(self, bot_id: str, mode: str) -> str:
        """Finish or cancel a resting limit entry. Returns filled | open | cancelled | none."""
        acc = self.ledger.account(bot_id, mode)
        if acc is None or not acc["pending_order_id"]:
            return "none"
        pair = self._pair(bot_id)
        ex = self._exchange(mode)
        order = ex.fetch_order(acc["pending_order_id"], pair)
        timeout_ms = float((self.cfg.get("execution") or {}).get("limit_timeout_s", 120)) * 1000
        if order.get("status") == "open" and self.clock() - int(acc["pending_ts"]) >= timeout_ms:
            self._cancel(mode, acc["pending_order_id"], pair)
            order = ex.fetch_order(acc["pending_order_id"], pair)
            if order.get("status") == "open":
                return "open"
        status = order.get("status")
        if status == "open":
            return "open"
        self.ledger.clear_pending(acc["account_id"])
        if order.get("filled"):  # full, or partial before a cancel
            self._complete_entry(acc, bot_id, mode, pair, order, acc["pending_stop_pct"],
                                 acc["pending_target_pct"], acc["pending_reason"])
            return "filled"
        return "cancelled"

    def cancel_pending(self, bot_id: str, mode: str, reason: str) -> None:
        """Withdraw a resting entry (exit signal, close-all, kill). A partial fill is kept."""
        acc = self.ledger.account(bot_id, mode)
        if acc is None or not acc["pending_order_id"]:
            return
        self._cancel(mode, acc["pending_order_id"], self._pair(bot_id))
        self._event("entry_cancelled", f"{bot_id} {mode}: resting entry withdrawn ({reason})",
                    kind="info")
        self.sync_pending(bot_id, mode)

    def exit_long(self, bot_id: str, mode: str, reason: str) -> float | None:
        """Cancel the stop and sell the whole position at market."""
        acc = self.ledger.account(bot_id, mode)
        if acc is not None and acc["pending_order_id"] and acc["qty"] <= 0:
            self.cancel_pending(bot_id, mode, reason)
            acc = self.ledger.account(bot_id, mode)
        if acc is None or acc["qty"] <= 0:
            return None
        pair = self._pair(bot_id)
        if acc["stop_order_id"]:
            pnl = self.sync_stop(bot_id, mode)
            if pnl is not None:
                return pnl  # the stop already sold the position
            self._cancel(mode, acc["stop_order_id"], pair)
            # The protective stop could fill during the cancellation request.
            # Verify its final state BEFORE placing another sell order.
            if mode == "live":
                confirmed = self._exchange(mode).fetch_order(acc["stop_order_id"], pair)
                if confirmed.get("status") == "closed":
                    return self.sync_stop(bot_id, mode)
                if confirmed.get("status") not in ("canceled", "expired", "rejected"):
                    self.state.set(LIVE_RECON_HALT, "uncertain stop cancellation for " + bot_id)
                    raise OrderFailed("stop cancellation remains unconfirmed")
            self.ledger.set_stop_order(acc["account_id"], None)
            acc = self.ledger.by_id(acc["account_id"])
        order = self.place_order(OrderRequest(bot_id, mode, pair, "sell", acc["qty"],
                                              reason=reason))
        qty, price, fee = self._settle(mode, order, pair, "sell")
        return self.ledger.record_sell(acc, qty, price, fee, self.clock(), reason,
                                       str(order.get("id")))

    def sync_stop(self, bot_id: str, mode: str) -> float | None:
        """Record a stop-loss that filled on the exchange. Returns the P&L if it did."""
        acc = self.ledger.account(bot_id, mode)
        if acc is None or not acc["stop_order_id"]:
            return None
        pair = self._pair(bot_id)
        order = self._exchange(mode).fetch_order(acc["stop_order_id"], pair)
        status = order.get("status")
        if status == "closed":
            qty = float(order.get("filled") or acc["qty"])
            price = float(order.get("average") or order.get("price") or acc["stop_price"])
            fee = self._fee_of(order, qty * price)
            return self.ledger.record_sell(acc, qty, price, fee, self.clock(), "stop",
                                           str(order.get("id")))
        if status in ("canceled", "expired", "rejected"):
            # The position is unprotected. Close it rather than trust it.
            self.ledger.set_stop_order(acc["account_id"], None)
            self._event("stop_missing", f"{bot_id} {mode}: stop order {status}; closing position",
                        kind="critical")
            return self.exit_long(bot_id, mode, f"stop order {status}")
        return None

    def _cancel(self, mode: str, order_id: str, pair: str) -> None:
        try:
            self._exchange(mode).cancel_order(order_id, pair)
        except Exception as e:  # noqa: BLE001
            self._event("cancel_failed", f"cancel {order_id} on {pair}: {e}",
                        kind="critical" if mode == "live" else "risk")
            if mode == "live":
                self.state.set(LIVE_RECON_HALT, "failed cancellation for " + pair)
                raise OrderFailed("cannot verify cancellation of live order") from e

    def _settle(self, mode: str, order: dict, pair: str, side: str) -> tuple[float, float, float]:
        """Wait for a market order to fill and return (qty, average price, fee)."""
        ex = self._exchange(mode)
        for _ in range(30):
            if order.get("status") == "closed" and order.get("filled"):
                break
            self.sleep(1)
            order = ex.fetch_order(order["id"], pair)
        else:
            self.halt_entries(f"{side} order {order.get('id')} on {pair} did not fill")
            raise OrderFailed(f"{side} order {order.get('id')} did not fill")
        qty, price = float(order["filled"]), float(order["average"])
        return qty, price, self._fee_of(order, qty * price)

    def _fee_of(self, order: dict, notional: float, rate: float | None = None) -> float:
        fee = order.get("fee") or {}
        if fee.get("cost") is not None:
            return float(fee["cost"])
        return notional * (self.fee if rate is None else rate)

    def _pair(self, bot_id: str) -> str:
        import json

        row = self.conn.execute("SELECT genome_json FROM deployed_bots WHERE bot_id=?",
                                (bot_id,)).fetchone()
        return json.loads(row[0])["pair"]

    # -- accounts ----------------------------------------------------------

    def open_paper_account(self, bot_id: str, price: float | None) -> int:
        return self.ledger.open_account(bot_id, "paper", float(self.cfg["paper"]["allocation_usd"]),
                                        0, self.clock(), price)

    def open_live_account(self, bot_id: str, price: float | None) -> int | None:
        """Give an approved bot real money, within every cap. None if not allowed now."""
        decision = self.mode()
        if decision.mode != "LIVE":
            return None
        try:
            self.verify_live_orders()
        except OrderRefused:
            return None
        risk = self.cfg["risk"]
        live = self.ledger.active("live")
        if len(live) >= risk["max_live_bots"]:
            return None
        cap = min(float(risk["max_total_capital_usd"]), HARD_CAPITAL_CEILING_USD)
        allocation = min(float(risk["max_per_bot_usd"]), cap - sum(a["allocation"] for a in live))
        min_cost = self._minimums("live", self._pair(bot_id))[1]
        if allocation < min_cost:
            return None
        return self.ledger.open_account(bot_id, "live", allocation, self.live_epoch(),
                                        self.clock(), price)

    def demote(self, bot_id: str, reason: str) -> None:
        """A live bot that broke its limits goes back to paper. It never gets more money."""
        self.exit_long(bot_id, "live", reason)
        acc = self.ledger.account(bot_id, "live")
        if acc is not None:
            self.ledger.close_account(acc["account_id"], self.clock())
        self._set_status(bot_id, "paper", reason)
        self._event("bot_demoted", f"{bot_id} returned to paper: {reason}")

    def _set_status(self, bot_id: str, status: str, reason: str) -> None:
        from datetime import datetime, timezone

        self.conn.execute("UPDATE deployed_bots SET status=?, status_reason=?, updated_at=? "
                          "WHERE bot_id=?",
                          (status, reason, datetime.now(timezone.utc).isoformat(), bot_id))
        self.conn.commit()

    # -- limits, halts and the kill switch --------------------------------

    def live_pnl(self) -> tuple[float, float]:
        """(today's live P&L, total live P&L this confirmation epoch), marked to market."""
        now = self.clock()
        today = now // DAY_MS
        daily = total = 0.0
        for acc in self.ledger.live_epoch_accounts(self.live_epoch()):
            if acc["active"]:
                pair = self._pair(acc["bot_id"])
                eq = self.ledger.equity(acc, self.marks.get(pair))
                if acc["day"] == today:
                    daily += eq - acc["day_start_equity"]
            else:
                eq = acc["last_equity"]
                if acc["closed_at"] and acc["closed_at"] // DAY_MS == today and acc["day"] == today:
                    daily += eq - acc["day_start_equity"]
            total += eq - acc["allocation"]
        return daily, total

    def check_limits(self) -> None:
        """Run after every trading loop. Enforces the daily loss limit and the kill switch."""
        if not self.ledger.live_epoch_accounts(self.live_epoch()):
            return
        risk = self.cfg["risk"]
        daily, total = self.live_pnl()
        if total <= -risk["total_drawdown_kill_usd"] and not self.state.is_set(KILL_SWITCH):
            self.kill(f"total live loss ${-total:.2f} reached ${risk['total_drawdown_kill_usd']}")
            return
        halt = int(self.state.get(DAILY_HALT_UNTIL, "0"))
        if daily <= -risk["daily_loss_limit_usd"] and self.clock() >= halt:
            next_day = (self.clock() // DAY_MS + 1) * DAY_MS
            self.state.set(DAILY_HALT_UNTIL, next_day)
            self._event("daily_loss_limit", f"live loss today ${-daily:.2f} hit the "
                        f"${risk['daily_loss_limit_usd']} limit; closing positions, live trading "
                        "paused until next UTC day", kind="critical")
            self.close_all("live", "daily loss limit")

    def kill(self, reason: str) -> None:
        """Close everything, cancel all orders, switch to PAPER. Never restarts by itself."""
        self.state.set(KILL_SWITCH, f"{reason} at {self.clock()}")
        if self.confirm_path.exists():
            self.confirm_path.rename(self.confirm_path.with_name(
                f"LIVE_CONFIRMED.revoked-{self.clock()}.txt"))
        self.close_all("live", f"kill switch: {reason}")
        for acc in self.ledger.active("live"):
            latest = self.ledger.by_id(acc["account_id"])
            # Do NOT erase ledger evidence of an unsettled live position.
            # An uncertain sell/stop must be reconciled and handled manually.
            if latest["qty"] > 0 or latest["pending_order_id"]:
                self.state.set(LIVE_RECON_HALT, "unsettled live exposure after emergency shutdown")
                self._event("live_exposure_unsettled",
                            f"{acc['bot_id']} still has an unsettled exchange position/order",
                            kind="critical")
                continue
            self.ledger.close_account(acc["account_id"], self.clock())
            self._set_status(acc["bot_id"], "paper", f"kill switch: {reason}")
        self._event("kill_switch", f"KILL SWITCH: {reason}. All live positions closed, orders "
                    "cancelled, mode is PAPER. Live trading stays off until the owner "
                    "re-confirms.", kind="critical")

    def close_all(self, mode: str, reason: str) -> None:
        for acc in self.ledger.active(mode):
            if acc["pending_order_id"]:
                try:
                    self.cancel_pending(acc["bot_id"], mode, reason)
                except (OrderRefused, OrderFailed) as e:
                    self._event("cancel_failed", f"could not withdraw {acc['bot_id']} {mode} "
                                                 f"entry: {e}", kind="critical")
                acc = self.ledger.by_id(acc["account_id"])
            if acc["qty"] > 0:
                try:
                    self.exit_long(acc["bot_id"], mode, reason)
                except (OrderRefused, OrderFailed) as e:
                    self._event("close_failed", f"could not close {acc['bot_id']} {mode}: {e}",
                                kind="critical")
        if mode == "live" and self._live is not None:
            for pair in self.cfg["data"]["pairs"]:
                try:
                    self._live.cancel_all_orders(pair)
                except Exception as e:  # noqa: BLE001
                    self._event("cancel_failed", f"cancel all on {pair}: {e}", kind="critical")

    def pause(self, who: str) -> None:
        """Owner's pause: no new entries (paper or live) until resume(). Exits still work."""
        if not self.state.is_set(OWNER_PAUSE):
            self.state.set(OWNER_PAUSE, f"{who} at {self.clock()}")
            self._event("owner_pause", f"New trades paused by {who}; exits and stops keep working",
                        kind="risk")

    def resume(self, who: str) -> None:
        if self.state.is_set(OWNER_PAUSE):
            self.state.clear(OWNER_PAUSE)
            self._event("owner_resume", f"New trades allowed again by {who}", kind="info")

    def halt_entries(self, reason: str) -> None:
        """Fail-safe: stop opening trades. Existing stop-losses stay on the exchange."""
        self._healthy_ticks = 0
        if not self.state.is_set(ENTRY_HALT):
            self.state.set(ENTRY_HALT, reason)
            self._event("entries_halted", f"New trades halted: {reason}", kind="critical")

    def note_healthy(self, needed: int) -> None:
        """A clean trading loop. Enough in a row lifts a fail-safe halt."""
        if not self.state.is_set(ENTRY_HALT):
            return
        self._healthy_ticks += 1
        if self._healthy_ticks >= needed:
            self.state.clear(ENTRY_HALT)
            self._healthy_ticks = 0
            self._event("entries_resumed", "Connection and data healthy again; new trades allowed")

    def _event(self, name: str, detail: str, kind: str = "risk") -> None:
        self.state.log(name, detail, self.clock())
        try:
            self.alerter.alert(detail, kind=kind)
        except Exception:  # noqa: BLE001 - alerts must never break risk handling
            pass
