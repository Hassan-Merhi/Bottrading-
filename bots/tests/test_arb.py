"""Arbitrage: the arithmetic, the scanner's tolerance for bad venues, and paper fills."""
from __future__ import annotations

import pytest

from bots.arb import model
from bots.arb.paper import equity, tick as paper_tick
from bots.arb.report import snapshot, text_summary
from bots.arb.scanner import edges_from_books, fetch_books, scan, venue_symbol
from bots.db import connect

NOW = 1_800_000_000_000


def cfg(**over):
    c = {"data": {"pairs": ["BTC/USD", "ETH/USD"]},
         "backtest": {"min_order": {"BTC/USD": {"min_qty": 0.0001, "min_cost_usd": 5},
                                    "ETH/USD": {"min_qty": 0.002, "min_cost_usd": 5}}},
         "arbitrage": {"enabled": True, "size_usd": 20, "min_net_edge_pct": 0.10,
                       "slippage_pct": 0.05, "pairs": ["BTC/USD", "ETH/USD"],
                       "venues": {"kraken": {"taker_fee_pct": 0.80, "quote": "USD"},
                                  "coinbaseexchange": {"taker_fee_pct": 0.60, "quote": "USD"}},
                       "funding": {"enabled": True, "venue": "krakenfutures",
                                   "symbols": ["BTC/USD:USD"], "interval_hours": 8,
                                   "spot_taker_fee_pct": 0.80, "futures_taker_fee_pct": 0.05,
                                   "hold_days": 30}}}
    c["arbitrage"].update(over)
    return c


class FakeVenue:
    def __init__(self, books: dict, fail: bool = False):
        self.books, self.fail, self.calls = books, fail, []

    def fetch_order_book(self, symbol, limit=None):
        self.calls.append(symbol)
        if self.fail:
            raise ConnectionError("venue down")
        return self.books[symbol]


class FakeFutures:
    def __init__(self, rate):
        self.rate = rate

    def fetch_funding_rates(self, symbols):
        return {s: {"fundingRate": self.rate} for s in symbols}


def book(mid, spread=0.0002, size=5.0):
    return {"bids": [[mid * (1 - spread / 2), size], [mid * (1 - spread), size]],
            "asks": [[mid * (1 + spread / 2), size], [mid * (1 + spread), size]]}


# -- arithmetic -------------------------------------------------------------

def test_walk_book_averages_levels_and_refuses_thin_books():
    assert model.walk_book([[100, 0.5], [101, 0.5]], 0.75) == pytest.approx(100.3333333)
    assert model.walk_book([[100, 0.5]], 1.0) is None
    assert model.walk_book([], 1.0) is None
    with pytest.raises(ValueError):
        model.walk_book([[100, 1]], 0)


def test_retail_fees_eat_a_one_percent_gap():
    e = model.cross_edge(100.0, 101.0, 1.0, 0.008, 0.006, 0.0005)
    assert e.gross_pct == pytest.approx(0.01)
    assert e.net_usd < 0 and not e.profitable
    assert model.breakeven_gross_pct(0.008, 0.006, 0.0005) == pytest.approx(0.0151, abs=1e-4)
    # The same gap at low fees clears.
    assert model.cross_edge(100.0, 101.0, 1.0, 0.001, 0.001, 0.0005).profitable
    assert model.breakeven_gross_pct(0.001, 0.001, 0.0005) == pytest.approx(0.0030, abs=1e-4)


def test_cross_edge_rejects_garbage():
    for bad in ((0, 1, 1), (1, float("nan"), 1), (1, 1, -1)):
        with pytest.raises(ValueError):
            model.cross_edge(*bad, 0.001, 0.001)
    with pytest.raises(ValueError):
        model.cross_edge(1, 1, 1, 0.6, 0.0)


def test_funding_carry_needs_a_long_hold_to_pay_four_fills():
    c = model.funding_carry(0.0001, 8, 0.008, 0.0005, 0.0005, hold_days=30)
    assert c.annual_gross_pct == pytest.approx(0.1096, abs=1e-3)
    assert c.round_trip_cost_pct == pytest.approx(0.019)
    assert c.annual_net_pct < 0 and not c.worthwhile
    assert c.breakeven_days == pytest.approx(63.3, abs=0.1)
    long_hold = model.funding_carry(0.0001, 8, 0.008, 0.0005, 0.0005, hold_days=365)
    assert long_hold.worthwhile
    negative = model.funding_carry(-0.0002, 8, 0.001, 0.0005)
    assert negative.annual_gross_pct < 0 and negative.breakeven_days is None


