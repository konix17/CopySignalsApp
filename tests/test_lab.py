import asyncio
import json

import pytest

from app import db, replay
from app.replay import Candles, SellLog, simulate
from app.stream import TickerStream

T0 = 1_000_000_200  # a trade opens here; candles start on the next 5-minute boundary


def candles(*rows, start=T0 - 200):
    """rows: (open, high, low, close), one per 5 minutes; the first is the candle the trade opened in."""
    c = Candles()
    for i, r in enumerate(rows):
        c.ts.append(start + i * replay.BAR_S)
        c.rows.append(r)
    return c


def trade(**kw):
    t = {"id": 1, "symbol": "ABC", "market_key": "perp:ABC", "style": "pick", "opened_at": T0, "entry_price": 100.0,
         "stop_price": 95.0, "target_price": 110.0, "hold_until": T0 + 86400, "trail_pct": None,
         "traders_at_entry": 4, "cost_pct": 0.004}
    return t | kw


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


# --- simulate ---------------------------------------------------------------------------------------

def test_the_candle_the_trade_opened_in_is_skipped():
    c = candles((100, 120, 80, 100), (100, 101, 99, 100))  # the first candle's range was before the buy
    assert simulate(trade(), c, now=c.ts[-1] + replay.BAR_S) == (None, "open", 100)


def test_a_candle_touching_stop_and_target_counts_as_the_stop():
    c = candles((100, 100, 100, 100), (100, 111, 94, 105))
    closed_at, reason, price = simulate(trade(), c, now=T0 + 10**6)
    assert reason == "stop" and price == 95.0 and closed_at == c.ts[1] + replay.BAR_S


def test_a_gap_through_the_stop_fills_at_the_open():
    c = candles((100, 100, 100, 100), (90, 91, 88, 89))
    assert simulate(trade(), c, now=T0 + 10**6)[1:] == ("stop", 90)


def test_target_fills_at_the_target():
    c = candles((100, 100, 100, 100), (100, 104, 99, 103), (103, 112, 102, 111))
    assert simulate(trade(), c, now=T0 + 10**6)[1:] == ("target", 110.0)


def test_hold_time_exits_at_the_last_close():
    c = candles((100, 100, 100, 100), (100, 103, 99, 102), (102, 104, 101, 103))
    t = trade(hold_until=c.ts[2])
    assert simulate(t, c, now=T0 + 10**6) == (c.ts[2], "time", 102)


def test_still_open_when_nothing_is_hit_yet():
    c = candles((100, 100, 100, 100), (100, 103, 99, 102))
    closed_at, reason, price = simulate(trade(), c, now=c.ts[-1] + replay.BAR_S)
    assert closed_at is None and reason == "open" and price == 102


def test_trailing_stop_follows_the_peak():
    c = candles((100, 100, 100, 100), (100, 120, 100, 119), (119, 119, 107, 108))
    t = trade(trail_pct=0.1, stop_price=90.0, target_price=200.0)
    assert simulate(t, c, now=T0 + 10**6)[1:] == ("stop", 108.0)  # 10% below the 120 peak


# --- top-trader sells ---------------------------------------------------------------------------------

def log_position(conn, first_seen, closed_at=None, reduced_at=None, address="a", exact=1, baseline=0):
    with conn:
        conn.execute("INSERT INTO position_log (source, address, market_key, direction, first_seen, exact, baseline, "
                     "last_seen, size_usd, peak_size_usd, reduced_at, closed_at) VALUES ('hl', ?, 'perp:ABC', 'long', "
                     "?, ?, ?, ?, 100, 100, ?, ?)", (address, first_seen, exact, baseline, first_seen, reduced_at, closed_at))


def test_sells_before_the_buy_dont_count(conn):
    for i in range(3):
        log_position(conn, T0 - 50_000, closed_at=T0 - 3600, address=f"old{i}")
    log_position(conn, T0 - 100_000, reduced_at=T0 + 900, address="new")  # bought over a day ago, halved after
    sells = SellLog(conn)
    assert sells.flow("perp:ABC", T0 - 86400, T0 + 1000) == (0, 4)  # the live rule's 24h window sees 4 sellers
    assert sells.flow("perp:ABC", T0, T0 + 1000) == (0, 1)


