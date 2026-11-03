"""Hourly Telegram update: every bot, its coin, its money, and what happened this hour.

Covers the four places a bot can live: the paper arena (evolved bots on fake
money), the research league (three fixed bots), the graduation paper slots and
live accounts (the real-money path), plus paper arbitrage. Everything is read
from the database; nothing here changes state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from bots.paper.league import ARENA_PREFIX
from bots.strategy.genome import Genome

HOUR_MS = 3_600_000
TELEGRAM_LIMIT = 3900  # Telegram cuts messages at 4096 characters


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _money(x: float) -> str:
    return f"{x:+.2f}$" if abs(x) >= 0.005 else "±0.00$"


def _coin(pair: str) -> str:
    return pair.split("/")[0]


def _iso_ms(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _lab_bot_lines(conn: sqlite3.Connection, cfg: dict, now: int, arena: bool) -> tuple[list[str], dict]:
    """One line per paper-league / arena account, with the hour's fills folded in."""
    since = now - HOUR_MS
    if not _has(conn, "lab_accounts"):
        return [], {"pnl": 0.0, "hour": 0.0, "n": 0}
    op = "LIKE" if arena else "NOT LIKE"
    rows = conn.execute(f"SELECT * FROM lab_accounts WHERE name {op} ? ORDER BY name",
                        (ARENA_PREFIX + "%",)).fetchall()
    initial = float((cfg.get("paper_arena" if arena else "research_league") or {}).get("allocation_usd", 20))
    lines, total, hour_total = [], 0.0, 0.0
    retired = {}
    if arena and _has(conn, "arena_bots"):
        retired = {r["name"]: r for r in conn.execute(
            "SELECT name, retired_ms, retire_reason, added_ms FROM arena_bots")}
    for a in rows:
        name = a["name"]
        meta = retired.get(name)
        if meta and meta["retired_ms"] and meta["retired_ms"] < since:
            continue  # retired more than an hour ago: no longer reported
        g = Genome.from_dict(json.loads(a["genome_json"]))
        pnl = float(a["last_equity"]) - initial
        prev = conn.execute("SELECT equity FROM lab_marks WHERE name=? AND candle_ts <= ? "
                            "ORDER BY candle_ts DESC LIMIT 1", (name, since)).fetchone()
        hour = float(a["last_equity"]) - float(prev[0]) if prev else 0.0
        fills = conn.execute("SELECT side, qty, price, pnl, reason FROM lab_fills WHERE name=? AND ts > ? "
                             "ORDER BY ts", (name, since)).fetchall()
        total += pnl
        hour_total += hour
        short = name.split("/", 1)[1] if "/" in name else name
        tags = []
        if meta and meta["retired_ms"] and meta["retired_ms"] >= since:
            tags.append(f"DIED: {meta['retire_reason']}")
        elif meta and meta["added_ms"] >= since:
            tags.append("NEW this hour")
        if a["qty"] > 0:
            tags.append(f"holding {_coin(g.pair)}")
        for f in fills:
            if f["side"] == "buy":
                tags.append(f"bought {f['qty']:.5g} {_coin(g.pair)} @ {f['price']:.4g}")
            else:
                tags.append(f"sold {f['qty']:.5g} {_coin(g.pair)} @ {f['price']:.4g} "
                            f"{_money(float(f['pnl'] or 0))} ({f['reason']})")
        verdict = "up" if hour > 0.005 else "down" if hour < -0.005 else "flat"
        lines.append(f"• {short} | {_coin(g.pair)} {g.timeframe} {g.entry_type.replace('_', ' ')} | "
                     f"{float(a['last_equity']):.2f}$ ({_money(pnl)} total, {verdict} {_money(hour)} this hour, "
                     f"{a['closed_trades']} trades)" + (" | " + "; ".join(tags) if tags else ""))
    return lines, {"pnl": total, "hour": hour_total, "n": len(lines)}


