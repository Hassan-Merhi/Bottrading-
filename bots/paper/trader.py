"""The trading loop, shared by paper and live bots.

Every loop: fetch live Kraken candles, check the data, update prices, then for
each bot sync its stop-loss, take exits, take entries on freshly closed candles,
and mark it to market. Orders only ever go through RiskGuard. After the bots:
enforce the money limits, check the graduation gate, return live bots that
broke their limits to paper, and hand approved bots their live allocation.
"""
from __future__ import annotations

import json
import math
from collections import Counter
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from bots.data.fetch import TIMEFRAME_MS
from bots.data.loader import Series, series_from_df
from bots.data.store import upsert_candles
from bots.evolution.graveyard import Graveyard
from bots.risk.guard import OrderFailed, OrderRefused, RiskGuard
from bots.risk.ledger import DAY_MS
from bots.risk.state import HEARTBEAT
from bots.data.mood import load_fear_greed
from bots.strategy.context import MarketContext
from bots.strategy.genome import Genome
from bots.strategy.signals import compute_signals, stop_pct_now

PAPER_STATUSES = ("paper", "ready_to_graduate", "approved")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DataError(Exception):
    pass


def validate_candles(rows: list, timeframe: str, now: int, max_age_multiple: float) -> pd.DataFrame:
    """Closed, ordered, sane candles, or DataError. Unexpected data stops new trades."""
    if not rows:
        raise DataError("no candles returned")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    step = TIMEFRAME_MS[timeframe]
    df = df[df.ts + step <= now]  # closed candles only
    if len(df) < 2:
        raise DataError("too few closed candles")
    vals = df[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(vals).all() or (vals <= 0).any():
        raise DataError("non-positive or missing prices")
    if (df.high < df[["open", "close"]].max(axis=1)).any() or \
            (df.low > df[["open", "close"]].min(axis=1)).any():
        raise DataError("candle high/low inconsistent")
    if not (np.diff(df.ts.to_numpy()) > 0).all():
        raise DataError("candle timestamps out of order")
    age = now - (int(df.ts.iloc[-1]) + step)
    if age > max_age_multiple * step:
        raise DataError(f"latest candle is stale ({age // 60000} min old)")
    return df.reset_index(drop=True)


@dataclass
class TickReport:
    healthy: bool = True
    errors: list[str] = field(default_factory=list)
    entries: int = 0
    exits: int = 0


class Trader:
    def __init__(self, cfg: dict, conn: sqlite3.Connection, guard: RiskGuard, market_data,
                 paper_exchange, alerter=None):
        self.cfg = cfg
        self.conn = conn
        self.guard = guard
        self.md = market_data          # read-only public market data
        self.paper = paper_exchange    # receives prices so resting paper stops can trigger
        self.alerter = alerter or guard.alerter
        self.clock = guard.clock
        self._low_since: dict[str, int] = {}

    # -- bots --------------------------------------------------------------

    def bots(self, statuses=PAPER_STATUSES + ("live",)) -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(f"SELECT * FROM deployed_bots WHERE status IN ({q})",
                                 statuses).fetchall()

    @staticmethod
    def genome(bot: sqlite3.Row) -> Genome:
        return Genome.from_dict(json.loads(bot["genome_json"]))

    def deploy_candidates(self) -> int:
        """Move test-passed candidates into paper trading, up to paper.max_bots."""
        pcfg = self.cfg["paper"]
        running = self.bots(PAPER_STATUSES)
        room = pcfg["max_bots"] - len(running)
        if room <= 0:
            return 0
        # Diversity: ten near-identical bots teach less than ten different ones.
        per_pair: Counter = Counter(self.genome(b).pair for b in running)
        per_rule: Counter = Counter(self.genome(b).entry_type for b in running)
        max_pair = pcfg.get("max_per_pair", pcfg["max_bots"])
        max_rule = pcfg.get("max_per_entry_type", pcfg["max_bots"])
        rows = []
        for row in self.conn.execute("SELECT * FROM candidates WHERE stage='awaiting_paper' "
                                     "ORDER BY validation_fitness DESC"):
            g = Genome.from_dict(json.loads(row["genome_json"]))
            if per_pair[g.pair] >= max_pair or per_rule[g.entry_type] >= max_rule:
                continue  # stays in the queue for when a slot of its kind frees up
            per_pair[g.pair] += 1
            per_rule[g.entry_type] += 1
            rows.append(row)
            if len(rows) >= room:
                break
        for row in rows:
            bot_id = f"bot-{row['genome_hash'][:8]}-{uuid.uuid4().hex[:4]}"
            now = _iso()
            self.conn.execute(
                "INSERT INTO deployed_bots (bot_id, genome_json, genome_hash, status, "
                "status_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (bot_id, row["genome_json"], row["genome_hash"], "paper",
                 "passed the test-set check", now, now))
            self.conn.execute("UPDATE candidates SET stage='deployed' WHERE genome_hash=?",
                              (row["genome_hash"],))
            self.conn.commit()
            g = Genome.from_dict(json.loads(row["genome_json"]))
            self.alerter.alert(f"New paper bot {bot_id}: {g.describe()}", kind="info")
        return len(rows)

    def _ensure_accounts(self, prices: dict[str, float]) -> None:
        for bot in self.bots():
            pair = json.loads(bot["genome_json"])["pair"]
            if bot["status"] in PAPER_STATUSES and self.guard.ledger.account(bot["bot_id"],
                                                                             "paper") is None:
                self.guard.open_paper_account(bot["bot_id"], prices.get(pair))
            if bot["status"] == "live" and self.guard.ledger.account(bot["bot_id"], "live") is None:
                self._set_status(bot["bot_id"], "paper", "no live account")

    def restore_paper_stops(self) -> None:
        """Paper stops live in memory. After a restart, put them back."""
        for acc in self.guard.ledger.active("paper"):
            if acc["qty"] > 0:
                self.guard.ledger.set_stop_order(acc["account_id"], None)
        self._pending_paper_restore = True

    def _restore_now(self) -> None:
        if not getattr(self, "_pending_paper_restore", False):
            return
        from bots.risk.guard import OrderRequest

        for acc in self.guard.ledger.active("paper"):
            if acc["qty"] > 0 and not acc["stop_order_id"]:
                pair = self.guard._pair(acc["bot_id"])
                if pair not in self.paper.prices:
                    continue
                try:
                    order = self.guard.place_order(OrderRequest(
                        acc["bot_id"], "paper", pair, "sell", acc["qty"], order_type="stop_loss",
                        stop_price=acc["stop_price"], reason="restore paper stop"))
                    self.guard.ledger.set_stop_order(acc["account_id"], str(order["id"]))
                    self.guard.sync_stop(acc["bot_id"], "paper")
                except (OrderRefused, OrderFailed):
                    self.guard.exit_long(acc["bot_id"], "paper", "stop could not be restored")
        self._pending_paper_restore = False

    # -- the loop ----------------------------------------------------------

    def tick(self) -> TickReport:
        report = TickReport()
        now = self.clock()
        bots = self.bots()
        genomes = [self.genome(b) for b in bots]
        needed = {(g.pair, g.timeframe) for g in genomes}
        base = self.cfg["data"]["base_timeframe"]
        pairs = {p for p, _ in needed}
        needed |= {(p, base) for p in pairs}
        # Daily candles for trend filters (Kraken serves 720 days, enough for a 200-day average).
        needed |= {(g.pair if g.trend_filter == "own_daily" else "BTC/USD", "1d")
                   for g in genomes if g.trend_filter != "none"}
        series: dict[tuple[str, str], Series] = {}
        for pair, tf in sorted(needed):
            try:
                rows = self.md.fetch_ohlcv(pair, tf, limit=self.cfg["paper"]["candles"])
                df = validate_candles(rows, tf, now, self.cfg["worker"]["max_candle_age_multiple"])
            except Exception as e:  # noqa: BLE001 - network, API or data problem
                report.healthy = False
                report.errors.append(f"{pair} {tf}: {e}")
                continue
            series[(pair, tf)] = series_from_df(pair, tf, df)
            if tf == base:
                upsert_candles(self.conn, pair, tf, df)

        prices: dict[str, float] = {}
        bids: dict[str, float] = {}
        for pair in sorted(pairs):
            try:
                ticker = self.md.fetch_ticker(pair)
                last = float(ticker["last"])
                if not (math.isfinite(last) and last > 0):
                    raise DataError("bad ticker price")
            except Exception as e:  # noqa: BLE001
                report.healthy = False
                report.errors.append(f"{pair} ticker: {e}")
                continue
            prices[pair] = last
            bid = ticker.get("bid")
            if bid is not None and math.isfinite(float(bid)) and 0 < float(bid) <= last:
                bids[pair] = float(bid)
            s = series.get((pair, base))
            low = None
            if s is not None:
                since = self._low_since.get(pair, int(s.ts[-1]))
                recent = s.low[s.ts >= since]
                low = float(recent.min()) if len(recent) else None
                self._low_since[pair] = int(s.ts[-1])
            self.paper.update_price(pair, last, low=low)
        self.guard.update_marks(prices)
        if not report.healthy:
            self.guard.halt_entries("; ".join(report.errors))

        ctx = MarketContext(daily={p: sr for (p, tf), sr in series.items() if tf == "1d"},
                            fear_greed=load_fear_greed(self.conn))
        self._ensure_accounts(prices)
        self._restore_now()
        for bot in self.bots():
            g = self.genome(bot)
            modes = ["live"] if bot["status"] == "live" else ["paper"]
            for mode in modes:
                try:
                    self._run_bot(bot, g, mode, series.get((g.pair, g.timeframe)),
                                  prices.get(g.pair), report, ctx, bids.get(g.pair))
                except (OrderRefused, OrderFailed) as e:
                    report.errors.append(f"{bot['bot_id']}: {e}")
        # Live bots keep being managed in PAPER mode (e.g. after a crash): exits and stops
        # still work, while the guard refuses their new entries until the owner confirms.
        self.guard.check_limits()
        self._graduation(prices)
        self._live_limits()
        self._activate_approved(prices)
        if report.healthy:
            self.guard.note_healthy(self.cfg["worker"]["healthy_ticks_to_resume"])
        self.guard.state.set(HEARTBEAT, self.clock())
        return report

    def _run_bot(self, bot, g: Genome, mode: str, s: Series | None, price: float | None,
                 report: TickReport, ctx: MarketContext | None = None,
                 bid: float | None = None) -> None:
        acc = self.guard.ledger.account(bot["bot_id"], mode)
        if acc is None:
            return
        acc = self.guard.ledger.mark(acc["account_id"], price, self.clock())
        if acc["pending_order_id"]:
            if self.guard.sync_pending(bot["bot_id"], mode) == "filled":
                report.entries += 1
            acc = self.guard.ledger.by_id(acc["account_id"])
        if acc["qty"] > 0:
            if self.guard.sync_stop(bot["bot_id"], mode) is not None:
                report.exits += 1
            acc = self.guard.ledger.by_id(acc["account_id"])
            if acc["qty"] > 0 and price is not None and price >= acc["target_price"]:
                self.guard.exit_long(bot["bot_id"], mode, "target")
                report.exits += 1
            elif acc["qty"] > 0 and price is not None:
                # Trailing stop and time exit run in software on top of the hard stop that
                # rests on the exchange; both sell through the guard like any other exit.
                if g.trail_pct > 0:
                    high = self.guard.ledger.note_high(acc["account_id"], price)
                    level = high * (1 - g.trail_pct / 100)
                    if level > (acc["stop_price"] or 0) and price <= level:
                        self.guard.exit_long(bot["bot_id"], mode, "trail")
                        report.exits += 1
                        acc = self.guard.ledger.by_id(acc["account_id"])
                if acc["qty"] > 0 and g.max_hold_candles and acc["entry_ts"] and \
                        self.clock() - acc["entry_ts"] >= \
                        g.max_hold_candles * TIMEFRAME_MS[g.timeframe]:
                    self.guard.exit_long(bot["bot_id"], mode, "time")
                    report.exits += 1
        if s is None or price is None or len(s) < 2:
            return
        last_ts = int(s.ts[-1])
        acc = self.guard.ledger.by_id(acc["account_id"])
        if last_ts <= acc["last_candle_ts"]:
            return
        entry, exit_ = compute_signals(s, g, ctx)
        self.guard.ledger.set_last_candle(acc["account_id"], last_ts)
        if acc["qty"] > 0 and exit_[-1]:
            self.guard.exit_long(bot["bot_id"], mode, "signal")
            report.exits += 1
        elif acc["pending_order_id"] and exit_[-1]:
            self.guard.cancel_pending(bot["bot_id"], mode, "exit signal before the entry filled")
        elif acc["qty"] == 0 and not acc["pending_order_id"] and entry[-1] \
                and acc["trades_today"] < g.max_trades_per_day:
            if self.guard.enter_long(bot["bot_id"], mode, g.pair, price, g.position_size_pct,
                                     stop_pct_now(s, g), g.take_profit_pct,
                                     f"{g.entry_type} entry", limit_price=bid):
                report.entries += 1
        self.guard.ledger.mark(acc["account_id"], price, self.clock())

    # -- the pipeline gates ------------------------------------------------

    def paper_stats(self, acc, price: float | None) -> dict:
        eq = self.guard.ledger.equity(acc, price)
        ret = eq / acc["allocation"] - 1
        hold = None
        if acc["hold_start_price"] and price:
            cost = (1 + self.guard.slippage) * (1 + self.guard.fee)
            hold = price * (1 - self.guard.slippage) * (1 - self.guard.fee) / \
                (acc["hold_start_price"] * cost) - 1
        return {"equity": eq, "return": ret, "hold_return": hold,
                "days": (self.clock() - acc["started_at"]) / DAY_MS,
                "max_drawdown": acc["max_drawdown"], "trades": acc["trades"]}

    def graduation_check(self, acc, price: float | None) -> tuple[bool, list[str]]:
        """Section 5 step 4: every condition except the owner's approval."""
        g = self.cfg["graduation"]
        st = self.paper_stats(acc, price)
        missing = []
        if st["days"] < g["min_paper_days"]:
            missing.append(f"{st['days']:.1f}/{g['min_paper_days']} paper days")
        if st["return"] <= 0:
            missing.append("not profitable after fees")
        if st["max_drawdown"] >= g["max_paper_drawdown_pct"] / 100:
            missing.append(f"drawdown {st['max_drawdown']:.1%} not under "
                           f"{g['max_paper_drawdown_pct']}%")
        if st["trades"] < g["min_paper_trades"]:
            missing.append(f"{st['trades']}/{g['min_paper_trades']} trades")
        if g["must_beat_hold"] and (st["hold_return"] is None or st["return"] <= st["hold_return"]):
            missing.append("did not beat holding the coin")
        return not missing, missing

    def _graduation(self, prices: dict[str, float]) -> None:
        for bot in self.bots(("paper",)):
            acc = self.guard.ledger.account(bot["bot_id"], "paper")
            if acc is None:
                continue
            price = prices.get(json.loads(bot["genome_json"])["pair"])
            if acc["max_drawdown"] > self.cfg["paper"]["retire_drawdown_pct"] / 100:
                self._retire(bot, acc, price, f"paper drawdown {acc['max_drawdown']:.0%}")
                continue
            ok, _ = self.graduation_check(acc, price)
            if ok:
                self._set_status(bot["bot_id"], "ready_to_graduate", "passed the paper gate")
                st = self.paper_stats(acc, price)
                self.alerter.alert(
                    f"Bot {bot['bot_id']} is ready to graduate: {self.genome(bot).describe()}. "
                    f"Paper: {st['return']:+.1%} vs hold {st['hold_return']:+.1%}, "
                    f"{st['trades']} trades, max drawdown {st['max_drawdown']:.1%} over "
                    f"{st['days']:.0f} days. Live trading can lose money up to your caps.",
                    kind="graduation",
                    buttons=[("Approve", f"approve:{bot['bot_id']}"),
                             ("Decline", f"decline:{bot['bot_id']}")])

    def _retire(self, bot, acc, price, reason: str) -> None:
        self.guard.exit_long(bot["bot_id"], "paper", reason)
        self.guard.ledger.close_account(acc["account_id"], self.clock())
        self._set_status(bot["bot_id"], "retired", reason)
        Graveyard(self.conn, self.cfg["graveyard"]).bury(
            "paper", 0, self.genome(bot), reason, "lost too much in paper trading", "live market",
            0.0, 0.0, self.paper_stats(acc, price))
        self.conn.commit()
        self.alerter.alert(f"Paper bot {bot['bot_id']} retired: {reason}", kind="info")

    def _live_limits(self) -> None:
        limit = self.cfg["graduation"]["max_paper_drawdown_pct"] / 100
        for acc in self.guard.ledger.active("live"):
            if acc["max_drawdown"] > limit:
                self.guard.demote(acc["bot_id"], f"live drawdown {acc['max_drawdown']:.1%} "
                                                 f"above {limit:.0%}")

    def _activate_approved(self, prices: dict[str, float]) -> None:
        for bot in self.bots(("approved",)):
            pair = json.loads(bot["genome_json"])["pair"]
            if self.guard.open_live_account(bot["bot_id"], prices.get(pair)) is None:
                continue
            paper = self.guard.ledger.account(bot["bot_id"], "paper")
            if paper is not None:
                self.guard.exit_long(bot["bot_id"], "paper", "graduated to live")
                self.guard.ledger.close_account(paper["account_id"], self.clock())
            self._set_status(bot["bot_id"], "live", "owner approved; live allocation granted")
            acc = self.guard.ledger.account(bot["bot_id"], "live")
            self.alerter.alert(f"Bot {bot['bot_id']} is now LIVE with ${acc['allocation']:.2f}. "
                               "Real money: losses are possible up to your caps.", kind="risk")

    def _set_status(self, bot_id: str, status: str, reason: str) -> None:
        self.conn.execute("UPDATE deployed_bots SET status=?, status_reason=?, updated_at=? "
                          "WHERE bot_id=?", (status, reason, _iso(), bot_id))
        self.conn.commit()


def decide_graduation(conn: sqlite3.Connection, bot_id: str, approve: bool, who: str) -> str:
    """The owner's manual approval (dashboard or Telegram button)."""
    row = conn.execute("SELECT status FROM deployed_bots WHERE bot_id=?", (bot_id,)).fetchone()
    if row is None:
        return f"Unknown bot {bot_id}."
    if row[0] != "ready_to_graduate":
        return f"Bot {bot_id} is {row[0]}, not waiting for approval."
    status, reason = ("approved", f"approved by {who}") if approve else \
        ("retired", f"declined by {who}")
    conn.execute("UPDATE deployed_bots SET status=?, status_reason=?, updated_at=? WHERE bot_id=?",
                 (status, reason, _iso(), bot_id))
    conn.execute("INSERT INTO risk_events (ts, kind, detail) VALUES (?, ?, ?)",
                 (int(datetime.now(timezone.utc).timestamp() * 1000), "graduation_decision",
                  f"{bot_id} {status} by {who}"))
    conn.commit()
    if approve:
        return (f"Bot {bot_id} approved. It goes live only while LIVE mode is unlocked and a "
                "slot and capital are free.")
    return f"Bot {bot_id} declined and retired."
