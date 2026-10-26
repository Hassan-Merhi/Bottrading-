"""Per-bot money accounting for paper and live accounts.

Only RiskGuard records fills. Everyone else reads.
"""
from __future__ import annotations

import sqlite3

DAY_MS = 86_400_000


class Ledger:
    def __init__(self, conn: sqlite3.Connection, house_money_multiple: float = 2.0):
        self.conn = conn
        self.house_money_multiple = house_money_multiple

    # -- reads -------------------------------------------------------------

    def account(self, bot_id: str, mode: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM accounts WHERE bot_id=? AND mode=? AND active=1", (bot_id, mode)
        ).fetchone()

    def by_id(self, account_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM accounts WHERE account_id=?",
                                 (account_id,)).fetchone()

    def active(self, mode: str | None = None) -> list[sqlite3.Row]:
        if mode is None:
            return self.conn.execute("SELECT * FROM accounts WHERE active=1").fetchall()
        return self.conn.execute("SELECT * FROM accounts WHERE active=1 AND mode=?",
                                 (mode,)).fetchall()

    def live_epoch_accounts(self, epoch: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM accounts WHERE mode='live' AND epoch=?",
                                 (epoch,)).fetchall()

    @staticmethod
    def equity(acc: sqlite3.Row, price: float | None) -> float:
        if acc["qty"] and price is not None:
            return acc["cash"] + acc["qty"] * price
        if acc["qty"]:
            return acc["last_equity"]
        return acc["cash"]

    @staticmethod
    def tradable_cash(acc: sqlite3.Row) -> float:
        """Cash the bot may trade with: protected house-money stake excluded."""
        return max(0.0, acc["cash"] - acc["protected"])

    # -- writes ------------------------------------------------------------

    def open_account(self, bot_id: str, mode: str, allocation: float, epoch: int, now: int,
                     hold_start_price: float | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO accounts (bot_id, mode, active, epoch, allocation, cash, peak_equity, "
            "last_equity, day, day_start_equity, hold_start_price, started_at) "
            "VALUES (?,?,1,?,?,?,?,?,?,?,?,?)",
            (bot_id, mode, epoch, allocation, allocation, allocation, allocation, now // DAY_MS,
             allocation, hold_start_price, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_account(self, account_id: int, now: int) -> None:
        self.conn.execute("UPDATE accounts SET active=0, closed_at=? WHERE account_id=?",
                          (now, account_id))
        self.conn.commit()

    def record_buy(self, acc: sqlite3.Row, qty: float, price: float, fee: float, ts: int,
                   stop: float, target: float, order_id: str | None, reason: str) -> None:
        cost = qty * price + fee
        self.conn.execute(
            "UPDATE accounts SET cash=cash-?, qty=?, entry_price=?, entry_cost=?, entry_ts=?, "
            "stop_price=?, target_price=?, stop_order_id=NULL, fees=fees+?, "
            "trades_today=trades_today+1 WHERE account_id=?",
            (cost, qty, price, cost, ts, stop, target, fee, acc["account_id"]),
        )
        self._fill(acc, ts, "buy", qty, price, fee, None, reason, order_id)

    def record_sell(self, acc: sqlite3.Row, qty: float, price: float, fee: float, ts: int,
                    reason: str, order_id: str | None) -> float:
        proceeds = qty * price - fee
        held = acc["qty"]
        share = min(qty / held, 1.0) if held else 1.0
        cost = (acc["entry_cost"] or 0.0) * share
        pnl = proceeds - cost
        left = max(held - qty, 0.0)
        if left < 1e-12:
            left = 0.0
        self.conn.execute(
            "UPDATE accounts SET cash=cash+?, qty=?, entry_cost=?, fees=fees+?, "
            "trades=trades+?, wins=wins+? WHERE account_id=?",
            (proceeds, left, (acc["entry_cost"] or 0.0) - cost if left else None, fee,
             1 if not left else 0, 1 if (not left and pnl > 0) else 0, acc["account_id"]),
        )
        if not left:
            self.conn.execute(
                "UPDATE accounts SET entry_price=NULL, entry_ts=NULL, stop_price=NULL, "
                "target_price=NULL, stop_order_id=NULL WHERE account_id=?", (acc["account_id"],))
        self._fill(acc, ts, "sell", qty, price, fee, pnl, reason, order_id)
        self._house_money(acc["account_id"])
        return pnl

    def set_stop_order(self, account_id: int, order_id: str | None) -> None:
        self.conn.execute("UPDATE accounts SET stop_order_id=? WHERE account_id=?",
                          (order_id, account_id))
        self.conn.commit()

    def mark(self, account_id: int, price: float | None, now: int) -> sqlite3.Row:
        """Mark to market: equity, peak, drawdown, and the UTC day rollover."""
        acc = self.by_id(account_id)
        eq = self.equity(acc, price)
        peak = max(acc["peak_equity"], eq)
        dd = max(acc["max_drawdown"], 1 - eq / peak if peak > 0 else 0.0)
        day = now // DAY_MS
        if day != acc["day"]:
            self.conn.execute("UPDATE accounts SET day=?, day_start_equity=?, trades_today=0 "
                              "WHERE account_id=?", (day, eq, account_id))
        self.conn.execute("UPDATE accounts SET last_equity=?, peak_equity=?, max_drawdown=? "
                          "WHERE account_id=?", (eq, peak, dd, account_id))
        self.conn.execute("INSERT INTO equity_history (account_id, day, ts, equity) "
                          "VALUES (?,?,?,?) ON CONFLICT(account_id, day) DO UPDATE SET "
                          "ts=excluded.ts, equity=excluded.equity", (account_id, day, now, eq))
        self.conn.commit()
        return self.by_id(account_id)

    def set_last_candle(self, account_id: int, ts: int) -> None:
        self.conn.execute("UPDATE accounts SET last_candle_ts=? WHERE account_id=?",
                          (ts, account_id))
        self.conn.commit()

    def _house_money(self, account_id: int) -> None:
        """Section 6.5: at 2x the starting allocation, lock the original stake away."""
        acc = self.by_id(account_id)
        if acc["mode"] != "live" or acc["protected"] > 0:
            return
        if acc["cash"] >= self.house_money_multiple * acc["allocation"] and not acc["qty"]:
            self.conn.execute("UPDATE accounts SET protected=? WHERE account_id=?",
                              (acc["allocation"], account_id))
            self.conn.commit()

    def _fill(self, acc, ts, side, qty, price, fee, pnl, reason, order_id) -> None:
        self.conn.execute(
            "INSERT INTO fills (account_id, bot_id, mode, ts, pair, side, qty, price, fee, pnl, "
            "reason, order_id) VALUES (?,?,?,?,(SELECT json_extract(genome_json, '$.pair') FROM "
            "deployed_bots WHERE bot_id=?),?,?,?,?,?,?,?)",
            (acc["account_id"], acc["bot_id"], acc["mode"], ts, acc["bot_id"], side, qty, price,
             fee, pnl, reason, order_id),
        )
        self.conn.commit()