# -- scanner ----------------------------------------------------------------

def test_venue_symbol_maps_quote():
    assert venue_symbol("BTC/USD", {"quote": "USDT"}) == "BTC/USDT"
    assert venue_symbol("BTC/USD", {}) == "BTC/USD"


def test_scan_records_every_direction_and_survives_a_dead_venue():
    conn = connect(":memory:")
    venues = {"kraken": FakeVenue({"BTC/USD": book(50_000), "ETH/USD": book(3_000)}),
              "coinbaseexchange": FakeVenue({"BTC/USD": book(50_050), "ETH/USD": book(3_000)})}
    r = scan(conn, cfg(), venues, NOW, futures_api=FakeFutures(0.0001))
    assert r["enabled"] and r["observations"] == 4 and r["errors"] == {}
    rows = conn.execute("SELECT pair, buy_venue, sell_venue, gross_pct, net_pct FROM arb_observations "
                        "ORDER BY net_pct DESC").fetchall()
    assert len(rows) == 4
    best = rows[0]
    assert (best["pair"], best["buy_venue"], best["sell_venue"]) == ("BTC/USD", "kraken", "coinbaseexchange")
    assert 0 < best["gross_pct"] < 0.002 and best["net_pct"] < 0   # 0.1% gap, 1.4% of fees
    assert conn.execute("SELECT COUNT(*) FROM arb_funding").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM arb_scan_log WHERE ok=1").fetchone()[0] == 2

    venues["coinbaseexchange"].fail = True
    r2 = scan(conn, cfg(), venues, NOW + 60_000, futures_api=None)
    assert r2["observations"] == 0 and "coinbaseexchange" in r2["errors"]
    assert conn.execute("SELECT ok FROM arb_scan_log WHERE venue='coinbaseexchange' AND ts=?",
                        (NOW + 60_000,)).fetchone()[0] == 0


def test_scan_skips_crossed_or_thin_books():
    crossed = {"bids": [[101, 1]], "asks": [[100, 1]]}
    thin = {"bids": [[50_000, 0.00001]], "asks": [[50_001, 0.00001]]}
    books, errors = fetch_books({"kraken": FakeVenue({"BTC/USD": crossed, "ETH/USD": book(3000)}),
                                 "coinbaseexchange": FakeVenue({"BTC/USD": thin, "ETH/USD": book(3000)})},
                                ["BTC/USD", "ETH/USD"], cfg()["arbitrage"])
    assert ("kraken", "BTC/USD") not in books and errors == {}
    edges = edges_from_books(books, ["BTC/USD", "ETH/USD"], cfg()["arbitrage"])
    assert {e["pair"] for e in edges} == {"ETH/USD"}


def test_scan_disabled_is_a_noop():
    conn = connect(":memory:")
    assert scan(conn, cfg(enabled=False), {}, NOW) == {"enabled": False, "observations": 0}
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='arb_observations'").fetchone()


# -- paper arbitrage ----------------------------------------------------------

def low_fee_cfg(**over):
    c = cfg(**over)
    c["arbitrage"]["venues"] = {"kraken": {"taker_fee_pct": 0.10, "quote": "USD"},
                                "coinbaseexchange": {"taker_fee_pct": 0.10, "quote": "USD"}}
    return c


