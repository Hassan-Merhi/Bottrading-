"""The paper arena: evolved bots trade fake money on real prices, and evolution learns from it.

The strict Phase 4 gate decides which bots may ever approach real money, and
it rejects almost everything. That is right for real money, but it also meant
no evolved bot ever traded forward, so the system learned nothing from live
prices. The arena fixes the second problem without touching the first:

* After every evolution run the best diverse bots of its final generation
  enter the arena with `allocation_usd` of fake money each, gate or no gate.
* Each one trades forward on closed Kraken candles with the same simulated
  fills as the research league (taker fee, spread, slippage, no replays).
* Clear losers retire: deep drawdown, or under water after enough trades.
  Their genome is buried in the graveyard with the cause "lost money forward",
  so evolution steers newcomers away from them.
* Bots that are ahead after enough trades are "forward winners": the next
  evolution run starts with them in its population.

Nothing in the arena can reach a paper slot on the real-money path, an
approval, or an exchange. It is where the system practises.
"""
from __future__ import annotations

import json
import sqlite3
import time

from bots.evolution.diversity import diverse_top
from bots.evolution.graveyard import Graveyard
from bots.paper.league import ARENA_PREFIX, account_rows, tick_genomes
from bots.strategy.genome import Genome

SCHEMA = """
CREATE TABLE IF NOT EXISTS arena_bots (
 name TEXT PRIMARY KEY,
 genome_json TEXT NOT NULL,
 genome_hash TEXT NOT NULL,
 run_id TEXT NOT NULL,
 rank INTEGER NOT NULL,
 train_fitness REAL NOT NULL,
 added_ms INTEGER NOT NULL,
 retired_ms INTEGER,
 retire_reason TEXT
);
CREATE INDEX IF NOT EXISTS arena_bots_active ON arena_bots(retired_ms);
"""

FORWARD_DEATH = "lost money forward (paper arena)"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _cfg(cfg: dict) -> dict:
    return cfg.get("paper_arena") or {}


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute("SELECT * FROM arena_bots WHERE retired_ms IS NULL ORDER BY added_ms").fetchall()


def active_genomes(conn: sqlite3.Connection) -> dict[str, Genome]:
    out = {}
    for r in active(conn):
        try:
            out[r["name"]] = Genome.from_dict(json.loads(r["genome_json"]))
        except (ValueError, TypeError, KeyError):
            continue
    return out


def refresh_from_run(conn: sqlite3.Connection, cfg: dict, run_id: str,
                     now: int | None = None) -> dict:
    """Fill free arena slots with the best diverse bots of `run_id`'s final generation."""
    acfg = _cfg(cfg)
    if not acfg.get("enabled", False):
        return {"enabled": False, "added": 0}
    now = int(now if now is not None else time.time() * 1000)
    ensure_schema(conn)
    rows = conn.execute("SELECT rank, genome_json, fitness FROM population WHERE run_id=? "
                        "ORDER BY rank", (run_id,)).fetchall()
    live = active(conn)
    free = max(0, int(acfg.get("max_bots", 20)) - len(live))
    if not rows or free == 0:
        return {"enabled": True, "added": 0, "free_slots": free}
    held = {r["genome_hash"] for r in conn.execute("SELECT genome_hash FROM arena_bots")}
    pairs = {json.loads(r["genome_json"])["pair"] for r in live}
    candidates = []
    for r in rows:
        try:
            g = Genome.from_dict(json.loads(r["genome_json"]))
        except (ValueError, TypeError, KeyError):
            continue
        if g.hash() in held:
            continue  # once in the arena (or retired from it), never re-admitted
        candidates.append((g, int(r["rank"]), float(r["fitness"])))
    chosen = diverse_top(candidates, free, lambda c: (c[0].pair, c[0].entry_type),
                         max_pair_share=float(acfg.get("max_pair_share", 0.3)),
                         max_family_share=float(acfg.get("max_family_share", 0.3)))
    added = 0
    for g, rank, fitness in chosen:
        name = f"{ARENA_PREFIX}{run_id[:15]}-{rank:03d}-{g.hash()[:6]}"
        conn.execute("INSERT OR IGNORE INTO arena_bots(name,genome_json,genome_hash,run_id,rank,"
                     "train_fitness,added_ms) VALUES(?,?,?,?,?,?,?)",
                     (name, g.to_json(), g.hash(), run_id, rank, fitness, now))
        added += 1
    conn.commit()
    return {"enabled": True, "added": added, "free_slots": free - added,
            "pairs_before": sorted(pairs)}


def tick(conn: sqlite3.Connection, cfg: dict, now: int | None = None,
         allow_entries: bool = True, price_source=None) -> dict:
    """Trade every active arena bot forward on its own timeframe, then retire clear losers."""
    acfg = _cfg(cfg)
    if not acfg.get("enabled", False):
        return {"enabled": False, "processed": 0, "retired": 0}
    now = int(now if now is not None else time.time() * 1000)
    genomes = active_genomes(conn)
    if not genomes:
        return {"enabled": True, "processed": 0, "retired": 0}
    result = tick_genomes(conn, cfg, genomes, now, allow_entries, price_source,
                          initial=max(5.0, float(acfg.get("allocation_usd", 20))))
    retired = retire_losers(conn, cfg, now)
    return {"enabled": True, "processed": result["processed"], "retired": retired}