def test_sells_after_rule_exits_on_new_selling_only(conn):
    c = candles(*[(100, 101, 99, 100)] * 6)
    for i in range(3):
        log_position(conn, T0 - 50_000, closed_at=T0 - 3600, address=f"old{i}")
    sells = SellLog(conn)
    assert simulate(trade(traders_at_entry=3), c, now=c.ts[-1] + replay.BAR_S, sells=sells)[1] == "open"
    for i in range(2):
        log_position(conn, T0 - 50_000, closed_at=c.ts[3] + 10, address=f"new{i}")
    closed_at, reason, _ = simulate(trade(traders_at_entry=3), c, now=c.ts[-1] + replay.BAR_S, sells=SellLog(conn))
    assert reason == "selling" and closed_at == c.ts[3] + replay.BAR_S  # seen when the candle they sold in closes


# --- replay ---------------------------------------------------------------------------------------------

def add_trade(conn, opened_at, status="closed", reason="selling", net=-0.005, closed_at=None):
    with conn:
        conn.execute(
            "INSERT INTO pick_trades (market_key, symbol, strength, opened_at, entry_price, stop_price, target_price, "
            "hold_until, cost_pct, traders_at_entry, status, closed_at, exit_price, exit_reason, net_return, last_price, "
            "style) VALUES ('perp:ABC', 'ABC', 'Strong', ?, 100, 95, 110, ?, 0.004, 4, ?, ?, 99.9, ?, ?, 100, 'pick')",
            (opened_at, opened_at + 86400, status, closed_at or opened_at + 600, reason if status == "closed" else None,
             net if status == "closed" else None))


def test_rebuys_while_a_coin_would_still_be_held_are_skipped(conn):
    add_trade(conn, T0)
    add_trade(conn, T0 + 1200)  # re-bought 20 minutes later under the live rules
    c = candles(*[(100, 101, 99, 100)] * 12, (100, 111, 100, 110))
    res = replay.replay(conn, {"ABC": c}, now=T0 + 10**5)
    assert len(res["recorded"]) == 2
    assert [(o.trade_id, o.reason) for o in res["plan"]] == [(1, "target")]
    assert res["plan"][0].net == pytest.approx(0.1 - 0.004)


def test_report_totals_and_open_trades(conn):
    add_trade(conn, T0, net=0.02)
    add_trade(conn, T0 + 100, net=-0.01)
    add_trade(conn, T0 + 200, status="open")
    s = replay.report(replay.replay(conn, {}, now=T0 + 1000))["recorded"]["all"]
    assert s["trades"] == 2 and s["open"] == 1 and s["win_rate"] == 0.5
    assert s["total_net"] == pytest.approx(0.01)
    assert s["by_reason"]["selling"]["trades"] == 2


def test_saved_result_round_trips(conn):
    assert replay.load(conn) is None
    replay.save(conn, {"now": 1, "rules": {}, "missing": []})
    assert replay.load(conn) == {"now": 1, "rules": {}, "missing": []}


# --- price stream ---------------------------------------------------------------------------------------

def test_stream_keeps_fresh_ticker_prices_only():
    s = TickerStream("eea")
    s.handle(json.dumps({"arg": {"channel": "tickers", "instId": "BTC-USDT"},
                         "data": [{"instId": "BTC-USDT", "last": "84000.5"}]}), now=1000)
    s.handle("pong")
    s.handle(json.dumps({"event": "subscribe", "arg": {"channel": "tickers", "instId": "ETH-USDT"}}))
    assert s.prices(["BTC-USDT", "ETH-USDT"], now=1010) == {"BTC-USDT": 84000.5}
    assert s.prices(["BTC-USDT"], now=1000 + 60) == {}


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))


def test_stream_subscribes_to_changes_in_batches():
    s, ws = TickerStream("eea"), FakeWs()
    s.want([f"C{i}-USDT" for i in range(60)])
    asyncio.run(s._sync(ws))
    assert [m["op"] for m in ws.sent] == ["subscribe", "subscribe"]
    assert sum(len(m["args"]) for m in ws.sent) == 60
    s.ticks["C0-USDT"] = (1.0, 0)
    ws.sent.clear()
    s.want(["C1-USDT", "NEW-USDT"])
    asyncio.run(s._sync(ws))
    assert [m["op"] for m in ws.sent] == ["unsubscribe", "unsubscribe", "subscribe"]
    assert sum(len(m["args"]) for m in ws.sent[:2]) == 59
    assert ws.sent[2] == {"op": "subscribe", "args": [{"channel": "tickers", "instId": "NEW-USDT"}]}
    assert "C0-USDT" not in s.ticks
