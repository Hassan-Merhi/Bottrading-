"""Live cross-venue arbitrage. Locked by default; every gate must pass on every trade.

Real money moves only when ALL of these hold at the moment of the trade:

* env MODE=LIVE, `arbitrage.live.enabled: true`, and ARB_LIVE_CONFIRMED.txt in
  the state directory containing exactly `arbitrage.live.max_total_capital_usd`
  typed by the owner. Missing or unclear anything: no trade.
* authenticated clients for every venue in the edge, built from API keys with
  trade permission only (the Kraken key is proven unable to withdraw).
* paper evidence: the scanner has run for `min_paper_days`, the paper account
  has closed `min_paper_trades` arbitrages and is ahead of holding.
* no global lock (kill switch, crash lock, entry halt, owner pause) and no
  arbitrage halt from an earlier unequal fill or loss limit.
* the edge, recomputed from this scan's live books at the live size, clears
  `min_net_edge_pct` after both fees and slippage, and the real free balances
  on both venues cover both legs.
* today's live trades and losses are within their limits.

Both legs are immediate-or-cancel limit orders at the walked book price plus
the slippage allowance, so neither can fill worse than the edge assumed. If
the two fills do not match, the module records the exposure, halts itself and
alerts the owner. It never unwinds blindly; the owner decides.
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from bots.arb.model import cross_edge
from bots.arb.report import DAY_MS
from bots.config import PROJECT_DIR
from bots.live.exchange import make_authenticated_venue
from bots.risk.state import CRASH_LOCK, ENTRY_HALT, KILL_SWITCH, OWNER_PAUSE, RiskState

CONFIRM_FILE = "ARB_LIVE_CONFIRMED.txt"
ARB_LIVE_HALT = "arb_live_halt"          # persisted until the owner clears it
HARD_ARB_CAPITAL_USD = 500.0             # code ceiling on the capital the owner may confirm
HARD_ARB_TRADE_USD = 100.0               # code ceiling on one leg

SCHEMA = """
CREATE TABLE IF NOT EXISTS arb_live_trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ts INTEGER NOT NULL,
 pair TEXT NOT NULL,
 buy_venue TEXT NOT NULL,
 sell_venue TEXT NOT NULL,
 intended_qty REAL NOT NULL,
 buy_filled REAL NOT NULL,
 sell_filled REAL NOT NULL,
 buy_avg REAL,
 sell_avg REAL,
 fees REAL NOT NULL,
 net_usd REAL NOT NULL,
 status TEXT NOT NULL,            -- filled | legged | rejected | error
 buy_order_id TEXT,
 sell_order_id TEXT,
 detail TEXT NOT NULL DEFAULT ''
);
"""

VENUE_CREDENTIALS = {
    "kraken": ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"),
    "coinbaseexchange": ("COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_API_PASSPHRASE"),
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def confirm_file_path(env: dict) -> Path:
    return Path(env.get("BOTS_STATE_DIR") or PROJECT_DIR) / CONFIRM_FILE


def read_confirmed_capital(path: Path) -> float | None:
    try:
        value = float(path.read_text().strip().replace("$", "").replace(",", ""))
    except (OSError, ValueError):
        return None
    return value if math.isfinite(value) else None


def live_config_problems(acfg: dict) -> list[str]:
    lcfg = acfg.get("live") or {}
    problems = []
    cap = lcfg.get("max_total_capital_usd", 0)
    per = lcfg.get("per_trade_usd", 0)
    for name, v, ceiling in (("max_total_capital_usd", cap, HARD_ARB_CAPITAL_USD),
                             ("per_trade_usd", per, HARD_ARB_TRADE_USD)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            problems.append(f"arbitrage.live.{name} must be a positive number")
        elif v > ceiling:
            problems.append(f"arbitrage.live.{name} above the ${ceiling:.0f} code ceiling")
    if isinstance(per, (int, float)) and isinstance(cap, (int, float)) and per > cap > 0:
        problems.append("per_trade_usd above max_total_capital_usd")
    if float(acfg.get("size_usd", 0)) != float(per or -1):
        problems.append("arbitrage.size_usd must equal arbitrage.live.per_trade_usd so the "
                        "live edge is the one the scanner measured")
    for name in ("max_daily_loss_usd", "max_total_loss_usd", "min_net_edge_pct"):
        v = lcfg.get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            problems.append(f"arbitrage.live.{name} must be a positive number")
    if (lcfg.get("min_net_edge_pct") or 0) < (acfg.get("min_net_edge_pct") or 0):
        problems.append("live min_net_edge_pct must not be below the paper threshold")
    return problems


def credentials_present(venue: str, env: dict) -> bool:
    keys = VENUE_CREDENTIALS.get(venue)
    return bool(keys) and all(env.get(k) for k in keys)


def make_live_venues(acfg: dict, env: dict) -> dict:
    """Authenticated ccxt clients for every enabled venue, or raise with the reason.

    Kraken keys are proven unable to withdraw before being used.
    """
    clients = {}
    for name, vcfg in (acfg.get("venues") or {}).items():
        if not vcfg.get("enabled", True):
            continue
        if not credentials_present(name, env):
            raise RuntimeError(f"no API credentials for {name} ({', '.join(VENUE_CREDENTIALS.get(name, ()))})")
        keys = VENUE_CREDENTIALS[name]
        creds = {"apiKey": env[keys[0]], "secret": env[keys[1]]}
        if len(keys) > 2:
            creds["password"] = env[keys[2]]
        clients[name] = make_authenticated_venue(name, creds, env)
    return clients


def paper_evidence(conn: sqlite3.Connection, acfg: dict, now: int) -> list[str]:
    lcfg = acfg.get("live") or {}
    reasons = []
    has = lambda t: conn.execute(  # noqa: E731
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None
    if not has("arb_observations") or not has("arb_paper_trades"):
        return ["no paper arbitrage evidence recorded yet"]
    first, last = conn.execute("SELECT MIN(ts), MAX(ts) FROM arb_observations").fetchone()
    days = (last - first) / DAY_MS if first and last else 0.0
    if days < float(lcfg.get("min_paper_days", 14)):
        reasons.append(f"scanner has {days:.1f} observed days, needs {lcfg.get('min_paper_days', 14)}")
    n, pnl = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_usd), 0) FROM arb_paper_trades").fetchone()
    if n < int(lcfg.get("min_paper_trades", 20)):
        reasons.append(f"{n} paper arbitrages, needs {lcfg.get('min_paper_trades', 20)}")
    if pnl <= 0:
        reasons.append(f"paper arbitrage not ahead after costs (${pnl:+.2f})")
    if last is None or now - last > 3 * int(acfg.get("scan_seconds", 120)) * 1000:
        reasons.append("no fresh scan: last observation too old")
    return reasons


def readiness(conn: sqlite3.Connection, cfg: dict, env: dict, state: RiskState, now: int,
              clients_ready: bool) -> list[str]:
    """Every reason live arbitrage may not trade right now. Empty list = allowed."""
    acfg = cfg.get("arbitrage") or {}
    lcfg = acfg.get("live") or {}
    reasons = []
    if not acfg.get("enabled"):
        reasons.append("arbitrage scanner disabled")
    if lcfg.get("enabled") is not True:
        reasons.append("arbitrage.live.enabled is not true")
    if str(env.get("MODE", "")).strip().upper() != "LIVE":
        reasons.append("MODE env is not LIVE")
    problems = live_config_problems(acfg)
    reasons += problems
    confirmed = read_confirmed_capital(confirm_file_path(env))
    if confirmed is None:
        reasons.append(f"{CONFIRM_FILE} missing or unreadable")
    elif not problems and abs(confirmed - float(lcfg["max_total_capital_usd"])) > 1e-9:
        reasons.append(f"{CONFIRM_FILE} says {confirmed:g}, config says "
                       f"{float(lcfg['max_total_capital_usd']):g}")
    for key, label in ((KILL_SWITCH, "kill switch"), (CRASH_LOCK, "crash lock"),
                       (ENTRY_HALT, "entry halt"), (OWNER_PAUSE, "owner pause"),
                       (ARB_LIVE_HALT, "arbitrage halt")):
        if state.is_set(key):
            reasons.append(f"{label}: {state.get(key)}")
    if not clients_ready:
        reasons.append("no authenticated venue connections")
    reasons += paper_evidence(conn, acfg, now)
    return reasons


@dataclass
class LegResult:
    order_id: str | None = None
    filled: float = 0.0
    average: float | None = None
    fee: float = 0.0
    error: str | None = None


@dataclass
class LiveOutcome:
    status: str                          # filled | legged | rejected | error | skipped
    detail: str = ""
    net_usd: float = 0.0
    legs: dict = field(default_factory=dict)


def _free(balance: dict, asset: str) -> float:
    try:
        return float(((balance.get("free") or {}).get(asset)) or (balance.get(asset) or {}).get("free") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _place_ioc(client, symbol: str, side: str, qty: float, limit: float, fallback_fee: float) -> LegResult:
    leg = LegResult()
    try:
        order = client.create_order(symbol, "limit", side, qty, limit, {"timeInForce": "IOC"})
        leg.order_id = str(order.get("id"))
        try:
            fetched = client.fetch_order(leg.order_id, symbol)
            order = fetched or order
        except Exception as e:  # noqa: BLE001 - keep the create response, record the doubt
            leg.error = f"fetch_order failed: {e}"[:200]
        leg.filled = float(order.get("filled") or 0.0)
        leg.average = order.get("average") or order.get("price")
        leg.average = float(leg.average) if leg.average is not None else None
        fee = order.get("fee") or {}
        leg.fee = float(fee.get("cost") or 0.0) if isinstance(fee, dict) else 0.0
        if leg.fee == 0.0 and leg.filled and leg.average:
            leg.fee = leg.filled * leg.average * fallback_fee
    except Exception as e:  # noqa: BLE001
        leg.error = f"{type(e).__name__}: {e}"[:200]
    return leg


def _today_stats(conn: sqlite3.Connection, now: int) -> tuple[int, float, float]:
    day_start = now - now % DAY_MS
    n, today = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_usd), 0) FROM arb_live_trades "
        "WHERE ts >= ? AND status IN ('filled','legged')", (day_start,)).fetchone()
    total = conn.execute("SELECT COALESCE(SUM(net_usd), 0) FROM arb_live_trades "
                         "WHERE status IN ('filled','legged')").fetchone()[0]
    return int(n), float(today), float(total)


def execute(conn: sqlite3.Connection, cfg: dict, env: dict, state: RiskState, clients: dict,
            edge: dict, now: int | None = None, alerter=None) -> LiveOutcome:
    """Attempt one live arbitrage on `edge` (a scanner edge dict). Fail closed everywhere."""
    now = int(now if now is not None else time.time() * 1000)
    acfg = cfg.get("arbitrage") or {}
    lcfg = acfg.get("live") or {}
    reasons = readiness(conn, cfg, env, state, now, bool(clients))
    if reasons:
        return LiveOutcome("skipped", "; ".join(reasons))
    ensure_schema(conn)
    buy_v, sell_v, pair = edge["buy_venue"], edge["sell_venue"], edge["pair"]
    if buy_v not in clients or sell_v not in clients:
        return LiveOutcome("skipped", f"no authenticated client for {buy_v} or {sell_v}")
    vcfgs = acfg.get("venues") or {}
    slippage = float(acfg.get("slippage_pct", 0.05)) / 100
    buy_fee = float(vcfgs[buy_v]["taker_fee_pct"]) / 100
    sell_fee = float(vcfgs[sell_v]["taker_fee_pct"]) / 100
    qty = float(edge["qty"])
    e = cross_edge(float(edge["buy_price"]), float(edge["sell_price"]), qty, buy_fee, sell_fee, slippage)
    if e.net_pct < float(lcfg["min_net_edge_pct"]) / 100 or e.net_usd <= 0:
        return LiveOutcome("skipped", f"edge {100 * e.net_pct:+.3f}% below live minimum")
    if qty * float(edge["buy_price"]) > float(lcfg["per_trade_usd"]) * 1.01:
        return LiveOutcome("skipped", "edge sized above per_trade_usd")
    n_today, pnl_today, pnl_total = _today_stats(conn, now)
    if n_today >= int(lcfg.get("max_trades_per_day", 20)):
        return LiveOutcome("skipped", "daily live trade limit reached")
    if pnl_today <= -float(lcfg["max_daily_loss_usd"]) or pnl_total <= -float(lcfg["max_total_loss_usd"]):
        state.set(ARB_LIVE_HALT, f"loss limit hit: today ${pnl_today:+.2f}, total ${pnl_total:+.2f}")
        state.log("arb_live_halt", state.get(ARB_LIVE_HALT))
        if alerter:
            alerter.alert(f"Live arbitrage halted: {state.get(ARB_LIVE_HALT)}", kind="critical")
        return LiveOutcome("skipped", state.get(ARB_LIVE_HALT) or "loss limit")
    base, quote = pair.split("/")
    buy_sym = f"{base}/{vcfgs[buy_v].get('quote', quote)}"
    sell_sym = f"{base}/{vcfgs[sell_v].get('quote', quote)}"
    buy_limit = float(edge["buy_price"]) * (1 + slippage)
    sell_limit = float(edge["sell_price"]) * (1 - slippage)
    try:
        bal_buy = clients[buy_v].fetch_balance()
        bal_sell = clients[sell_v].fetch_balance()
    except Exception as ex:  # noqa: BLE001
        return LiveOutcome("skipped", f"balance check failed: {ex}"[:200])
    need_cash = qty * buy_limit * (1 + buy_fee) * 1.005
    if _free(bal_buy, vcfgs[buy_v].get("quote", quote)) < need_cash:
        return LiveOutcome("skipped", f"insufficient {quote} on {buy_v} for ${need_cash:.2f}")
    if _free(bal_sell, base) < qty:
        return LiveOutcome("skipped", f"insufficient {base} on {sell_v} for {qty:.6f}")
    mins = ((cfg.get("backtest") or {}).get("min_order") or {}).get(pair, {})
    if qty < float(mins.get("min_qty", 0)) or qty * buy_limit < float(mins.get("min_cost_usd", 0)):
        return LiveOutcome("skipped", "below exchange minimum order")

    # The sell leg first: being short coin for a moment on the dear venue is
    # covered by inventory; being long an unhedged coin is not.
    sell = _place_ioc(clients[sell_v], sell_sym, "sell", qty, sell_limit, sell_fee)
    buy = LegResult(error="not attempted") if sell.error and not sell.filled else \
        _place_ioc(clients[buy_v], buy_sym, "buy", sell.filled if sell.filled else qty, buy_limit, buy_fee)
    fees = buy.fee + sell.fee
    net = (sell.filled * (sell.average or 0.0)) - (buy.filled * (buy.average or 0.0)) - fees
    legs = {"buy": buy.__dict__, "sell": sell.__dict__}
    if sell.filled == 0 and buy.filled == 0:
        status = "rejected" if not (sell.error or buy.error) else "error"
        detail = "; ".join(x for x in (sell.error, buy.error) if x) or "neither IOC leg filled"
    elif abs(sell.filled - buy.filled) <= 1e-9 * max(1.0, qty) or (
            qty and abs(sell.filled - buy.filled) / qty < 0.001):
        status, detail = "filled", "both legs filled"
    else:
        status = "legged"
        detail = (f"unequal fills: sold {sell.filled:.8f} on {sell_v}, bought {buy.filled:.8f} on {buy_v}"
                  + ("; " + "; ".join(x for x in (sell.error, buy.error) if x) if (sell.error or buy.error) else ""))
    conn.execute(
        "INSERT INTO arb_live_trades(ts,pair,buy_venue,sell_venue,intended_qty,buy_filled,sell_filled,"
        "buy_avg,sell_avg,fees,net_usd,status,buy_order_id,sell_order_id,detail) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, pair, buy_v, sell_v, qty, buy.filled, sell.filled, buy.average, sell.average, fees, net,
         status, buy.order_id, sell.order_id, detail))
    conn.commit()
    state.log("arb_live_trade", f"{pair} {status}: {detail}; net ${net:+.3f}")
    if status in ("legged", "error"):
        state.set(ARB_LIVE_HALT, f"{pair} {status} at {now}: {detail}")
        if alerter:
            alerter.alert(f"LIVE ARBITRAGE HALTED. {pair}: {detail}. Check both venues and clear "
                          f"the '{ARB_LIVE_HALT}' lock only after reconciling.", kind="critical")
    elif alerter and status == "filled":
        alerter.alert(f"Live arbitrage {pair}: bought {buy.filled:.6f} on {buy_v}, sold on {sell_v}, "
                      f"net ${net:+.3f} after ${fees:.3f} fees.", kind="info")
    return LiveOutcome(status, detail, net, legs)


def tick(conn: sqlite3.Connection, cfg: dict, env: dict, state: RiskState, clients: dict,
         edges: list[dict], now: int | None = None, alerter=None) -> dict:
    """At most one live arbitrage per scan, on the best edge that clears the live bar."""
    now = int(now if now is not None else time.time() * 1000)
    acfg = cfg.get("arbitrage") or {}
    if not (acfg.get("live") or {}).get("enabled"):
        return {"enabled": False, "attempted": 0}
    reasons = readiness(conn, cfg, env, state, now, bool(clients))
    if reasons:
        return {"enabled": True, "attempted": 0, "blocked": reasons}
    ranked = sorted((e for e in edges if e["net_usd"] > 0), key=lambda e: e["net_pct"], reverse=True)
    for edge in ranked:
        out = execute(conn, cfg, env, state, clients, edge, now, alerter)
        if out.status != "skipped":
            return {"enabled": True, "attempted": 1, "status": out.status, "detail": out.detail,
                    "net_usd": out.net_usd}
    return {"enabled": True, "attempted": 0, "blocked": ["no edge cleared the live minimum"]}


def snapshot(conn: sqlite3.Connection, cfg: dict, env: dict, now: int, clients_ready: bool) -> dict:
    state = RiskState(conn)
    acfg = cfg.get("arbitrage") or {}
    lcfg = acfg.get("live") or {}
    reasons = readiness(conn, cfg, env, state, now, clients_ready)
    out = {"enabled": bool(lcfg.get("enabled")), "ready": not reasons, "blockers": reasons,
           "per_trade_usd": lcfg.get("per_trade_usd"), "max_total_capital_usd": lcfg.get("max_total_capital_usd"),
           "halt": state.get(ARB_LIVE_HALT), "trades": 0, "net_usd": 0.0, "fees_usd": 0.0,
           "legged": 0, "last_trades": []}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='arb_live_trades'").fetchone():
        n, net, fees, legged = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_usd),0), COALESCE(SUM(fees),0), "
            "SUM(CASE WHEN status='legged' THEN 1 ELSE 0 END) FROM arb_live_trades "
            "WHERE status IN ('filled','legged')").fetchone()
        out.update(trades=int(n), net_usd=float(net), fees_usd=float(fees), legged=int(legged or 0),
                   last_trades=[dict(r) for r in conn.execute(
                       "SELECT * FROM arb_live_trades ORDER BY id DESC LIMIT 10")])
    return out