def retire_losers(conn: sqlite3.Connection, cfg: dict, now: int) -> int:
    """Bury bots that are clearly losing forward, so evolution learns to avoid them."""
    acfg = _cfg(cfg)
    initial = max(5.0, float(acfg.get("allocation_usd", 20)))
    dd_limit = float(acfg.get("retire_drawdown_pct", 25)) / 100
    min_trades = int(acfg.get("retire_min_trades", 10))
    accounts = {a["name"]: a for a in account_rows(conn, arena=True)}
    gy = Graveyard(conn, cfg["graveyard"])
    retired = 0
    for r in active(conn):
        a = accounts.get(r["name"])
        if a is None:
            continue
        pnl = float(a["last_equity"]) - initial
        reason = None
        if float(a["max_drawdown"]) >= dd_limit:
            reason = f"forward drawdown {100 * float(a['max_drawdown']):.0f}% beyond {100 * dd_limit:.0f}% limit"
        elif int(a["closed_trades"]) >= min_trades and pnl < 0:
            reason = f"under water after {a['closed_trades']} forward trades (${pnl:+.2f})"
        if reason is None:
            continue
        g = Genome.from_dict(json.loads(r["genome_json"]))
        gy.bury(r["run_id"], -1, g, reason, FORWARD_DEATH, "forward", 0.0,
                float(r["train_fitness"]),
                {"forward_pnl": pnl, "forward_trades": int(a["closed_trades"]),
                 "forward_fees": float(a["fees"]), "forward_drawdown": float(a["max_drawdown"]),
                 "days": (now - int(a["started_ms"])) / 86_400_000})
        conn.execute("UPDATE arena_bots SET retired_ms=?, retire_reason=? WHERE name=?",
                     (now, reason, r["name"]))
        retired += 1
    conn.commit()
    return retired


def forward_winners(conn: sqlite3.Connection, cfg: dict) -> list[Genome]:
    """Active arena bots ahead of their starting money after enough forward trades, best first."""
    acfg = _cfg(cfg)
    if not acfg.get("enabled", False):
        return []
    initial = max(5.0, float(acfg.get("allocation_usd", 20)))
    min_trades = int(acfg.get("winner_min_trades", 5))
    accounts = {a["name"]: a for a in account_rows(conn, arena=True)}
    scored = []
    for r in active(conn):
        a = accounts.get(r["name"])
        if a is None or int(a["closed_trades"]) < min_trades or float(a["last_equity"]) <= initial:
            continue
        try:
            scored.append((float(a["last_equity"]) - initial, Genome.from_dict(json.loads(r["genome_json"]))))
        except (ValueError, TypeError, KeyError):
            continue
    scored.sort(key=lambda t: t[0], reverse=True)
    return [g for _, g in scored]


def snapshot(conn: sqlite3.Connection, cfg: dict, now: int | None = None) -> dict:
    acfg = _cfg(cfg)
    now = int(now if now is not None else time.time() * 1000)
    base = {"enabled": bool(acfg.get("enabled", False)), "real_money": False, "simulation": True,
            "allocation_usd": float(acfg.get("allocation_usd", 20)), "max_bots": int(acfg.get("max_bots", 20))}
    if not base["enabled"]:
        return {**base, "bots": [], "retired": [], "active": 0, "pnl": 0.0, "winners": 0}
    ensure_schema(conn)
    initial = base["allocation_usd"]
    accounts = {a["name"]: a for a in account_rows(conn, arena=True)}
    bots = []
    for r in active(conn):
        a = accounts.get(r["name"])
        g = Genome.from_dict(json.loads(r["genome_json"]))
        pnl = (float(a["last_equity"]) - initial) if a else 0.0
        bots.append({"name": r["name"], "pair": g.pair, "timeframe": g.timeframe,
                     "strategy": g.describe(), "run_id": r["run_id"], "train_fitness": r["train_fitness"],
                     "added_ms": r["added_ms"], "days": (now - int(r["added_ms"])) / 86_400_000,
                     "equity": float(a["last_equity"]) if a else initial, "pnl": pnl,
                     "return_pct": 100 * pnl / initial, "trades": int(a["closed_trades"]) if a else 0,
                     "wins": int(a["wins"]) if a else 0, "fees": float(a["fees"]) if a else 0.0,
                     "max_drawdown_pct": 100 * float(a["max_drawdown"]) if a else 0.0,
                     "in_position": bool(a and a["qty"] > 0), "waiting_for_data": a is None})
    bots.sort(key=lambda b: b["pnl"], reverse=True)
    retired = [dict(r) for r in conn.execute(
        "SELECT name, run_id, retired_ms, retire_reason FROM arena_bots WHERE retired_ms IS NOT NULL "
        "ORDER BY retired_ms DESC LIMIT 20")]
    n_retired = conn.execute("SELECT COUNT(*) FROM arena_bots WHERE retired_ms IS NOT NULL").fetchone()[0]
    winners = len(forward_winners(conn, cfg))
    return {**base, "bots": bots, "active": len(bots), "retired": retired, "retired_total": int(n_retired),
            "pnl": sum(b["pnl"] for b in bots), "fees": sum(b["fees"] for b in bots),
            "trades": sum(b["trades"] for b in bots), "capital": initial * len(bots),
            "winners": winners, "profitable": sum(1 for b in bots if b["pnl"] > 0)}


def text_summary(snap: dict) -> str:
    if not snap.get("enabled"):
        return "Paper arena: off."
    line = (f"Paper arena (fake money): {snap['active']} bots trading, {snap['profitable']} ahead, "
            f"{snap['winners']} forward winners seeding evolution, {snap.get('retired_total', 0)} retired; "
            f"net ${snap['pnl']:+.2f} on ${snap.get('capital', 0):.0f}, {snap.get('trades', 0)} trades.")
    if snap["bots"]:
        b = snap["bots"][0]
        line += f" Best: {b['pair']} {b['timeframe']} ${b['pnl']:+.2f} over {b['trades']} trades."
    return line