def test_paper_trade_only_when_edge_clears_and_inventory_allows():
    conn = connect(":memory:")
    c = low_fee_cfg()
    # 1% gap, 0.1% fees: clears the 0.10% minimum edge.
    venues = {"kraken": FakeVenue({"BTC/USD": book(50_000, size=5), "ETH/USD": book(3_000)}),
              "coinbaseexchange": FakeVenue({"BTC/USD": book(50_500, size=5), "ETH/USD": book(3_000)})}
    r = scan(conn, c, venues, NOW)
    p = paper_tick(conn, c, r["edges"], NOW)
    assert p["trades"] == 1
    t = conn.execute("SELECT * FROM arb_paper_trades").fetchone()
    assert t["pair"] == "BTC/USD" and t["buy_venue"] == "kraken" and t["sell_venue"] == "coinbaseexchange"
    assert t["net_usd"] > 0 and t["fees"] > 0
    kr = conn.execute("SELECT cash, qty, start_cash, start_qty FROM arb_paper_books WHERE venue='kraken' AND pair='BTC/USD'").fetchone()
    cb = conn.execute("SELECT cash, qty FROM arb_paper_books WHERE venue='coinbaseexchange' AND pair='BTC/USD'").fetchone()
    assert kr["cash"] < kr["start_cash"] and kr["qty"] > kr["start_qty"]
    assert cb["cash"] > 60 and cb["qty"] < kr["start_qty"]
    eq = equity(conn, {"BTC/USD": 50_250.0, "ETH/USD": 3_000.0})
    assert eq["arbitrage_pnl"] == pytest.approx(t["net_usd"], rel=1e-6)

    # Same gap again and again: inventory on the sell side runs out after a few trades.
    trades = 1
    for k in range(1, 12):
        r = scan(conn, c, venues, NOW + k * 60_000)
        trades += paper_tick(conn, c, r["edges"], NOW + k * 60_000)["trades"]
    cb = conn.execute("SELECT qty FROM arb_paper_books WHERE venue='coinbaseexchange' AND pair='BTC/USD'").fetchone()
    assert 2 <= trades < 12 and cb["qty"] >= 0  # buffer used up, never negative
    # The paper ledger never touches the strategy bots' accounts.
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_paper_does_not_trade_below_min_edge_or_when_paused():
    conn = connect(":memory:")
    c = low_fee_cfg(min_net_edge_pct=5.0)
    venues = {"kraken": FakeVenue({"BTC/USD": book(50_000), "ETH/USD": book(3_000)}),
              "coinbaseexchange": FakeVenue({"BTC/USD": book(50_500), "ETH/USD": book(3_000)})}
    r = scan(conn, c, venues, NOW)
    assert paper_tick(conn, c, r["edges"], NOW)["trades"] == 0
    c = low_fee_cfg()
    assert paper_tick(conn, c, r["edges"], NOW, allow_entries=False)["trades"] == 0
    assert conn.execute("SELECT COUNT(*) FROM arb_paper_books").fetchone()[0] == 4  # seeded, idle


def test_paper_never_trades_at_retail_fees_on_realistic_gaps():
    conn = connect(":memory:")
    c = cfg()  # 0.80% / 0.60%
    venues = {"kraken": FakeVenue({"BTC/USD": book(50_000), "ETH/USD": book(3_000)}),
              "coinbaseexchange": FakeVenue({"BTC/USD": book(50_400), "ETH/USD": book(3_010)})}
    r = scan(conn, c, venues, NOW)
    assert all(e["net_usd"] < 0 for e in r["edges"])
    assert paper_tick(conn, c, r["edges"], NOW)["trades"] == 0


# -- report ----------------------------------------------------------------

def test_snapshot_and_text_on_empty_and_populated_db():
    conn = connect(":memory:")
    empty = snapshot(conn, cfg(), NOW)
    assert empty["enabled"] and empty["observations"] == 0 and empty["paper"] is None
    assert "No order books" in text_summary(empty)
    assert text_summary(snapshot(conn, cfg(enabled=False), NOW)).endswith("off.")
    venues = {"kraken": FakeVenue({"BTC/USD": book(50_000), "ETH/USD": book(3_000)}),
              "coinbaseexchange": FakeVenue({"BTC/USD": book(50_050), "ETH/USD": book(3_000)})}
    r = scan(conn, cfg(), venues, NOW, futures_api=FakeFutures(0.0001))
    paper_tick(conn, cfg(), r["edges"], NOW)
    s = snapshot(conn, cfg(), NOW + 1000)
    assert s["observations"] == 4 and s["pairs"][0]["pair"] == "BTC/USD"
    assert s["pairs"][0]["positive_share_pct"] == 0.0
    assert s["paper"]["trades"] == 0 and len(s["paper"]["inventory"]) == 4
    assert s["funding"][0]["symbol"] == "BTC/USD:USD"
    txt = text_summary(s)
    assert "Funding BTC/USD:USD" in txt and "Paper arbitrage: 0 trades" in txt
