"""Paper arena: admission from a run, forward trading, retirement into the graveyard, seeding."""
from __future__ import annotations

import copy
import json

from bots.config import load_config
from bots.db import connect
from bots.paper import arena, league
from bots.paper.league import ARENA_PREFIX
from bots.strategy.genome import Genome
from bots.tests.test_research_league import seed_candles
from bots.tests.test_strategy import base_genome

NOW = (1_800_000_000_000 // 3_600_000) * 3_600_000
HOUR = 3_600_000


def cfg(**over):
    c = copy.deepcopy(load_config())
    c["paper_arena"].update(over)
    return c


def fake_run(conn, run_id="run-a", n=12):
    """A saved final generation: n distinct genomes across pairs and entry types."""
    pairs = ["BTC/USD", "ETH/USD", "SOL/USD"]
    types = ["ema_cross", "rsi_reversion", "breakout", "macd"]
    for rank in range(n):
        g = base_genome(pair=pairs[rank % 3], entry_type=types[rank % 4], timeframe="1h",
                        fast_period=10 + rank, slow_period=40 + rank)
        conn.execute("INSERT INTO population(run_id,rank,genome_json,fitness,summary_json) VALUES(?,?,?,?,?)",
                     (run_id, rank, g.to_json(), 2.0 - rank * 0.1, "{}"))
    conn.commit()


def test_admission_is_diverse_capped_and_never_readmits():
    conn = connect(":memory:")
    fake_run(conn)
    out = arena.refresh_from_run(conn, cfg(max_bots=6), "run-a", NOW)
    assert out["added"] == 6
    bots = arena.active(conn)
    assert len(bots) == 6 and all(b["name"].startswith(ARENA_PREFIX) for b in bots)
    pairs = [json.loads(b["genome_json"])["pair"] for b in bots]
    assert len(set(pairs)) == 3                       # spread over coins, not six BTC bots
    assert arena.refresh_from_run(conn, cfg(max_bots=6), "run-a", NOW)["added"] == 0  # full
    conn.execute("UPDATE arena_bots SET retired_ms=?, retire_reason='x' WHERE rank=0", (NOW,))
    conn.commit()
    again = arena.refresh_from_run(conn, cfg(max_bots=6), "run-a", NOW + 1)
    assert again["added"] == 1
    assert conn.execute("SELECT COUNT(*) FROM arena_bots WHERE rank=0").fetchone()[0] == 1  # not re-admitted
    assert arena.refresh_from_run(conn, cfg(enabled=False), "run-a", NOW) == {"enabled": False, "added": 0}


def test_arena_trades_forward_and_stays_out_of_real_money_tables():
    conn = connect(":memory:")
    fake_run(conn, n=3)
    arena.refresh_from_run(conn, cfg(max_bots=3), "run-a", NOW)
    seed_candles(conn, NOW)
    first = arena.tick(conn, cfg(), NOW)
    assert first["processed"] == 0                    # seeds accounts, no retroactive fills
    assert len(league.account_rows(conn, arena=True)) == 3
    assert league.account_rows(conn, arena=False) == []  # research league untouched
    seed_candles(conn, NOW + HOUR)
    second = arena.tick(conn, cfg(), NOW + HOUR)
    assert second["processed"] == 3
    assert conn.execute("SELECT COUNT(*) FROM lab_marks").fetchone()[0] == 6
    for table in ("deployed_bots", "accounts", "candidates"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    snap = arena.snapshot(conn, cfg(), NOW + HOUR)
    assert snap["active"] == 3 and snap["real_money"] is False and snap["capital"] == 60
    assert "Paper arena (fake money): 3 bots trading" in arena.text_summary(snap)


def test_losers_are_buried_and_winners_seed_evolution():
    conn = connect(":memory:")
    fake_run(conn, n=3)
    arena.refresh_from_run(conn, cfg(max_bots=3), "run-a", NOW)
    seed_candles(conn, NOW)
    arena.tick(conn, cfg(), NOW)
    names = [b["name"] for b in arena.active(conn)]
    # Fake forward results straight into the shared paper accounts.
    conn.execute("UPDATE lab_accounts SET last_equity=14, max_drawdown=0.30 WHERE name=?", (names[0],))
    conn.execute("UPDATE lab_accounts SET last_equity=18, closed_trades=12 WHERE name=?", (names[1],))
    conn.execute("UPDATE lab_accounts SET last_equity=20.03, closed_trades=6, wins=4 WHERE name=?", (names[2],))
    conn.commit()
    retired = arena.retire_losers(conn, cfg(), NOW + HOUR)
    assert retired == 2
    rows = conn.execute("SELECT diagnosis, cause FROM graveyard ORDER BY id").fetchall()
    assert [r["diagnosis"] for r in rows] == [arena.FORWARD_DEATH] * 2
    assert "drawdown 30%" in rows[0]["cause"] and "under water after 12" in rows[1]["cause"]
    assert [b["name"] for b in arena.active(conn)] == [names[2]]
    winners = arena.forward_winners(conn, cfg())
    assert len(winners) == 1 and isinstance(winners[0], Genome)
    assert winners[0].to_json() == conn.execute("SELECT genome_json FROM arena_bots WHERE name=?",
                                                (names[2],)).fetchone()[0]
    # A cent ahead but too few trades does not count as a winner yet.
    assert arena.forward_winners(conn, cfg(winner_min_trades=7)) == []
    snap = arena.snapshot(conn, cfg(), NOW + HOUR)
    assert snap["retired_total"] == 2 and snap["winners"] == 1 and snap["profitable"] == 1


def test_evolution_seeds_from_forward_winners(synthetic_market):
    from bots.evolution.runner import EvolutionRun
    from bots.tests.test_evolution import small_cfg
    conn = connect(":memory:")
    c = small_cfg(cfg())
    c["evolution"]["carry_over_fraction"] = 0.25
    winner = base_genome(pair="ETH/USD", timeframe="1h", entry_type="ema_cross")
    arena.ensure_schema(conn)
    conn.execute("INSERT INTO arena_bots(name,genome_json,genome_hash,run_id,rank,train_fitness,added_ms) "
                 "VALUES(?,?,?,?,?,?,?)", (ARENA_PREFIX + "w", winner.to_json(), winner.hash(), "r", 0, 1.0, NOW))
    league.ensure_schema(conn)
    conn.execute("INSERT INTO lab_accounts(name,pair,genome_json,cash,peak_equity,last_equity,hold_start_price,"
                 "last_candle_ts,started_ms,closed_trades) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (ARENA_PREFIX + "w", "ETH/USD", winner.to_json(), 21, 21, 21, 100, NOW, NOW, 8))
    conn.commit()
    run = EvolutionRun(conn, c, synthetic_market, "synthetic", log=lambda _: None)
    carried = run._carried_over(24)
    assert carried and carried[0] == winner