def _account_lines(conn: sqlite3.Connection, now: int) -> tuple[list[str], dict]:
    """Graduation-path paper and live accounts, with status changes this hour."""
    since = now - HOUR_MS
    rows = conn.execute(
        "SELECT a.*, b.genome_json, b.status, b.status_reason, b.updated_at FROM accounts a "
        "JOIN deployed_bots b ON a.bot_id=b.bot_id WHERE a.active=1 OR b.updated_at >= ? "
        "ORDER BY a.mode DESC, a.account_id",
        (datetime.fromtimestamp(since / 1000, timezone.utc).isoformat(),)).fetchall()
    lines, total, hour_total = [], 0.0, 0.0
    for a in rows:
        g = Genome.from_dict(json.loads(a["genome_json"]))
        pnl = float(a["last_equity"]) - float(a["allocation"])
        prev = conn.execute("SELECT equity FROM paper_observations WHERE account_id=? AND ts <= ? "
                            "ORDER BY ts DESC LIMIT 1", (a["account_id"], since)).fetchone()
        hour = float(a["last_equity"]) - float(prev[0]) if prev else 0.0
        fills = conn.execute("SELECT side, qty, price, pnl, reason FROM fills WHERE account_id=? AND ts > ? "
                             "ORDER BY ts", (a["account_id"], since)).fetchall()
        tags = []
        changed = _iso_ms(a["updated_at"])
        if changed and changed >= since:
            tags.append(f"MOVED ON: now {a['status']}" + (f" ({a['status_reason']})" if a["status_reason"] else ""))
        if a["qty"] > 0:
            tags.append(f"holding {_coin(g.pair)}")
        for f in fills:
            if f["side"] == "buy":
                tags.append(f"bought {f['qty']:.5g} {_coin(g.pair)} @ {f['price']:.4g}")
            else:
                tags.append(f"sold {f['qty']:.5g} {_coin(g.pair)} @ {f['price']:.4g} "
                            f"{_money(float(f['pnl'] or 0))} ({f['reason']})")
        total += pnl
        hour_total += hour
        verdict = "up" if hour > 0.005 else "down" if hour < -0.005 else "flat"
        lines.append(f"• {a['bot_id']} [{a['mode'].upper()}] | {_coin(g.pair)} {g.timeframe} "
                     f"{g.entry_type.replace('_', ' ')} | {float(a['last_equity']):.2f}$ ({_money(pnl)} total, "
                     f"{verdict} {_money(hour)} this hour, {a['trades']} trades)"
                     + (" | " + "; ".join(tags) if tags else ""))
    return lines, {"pnl": total, "hour": hour_total, "n": len(lines)}


def _arbitrage_lines(conn: sqlite3.Connection, now: int) -> list[str]:
    since = now - HOUR_MS
    out = []
    if _has(conn, "arb_observations"):
        n, best = conn.execute("SELECT COUNT(*), MAX(net_pct) FROM arb_observations WHERE ts > ?",
                               (since,)).fetchone()
        if n:
            out.append(f"Arbitrage scanner: {n} checks this hour, best gap after fees {100 * best:+.3f}%")
    if _has(conn, "arb_paper_trades"):
        for t in conn.execute("SELECT * FROM arb_paper_trades WHERE ts > ? ORDER BY ts", (since,)):
            out.append(f"• paper arbitrage {_coin(t['pair'])}: bought on {t['buy_venue']}, sold on "
                       f"{t['sell_venue']}, {_money(t['net_usd'])} after fees")
    if _has(conn, "arb_live_trades"):
        for t in conn.execute("SELECT * FROM arb_live_trades WHERE ts > ? ORDER BY ts", (since,)):
            out.append(f"• REAL arbitrage {_coin(t['pair'])}: {t['status']}, {_money(t['net_usd'])}"
                       + (f" ({t['detail']})" if t["status"] != "filled" else ""))
    return out


def _events(conn: sqlite3.Connection, now: int) -> list[str]:
    since = now - HOUR_MS
    out = []
    if _has(conn, "graveyard"):
        n = conn.execute("SELECT COUNT(*) FROM graveyard WHERE created_at >= ?",
                         (datetime.fromtimestamp(since / 1000, timezone.utc).isoformat(),)).fetchone()[0]
        if n:
            out.append(f"Evolution buried {n} bot(s) this hour")
    for r in conn.execute("SELECT kind, detail FROM risk_events WHERE ts > ? ORDER BY ts", (since,)):
        if r["kind"] in ("entries_halted", "entries_resumed", "kill", "arb_live_halt", "owner_pause",
                         "owner_resume", "graduation", "approved", "demoted"):
            out.append(f"{r['kind'].replace('_', ' ')}: {r['detail'][:140]}")
    return out


def hourly_report_text(conn: sqlite3.Connection, cfg: dict, now: int) -> str:
    stamp = datetime.fromtimestamp(now / 1000, timezone.utc).strftime("%H:%M UTC, %d %b")
    arena_lines, arena = _lab_bot_lines(conn, cfg, now, arena=True)
    league_lines, league = _lab_bot_lines(conn, cfg, now, arena=False)
    acct_lines, accts = _account_lines(conn, now)
    arb_lines = _arbitrage_lines(conn, now)
    events = _events(conn, now)
    total_hour = arena["hour"] + league["hour"] + accts["hour"]
    total = arena["pnl"] + league["pnl"] + accts["pnl"]
    parts = [f"Hourly update {stamp}",
             f"All bots: {arena['n'] + league['n'] + accts['n']} running, {_money(total_hour)} this hour, "
             f"{_money(total)} since start. Fake money unless marked REAL."]
    if acct_lines:
        parts += ["", f"Real-money path ({accts['n']} bots, {_money(accts['hour'])} this hour):"] + acct_lines
    if arena_lines:
        parts += ["", f"Paper arena ({arena['n']} bots, {_money(arena['hour'])} this hour):"] + arena_lines
    else:
        parts += ["", "Paper arena: empty until the next evolution run."]
    if league_lines:
        parts += ["", f"Research league ({league['n']} bots, {_money(league['hour'])} this hour):"] + league_lines
    if arb_lines:
        parts += [""] + arb_lines
    if events:
        parts += ["", "Events:"] + events
    return "\n".join(parts)


def chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so no message exceeds Telegram's size limit."""
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        out.append(cur)
    return out
