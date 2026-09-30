import sqlite3

import pytest

from app import activity, db, portfolio, swing, tracker, users
from app.accounts import average_cost
from app.market import Market, round_trip_cost, trend_from_closes
from app.models import PERP, Position, TraderStat
from app.scoring import max_drawdown, percentile_ranks, score_stats, top_traders
from app.signals import meaningful_positions
from app.sources.gmx import market_symbol, window_starts
from app.sources.hyperliquid import normalize_coin
from app.symbols import crypto_symbol


def stat(addr, window="week", pnl=100.0, roi=0.1, source="hl", **kw):
    return TraderStat(source=source, address=addr, window=window, pnl=pnl, roi=roi, **kw)


def trader(addr, pnl=100.0, roi=0.1, **kw):
    """A trader profitable in week and month (eligible), scored on week."""
    return [stat(addr, "week", pnl, roi, **kw), stat(addr, "month", 1.0, 0.01)]


def pos(addr, key="perp:BTC", direction="long", size=1000.0, entry=100.0, mark=110.0, source="hl", opened_at=None):
    return Position(source=source, address=addr, market_key=key, asset_class=PERP, symbol=key.split(":")[1],
                    title=key, direction=direction, size_usd=size, entry_price=entry, mark_price=mark, price_key=key,
                    opened_at=opened_at)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    return db.connect(tmp_path / "t.db")


# --- scoring ---------------------------------------------------------------

def test_percentile_ranks_handles_ties_and_edges():
    assert percentile_ranks([]) == []
    assert percentile_ranks([5]) == [1.0]
    assert percentile_ranks([1, 2, 3]) == [0.0, 0.5, 1.0]
    assert percentile_ranks([1, 1, 3]) == [0.25, 0.25, 1.0]


def test_losing_traders_score_zero_and_best_ranks_first():
    stats = trader("a", pnl=1000, roi=0.5) + trader("b", pnl=500, roi=0.2) + trader("c", pnl=-50, roi=-0.1)
    score_stats(stats)
    by = {s.address: s.score for s in stats if s.window == "week"}
    assert by["c"] == 0
    assert by["a"] > by["b"] > 0


def test_consistency_rewards_profit_across_windows():
    stats = trader("a") + trader("b") + [stat("a", "day", 10), stat("a", "allTime", 10),
                                         stat("b", "day", -10), stat("b", "allTime", -10)]
    score_stats(stats)
    week = {s.address: s.score for s in stats if s.window == "week"}
    assert week["a"] > week["b"] > 0


def test_one_streak_wonders_bots_and_reckless_traders_are_excluded():
    streak = [stat("streak", "week", 5000, 2.0), stat("streak", "month", -100, -0.1)]
    bot = trader("bot", pnl=100, roi=0.1)
    bot[1] = stat("bot", "month", 10, 0.01, volume=5e8, account_value=1e6)  # 500x turnover, 1% return
    reckless = trader("reckless", pnl=900, roi=0.9)
    fine = trader("fine")
    stats = streak + bot + reckless + fine
    score_stats(stats, drawdowns={"reckless": 0.6})
    week = {s.address: s.score for s in stats if s.window == "week"}
    assert week["streak"] == week["bot"] == week["reckless"] == 0
    assert week["fine"] > 0


def test_drawdown_lowers_score_and_roi_outliers_are_ignored():
    a, b = trader("a", pnl=100, roi=0.1), trader("b", pnl=100, roi=0.1)
    score_stats(a + b, drawdowns={"b": 0.3})
    assert a[0].score > b[0].score
    x, y = trader("x", pnl=100, roi=60.0), trader("y", pnl=200, roi=0.5)  # 6000% in a week: deposit artifact
    score_stats(x + y)
    assert y[0].score > x[0].score


def test_max_drawdown_ignores_deposits():
    # PnL rises to 50, falls to 10 (a 40 drop) while the balance is ~100 -> 40%.
    assert max_drawdown([0, 50, 10, 30], [100, 150, 100, 1000]) == pytest.approx(40 / 125)
    assert max_drawdown([0, 10, 20], [100, 100, 100]) == 0


def test_top_traders_limits_per_window():
    stats = [x for i in range(10) for x in trader(str(i), pnl=i + 1)]
    score_stats(stats)
    top = top_traders(stats, 3)
    assert [s.address for s in top["week"]] == ["9", "8", "7"]
    assert top["day"] == []


# --- signals ---------------------------------------------------------------

def test_dust_positions_are_ignored():
    positions = [pos("a", size=10_000), pos("a", key="perp:ETH", size=10)]
    assert [p.market_key for p in meaningful_positions(positions)] == ["perp:BTC"]


# --- symbols ---------------------------------------------------------------

def test_only_crypto_passes():
    assert crypto_symbol("BTC") == "BTC"
    assert crypto_symbol("xyz:TSLA") is None
    assert crypto_symbol("GOLD") is None and crypto_symbol("XAUT.v2") is None
    assert crypto_symbol("APE_deprecated (deprecated)") is None
    assert crypto_symbol("SPX6900") == "SPX6900"  # a memecoin, not the index
    assert normalize_coin("kPEPE") == ("PEPE", 0.001)
    assert normalize_coin("xyz:NVDA") == (None, 1.0)
    assert market_symbol("ETH/USD [WETH-USDC]") == "ETH"
    assert market_symbol("SPY/USD [USDC-USDC]") is None
    assert market_symbol("SWAP-ONLY [USDC-USDT]") is None


def test_gmx_windows_start_at_midnight():
    starts = window_starts(1_790_334_674)
    assert all(v % 86400 == 0 for v in starts.values()) and starts["allTime"] == 0


def test_trend_from_closes():
    rising = [100 + i for i in range(60)]
    ma20, ma50, close30, vol = trend_from_closes(rising)
    assert ma20 == pytest.approx(149.5) and ma50 == pytest.approx(134.5) and close30 == 130
    assert vol is not None and vol < 0.02
    assert trend_from_closes([100.0] * 5) == (None, None, None, None)


# --- activity --------------------------------------------------------------

def _follow(conn, *addresses, source="hl"):
    db.replace_source(conn, source, [stat(a, source=source) for a in addresses], set(), [], ts=0)


def _log(conn):
    return {r["address"]: r for r in conn.execute("SELECT * FROM position_log")}


def test_position_log_tracks_entries_and_exits(conn):
    _follow(conn, "a", "b")
    stale = 1800
    # First sight: existing positions are baseline (open time unknown), not fresh buys.
    activity.sync_position_log(conn, "hl", [pos("a")], {"a", "b"}, ts=1000, stale_after_s=stale)
    assert _log(conn)["a"]["baseline"] == 1
    # b opens a position while watched: a fresh buy. a closes it.
    activity.sync_position_log(conn, "hl", [pos("b")], {"a", "b"}, ts=1600, stale_after_s=stale)
    log = _log(conn)
    assert log["b"]["baseline"] == 0 and log["b"]["first_seen"] == 1600 and log["b"]["closed_at"] is None
    assert log["a"]["closed_at"] == 1600


def test_failed_fetch_is_not_a_sell(conn):
    _follow(conn, "a")
    activity.sync_position_log(conn, "hl", [pos("a")], {"a"}, ts=1000, stale_after_s=1800)
    activity.sync_position_log(conn, "hl", [], set(), ts=1600, stale_after_s=1800)  # a's request failed
    assert _log(conn)["a"]["closed_at"] is None


def test_cutting_half_the_position_counts_as_selling(conn):
    _follow(conn, "a")
    activity.sync_position_log(conn, "hl", [pos("a", size=1000)], {"a"}, ts=1000, stale_after_s=1800)
    activity.sync_position_log(conn, "hl", [pos("a", size=600)], {"a"}, ts=1300, stale_after_s=1800)
    assert _log(conn)["a"]["reduced_at"] is None
    activity.sync_position_log(conn, "hl", [pos("a", size=400)], {"a"}, ts=1600, stale_after_s=1800)
    assert _log(conn)["a"]["reduced_at"] == 1600


# --- markets ---------------------------------------------------------------

def mkt(coin="BTC", price=100.0, uptrend=True, volume=1e9, spread_bps=1, vol=0.03):
    half = price * spread_bps / 20000
    return Market(coin, f"{coin}-USDT", price, price - half, price + half, volume,
                  ma20=price * (0.95 if uptrend else 1.05), ma50=price * (0.9 if uptrend else 1.1),
                  ret30=0.2 if uptrend else -0.2, daily_vol=vol)


def test_round_trip_cost_includes_the_spread():
    assert round_trip_cost(mkt(spread_bps=10), 0.001, 0.0005) > round_trip_cost(mkt(spread_bps=1), 0.001, 0.0005)
    assert round_trip_cost(mkt(spread_bps=0), 0.002, 0.0005) == pytest.approx(0.005)


# --- portfolio -------------------------------------------------------------

def _open(conn, pick=None, entry=100.0):
    pid = portfolio.open_position(conn, user_id=1, market_key="perp:BTC", symbol="BTC", entry_price=entry, size_usd=100,
                                  pick=pick, now=0)
    return conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()


def _hold(conn, address, first_seen, key="perp:SOL", closed_at=None, reduced_at=None, score=1.0, entry=100.0):
    """A followed trader's long, as the position log and the positions table record it."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO trader_stats (source, address, window, score, followed, updated_at) "
                     "VALUES ('hl', ?, 'week', ?, 1, 0)", (address, score))
        cur = conn.execute("INSERT INTO position_log (source, address, market_key, direction, first_seen, exact, baseline, "
                           "last_seen, size_usd, peak_size_usd, reduced_at, closed_at) VALUES ('hl', ?, ?, 'long', ?, 1, 0, ?, "
                           "1000, 1000, ?, ?)", (address, key, first_seen, first_seen, reduced_at, closed_at))
        conn.execute("INSERT INTO positions (source, address, market_key, asset_class, symbol, title, direction, size_usd, "
                     "entry_price, mark_price, price_key, updated_at) VALUES ('hl', ?, ?, 'perp', ?, ?, 'long', 1000, ?, "
                     "100, ?, 0)", (address, key, key.split(":")[1], key, entry, key))
    return cur.lastrowid


def _copy(conn, coin="BTC", price=100.0, now=13 * 3600):
    """A swing copy of trader a's long in `coin`, held 13 hours at `now`."""
    _hold(conn, "a", first_seen=now - 13 * 3600, key=f"perp:{coin}")
    return swing.candidates(conn, {coin: mkt(coin, price=price)}, {("hl", "a"): 1.0}, now, 1000, 0.002, 0.0005, 1e6, 0.01)[0]


def _trader_sells(conn, when, address="a"):
    with conn:
        conn.execute("UPDATE position_log SET closed_at = ? WHERE address = ?", (when, address))


def test_manual_position_sells_at_its_stop_and_target(conn):
    p = _open(conn)  # no copy: the default plan, -10% / +20%
    assert portfolio.advice_of(portfolio.evaluate(p, 101, now=1)) == "HOLD"
    assert portfolio.advice_of(portfolio.evaluate(p, p["stop_price"] * 0.99, now=1)) == "SELL"
    assert portfolio.advice_of(portfolio.evaluate(p, p["target_price"] * 1.01, now=1)) == "TAKE_PROFIT"
    assert portfolio.advice_of(portfolio.evaluate(p, 101, now=p["hold_until"])) == "WATCH"


def test_copy_has_no_target_and_sells_when_the_trader_sells(conn):
    p = _open(conn, _copy(conn))
    assert p["style"] == "copy" and p["stop_price"] == pytest.approx(75)
    assert portfolio.advice_of(portfolio.evaluate(p, 300, now=1, copy_holding=True)) == "HOLD"  # no take-profit
    assert portfolio.advice_of(portfolio.evaluate(p, 74, now=1, copy_holding=True)) == "SELL"  # safety stop
    f = portfolio.evaluate(p, 101, now=1, copy_holding=False)
    assert {x.kind for x in f} == {"trader_closed"} and portfolio.advice_of(f) == "SELL"
    assert portfolio.copy_holding(conn, p) is True
    _trader_sells(conn, 5)
    assert portfolio.copy_holding(conn, p) is False


def test_alerts_fire_once_per_reason(conn):
    _open(conn, _copy(conn))
    _trader_sells(conn, 5)
    markets = {"BTC": mkt("BTC", price=101)}
    first = portfolio.update_positions(conn, markets, now=10)
    again = portfolio.update_positions(conn, markets, now=20)
    assert [a["kind"] for a in first] == ["trader_closed"] and not again
    assert conn.execute("SELECT advice FROM my_positions").fetchone()[0] == "SELL"


# --- tracker ---------------------------------------------------------------

def _track(conn, price_path, hold_days=None, sells_at=None):
    """Open a paper trade for a BTC copy at 100, then replay prices every 10 minutes; returns the closed row or None.
    `sells_at`: the step at which the copied trader closes."""
    p = _copy(conn, now=0)
    if hold_days is not None:
        p.hold_days = hold_days
    tracker.open_trades(conn, [p], {"BTC": mkt("BTC", price=100)}, now=0)
    for i, px in enumerate(price_path, 1):
        if i == sells_at:
            _trader_sells(conn, i * 600)
        tracker.update_trades(conn, {"BTC": mkt("BTC", price=px)}, now=i * 600)
    return conn.execute("SELECT * FROM pick_trades WHERE status = 'closed'").fetchone()


def test_copy_opens_once(conn):
    p = _copy(conn, now=0)
    assert tracker.open_trades(conn, [p], {"BTC": mkt()}, now=0) == 1
    assert tracker.open_trades(conn, [p], {"BTC": mkt()}, now=600) == 0


def test_stop_exit_counts_the_loss_net_of_costs(conn):
    t = _track(conn, [99, 80, 60])  # gaps through the 25% safety stop
    assert t["exit_reason"] == "stop" and t["exit_price"] == 60
    assert t["net_return"] == pytest.approx(-0.40 - t["cost_pct"])


def test_copy_rides_past_any_target_until_the_trader_sells(conn):
    t = _track(conn, [150, 250, 300], sells_at=3)
    assert t["exit_reason"] == "trader_closed" and t["closed_at"] == 1800
    assert t["exit_price"] == 300 and t["net_return"] == pytest.approx(2.0 - t["cost_pct"])


def test_hold_time_exit_and_btc_benchmark(conn):
    t = _track(conn, [101, 102], hold_days=0.01)
    assert t["exit_reason"] == "time"
    assert t["btc_return"] is not None


def test_performance_counts_only_copies(conn):
    _track(conn, [150], sells_at=1)
    with conn:  # a paper trade of a strategy that was dropped
        conn.execute("INSERT INTO pick_trades (market_key, symbol, strength, opened_at, entry_price, stop_price, "
                     "target_price, hold_until, cost_pct, status, net_return, style) VALUES "
                     "('perp:ETH', 'ETH', 'Strong', 0, 1, 1, 1, 1, 0, 'closed', -0.5, 'pick')")
    perf = tracker.performance(conn, {})
    assert perf["overall"]["trades"] == 1 and perf["overall"]["win_rate"] == 1.0
    assert [t["symbol"] for t in perf["recent"]] == ["BTC"]


# --- binance account -------------------------------------------------------

def trade(buy, qty, price, t, fee=0.0, fee_asset="BNB"):
    return {"is_buyer": buy, "qty": qty, "price": price, "time": t * 1000, "commission": fee, "commission_asset": fee_asset}


def test_average_cost_tracks_buys_sells_and_fees():
    trades = [trade(True, 10, 1.0, 100), trade(True, 10, 2.0, 200), trade(False, 5, 3.0, 300)]
    qty, avg, started = average_cost(trades, "DOGE")
    assert qty == pytest.approx(15) and avg == pytest.approx(1.5) and started == 100
    # Fee paid in the coin itself reduces the amount held.
    qty, _, _ = average_cost([trade(True, 10, 1.0, 100, fee=0.01, fee_asset="DOGE")], "DOGE")
    assert qty == pytest.approx(9.99)


def test_average_cost_restarts_after_full_exit():
    trades = [trade(True, 10, 1.0, 100), trade(False, 10, 2.0, 200), trade(True, 4, 5.0, 300)]
    qty, avg, started = average_cost(trades, "DOGE")
    assert (qty, avg, started) == (pytest.approx(4), pytest.approx(5.0), 300)


import asyncio  # noqa: E402

import httpx  # noqa: E402

from app.accounts import OkxAccount, UnsafeKeyError  # noqa: E402


def okx_prices(prices: dict[str, float]):
    """A fake OKX tickers endpoint returning `prices` by pair."""
    def handler(request):
        assert request.url.path == "/api/v5/market/tickers"
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [
            {"instId": pair, "last": str(px)} for pair, px in prices.items()]})
    return handler


def test_empty_okx_account_falls_back_to_manual_bankroll(conn):
    import json as _json

    from app.config import Settings
    from app.pipeline import bankroll_info

    users.set_setting(conn, 1, "bankroll", 750)
    users.set_setting(conn, 1, "account_status", {"ok": True, "synced_at": 1000, "total_usd": 0, "fee_rate": 0.00075})
    b = bankroll_info(conn, Settings(), now=1100, user_id=1)
    assert {k: b[k] for k in ("amount", "source", "fee_rate", "spot_empty")} == \
        {"amount": 750.0, "source": "manual", "fee_rate": 0.00075, "spot_empty": True}
    users.set_setting(conn, 1, "account_status", {"ok": True, "synced_at": 1000, "total_usd": 2500, "fee_rate": 0.00075})
    assert bankroll_info(conn, Settings(), now=1100, user_id=1)["source"] == "exchange"


# --- demo trades -----------------------------------------------------------

def _demo(conn, hold_days=None, size=100.0):
    p = _copy(conn)
    if hold_days is not None:
        p.hold_days = hold_days
    pid = portfolio.open_position(conn, user_id=1, market_key=p.market_key, symbol=p.symbol, entry_price=100, size_usd=size,
                                  pick=p, now=0, source="demo", btc_entry=100)
    return pid, p


def _settle(conn, price, now):
    return portfolio.settle_demo_trades(conn, {"BTC": mkt("BTC", price=price)}, now)


def test_demo_trade_runs_until_the_trader_sells(conn):
    pid, p = _demo(conn)
    assert _settle(conn, 101, now=3600) == []  # still running
    assert _settle(conn, 250, now=4000) == []  # copies have no target
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "open" and row["last_price"] == 250

    _trader_sells(conn, 7000)
    done = _settle(conn, 200, now=7200)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["exit_reason"] == "trader_closed"
    assert row["net_return"] == pytest.approx(1.0 - p.cost_pct)
    assert done[0]["level"] == "success" and "successful" in done[0]["message"] and "the copied trader sold" in done[0]["message"]


def test_demo_trade_stop_is_unsuccessful(conn):
    pid, _ = _demo(conn)
    done = _settle(conn, 50, now=600)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["exit_reason"] == "stop" and row["net_return"] < 0
    assert done[0]["level"] == "danger" and "unsuccessful" in done[0]["message"]


def test_demo_trade_ends_when_hold_time_is_over(conn):
    pid, _ = _demo(conn, hold_days=1)
    _settle(conn, 102, now=86400 + 1)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["exit_reason"] == "time" and row["net_return"] == pytest.approx(0.02 - row["cost_pct"])
    assert row["btc_return"] == pytest.approx(0.02)


def test_demo_trades_stay_out_of_the_real_portfolio(conn):
    _demo(conn, size=500)
    _trader_sells(conn, 5)
    assert portfolio.update_positions(conn, {"BTC": mkt("BTC")}, now=10) == []
    fake = FakeOkx()
    fake.balance = [{"ccy": "USDT", "cashBal": "10", "availBal": "10", "frozenBal": "0"}]
    _okx_sync(conn, fake, {"BTC": mkt("BTC")}, now=2000)
    row = conn.execute("SELECT * FROM my_positions WHERE source = 'demo'").fetchone()
    assert row["status"] == "open" and row["exchange_check"] is None


def test_demo_account_math(conn):
    pid1, p = _demo(conn, size=1000)          # open, entry 100
    pid2, _ = _demo(conn, size=500)
    with conn:  # second one finished +10% after fees
        conn.execute("UPDATE my_positions SET status = 'closed', net_return = 0.10, symbol = 'ETH' WHERE id = ?", (pid2,))
        conn.execute("UPDATE my_positions SET last_price = 105 WHERE id = ?", (pid1,))
    a = portfolio.demo_account(conn, 1, 10_000)
    cost = p.cost_pct
    assert a["invested"] == 1000 and a["realized"] == pytest.approx(50)
    assert a["cash"] == pytest.approx(10_000 - 1000 + 50)
    assert a["open_result"] == pytest.approx(1000 * (0.05 - cost))
    assert a["value"] == pytest.approx(a["cash"] + 1000 + a["open_result"])
    assert a["best_case"] is None  # copies have no target
    assert a["worst_case"] == pytest.approx(1000 * (p.stop_pct - cost))
    assert (a["finished"], a["successful"], a["running"]) == (1, 1, 1)


def _pipeline(conn, price=100.0):
    from app.config import Settings
    from app.pipeline import MarketView, Pipeline

    pipe = Pipeline(conn, Settings())
    pipe.last_view = MarketView({}, {"BTC": mkt("BTC", price=price)}, {}, [])
    return pipe


def _live_tick(pipe, btc_price):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(okx_prices({"BTC-USDT": btc_price}))) as client:
            await pipe.live_tick(client)
    asyncio.run(run())


def test_live_tick_closes_demo_trade_at_the_stop_within_seconds(conn):
    pid, p = _demo(conn)
    pipe = _pipeline(conn)
    _live_tick(pipe, p.stop_price * 0.99)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["exit_reason"] == "stop" and pipe.live_at


def test_real_account_and_results(conn):
    p = _copy(conn)
    pid = portfolio.open_position(conn, user_id=1, market_key="perp:BTC", symbol="BTC", entry_price=100, size_usd=1000, pick=p,
                                  now=0, btc_entry=100)
    with conn:
        conn.execute("UPDATE my_positions SET last_price = 110 WHERE id = ?", (pid,))
    a = portfolio.real_account(conn, 1, {"ok": True, "cash_usd": 500})
    assert a["connected"] and a["invested"] == 1000 and a["market_value"] == pytest.approx(1100)
    assert a["value"] == pytest.approx(500) and a["outside_exchange"] == pytest.approx(1100)  # entered by hand
    with conn:
        conn.execute("UPDATE my_positions SET exchange_check = 'ok' WHERE id = ?", (pid,))
    assert portfolio.real_account(conn, 1, {"ok": True, "cash_usd": 500})["value"] == pytest.approx(1600)
    assert a["open_result"] == pytest.approx(1000 * (0.10 - p.cost_pct))
    assert a["best_case"] is None and a["worst_case"] < 0
    manual = portfolio.open_position(conn, user_id=1, market_key="perp:ETH", symbol="ETH", entry_price=100, size_usd=100,
                                     pick=None, now=0)
    assert portfolio.real_account(conn, 1, None)["best_case"] == pytest.approx(100 * 0.20)  # the default +20% target
    with conn:
        conn.execute("DELETE FROM my_positions WHERE id = ?", (manual,))
    # "I sold": estimated fees, BTC comparison
    portfolio.close_position(conn, pid, 120, now=10, reason="sold", btc_price=110)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["net_return"] == pytest.approx(0.20 - p.cost_pct) and row["btc_return"] == pytest.approx(0.10)
    a = portfolio.real_account(conn, 1, None)
    assert (a["finished"], a["successful"], a["running"]) == (1, 1, 0)


def test_live_tick_alerts_real_positions_without_selling(conn):
    pid = portfolio.open_position(conn, user_id=1, market_key="perp:BTC", symbol="BTC", entry_price=100, size_usd=100,
                                  pick=None, now=0)
    stop = conn.execute("SELECT stop_price FROM my_positions WHERE id = ?", (pid,)).fetchone()[0]
    _live_tick(_pipeline(conn), stop * 0.99)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "open" and row["advice"] == "SELL" and row["last_price"] == pytest.approx(stop * 0.99)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM alerts WHERE position_id = ?", (pid,))}
    assert "stop" in kinds


# --- OKX ---------------------------------------------------------------------

class FakeOkx:
    """Just enough of OKX's v5 API for a read-only sync."""

    def __init__(self, perm="read_only"):
        self.perm = perm
        self.balance = [{"ccy": "USDT", "cashBal": "300", "availBal": "300", "frozenBal": "0"},
                        {"ccy": "SOL", "cashBal": "2", "availBal": "0", "frozenBal": "2"}]
        self.fills = [{"instId": "SOL-USDT", "billId": "1001", "ordId": "9", "fillPx": "100", "fillSz": "2.002",
                       "side": "buy", "fee": "-0.002", "feeCcy": "SOL", "ts": "1000000"}]
        self.algos = {"conditional,oco": [{"instId": "SOL-USDT", "algoId": "a1", "ordType": "oco", "side": "sell",
                                           "sz": "2", "slTriggerPx": "90", "tpTriggerPx": "130", "cTime": "1000500"}],
                      "move_order_stop": []}

    def handler(self, request: httpx.Request) -> httpx.Response:
        h = request.headers
        assert request.url.host == "eea.okx.com"  # European accounts' keys only work on the EEA API domain
        assert h["OK-ACCESS-KEY"] == "key" and h["OK-ACCESS-PASSPHRASE"] == "pass" and h["OK-ACCESS-SIGN"]
        path, q = request.url.path, request.url.params
        data = {
            "/api/v5/account/config": [{"perm": self.perm}],
            "/api/v5/account/balance": [{"details": self.balance}],
            "/api/v5/trade/orders-pending": [],
            "/api/v5/asset/balances": [{"ccy": "USDT", "bal": "50"}],
            "/api/v5/finance/savings/balance": [],
            "/api/v5/account/trade-fee": [{"taker": "-0.0008", "maker": "-0.0006"}],
        }.get(path)
        if path == "/api/v5/trade/orders-algo-pending":
            data = self.algos[q["ordType"]]
        if path == "/api/v5/trade/fills-history":
            before = q.get("before")
            data = [f for f in self.fills if not before or int(f["billId"]) > int(before)]
        if data is None:
            return httpx.Response(404, json={"code": "1", "msg": "not found"})
        return httpx.Response(200, json={"code": "0", "data": data, "msg": ""})


def _okx_sync(conn, fake, markets, now):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
            return await OkxAccount("key", "secret", "pass").sync(client, conn, markets, now, 1)
    return asyncio.run(run())


def okx_mkt(coin="SOL", price=110.0):
    return mkt(coin, price=price)


def test_okx_key_with_withdrawals_is_refused(conn):
    with pytest.raises(UnsafeKeyError):
        _okx_sync(conn, FakeOkx(perm="read_only,withdraw"), {"SOL": okx_mkt()}, now=2000)


def test_okx_account_sync(conn):
    fake = FakeOkx()
    # A SOL swing copy was live when the buy happened: its plan should carry over.
    tracker.open_trades(conn, [_copy(conn, "SOL", now=900)], {"SOL": mkt("SOL", price=100)}, now=900)
    result = _okx_sync(conn, fake, {"SOL": okx_mkt(), "BTC": okx_mkt("BTC", 100)}, now=2000)
    assert result["exchange"] == "okx" and result["fee_rate"] == 0.0008 and result["funding_usd"] == 50
    assert result["total_usd"] == pytest.approx(300 + 2 * 110) and not result["can_trade"]
    pos = conn.execute("SELECT * FROM my_positions").fetchone()
    # 2.002 bought, 0.002 paid as fee in SOL -> 2 held at $100 each
    assert pos["source"] == "synced" and pos["qty"] == pytest.approx(2) and pos["entry_price"] == pytest.approx(100.1, rel=1e-3)
    assert pos["stop_order_price"] == 90 and pos["stop_order_kind"] == "stop"  # the OCO's stop-loss leg
    assert pos["style"] == "copy" and pos["stop_price"] == pytest.approx(0.75 * pos["entry_price"])
    assert pos["traders_at_entry"] == 1 and pos["exchange_check"] == "ok"

    # Trailing stop instead of an OCO is recognised too.
    fake.algos = {"conditional,oco": [], "move_order_stop": [{"instId": "SOL-USDT", "algoId": "t1", "ordType": "move_order_stop",
                                                               "side": "sell", "sz": "2", "callbackRatio": "0.05", "cTime": "1"}]}
    _okx_sync(conn, fake, {"SOL": okx_mkt()}, now=2600)
    assert conn.execute("SELECT stop_order_kind FROM my_positions").fetchone()[0] == "trailing"

    # Sold on OKX: closes at the real price with real fees (0.002 SOL on the buy + 0.24 USDT on the sell).
    fake.balance = [{"ccy": "USDT", "cashBal": "540", "availBal": "540", "frozenBal": "0"}]
    fake.algos = {"conditional,oco": [], "move_order_stop": []}
    fake.fills.append({"instId": "SOL-USDT", "billId": "1002", "ordId": "10", "fillPx": "120", "fillSz": "2",
                       "side": "sell", "fee": "-0.24", "feeCcy": "USDT", "ts": "3000000"})
    _okx_sync(conn, fake, {"SOL": okx_mkt(), "BTC": okx_mkt("BTC", 100)}, now=4000)
    pos = conn.execute("SELECT * FROM my_positions").fetchone()
    assert pos["status"] == "closed" and pos["exit_price"] == 120 and pos["exit_reason"] == "sold on OKX"
    assert pos["fees_usd"] == pytest.approx(0.002 * 100 + 0.24)


def test_okx_market_data_is_converted():
    from app.market import OkxSpot

    def okx(request):
        p = request.url.path
        if p == "/api/v5/public/instruments":
            data = [{"instId": "DOGE-USDT", "baseCcy": "DOGE", "quoteCcy": "USDT", "state": "live"},
                    {"instId": "DOGE-EUR", "baseCcy": "DOGE", "quoteCcy": "EUR", "state": "live"}]
        elif p == "/api/v5/market/tickers":
            data = [{"instId": "DOGE-USDT", "last": "0.11", "bidPx": "0.1099", "askPx": "0.1101", "open24h": "0.10",
                     "volCcy24h": "5000000"}]
        else:  # candles, newest first
            data = [[str(1000 - i * 60_000), "1", "2", "0.5", str(1 + i), "10", "10", "7", "1" if i else "0"] for i in range(60)]
        return httpx.Response(200, json={"code": "0", "data": data, "msg": ""})

    async def run():
        s = OkxSpot()
        async with httpx.AsyncClient(transport=httpx.MockTransport(okx)) as client:
            m = await s.markets(client)
            await s.add_trends(client, m, ["DOGE"])
        return m
    m = asyncio.run(run())
    assert list(m) == ["DOGE"] and m["DOGE"].volume_usd == 5e6 and m["DOGE"].change_24h == pytest.approx(0.10)
    assert m["DOGE"].ma50 is not None


def test_manual_position_missing_from_okx_is_flagged(conn):
    portfolio.open_position(conn, user_id=1, market_key="perp:ETH", symbol="ETH", entry_price=2000, size_usd=100, pick=None, now=0)
    fake = FakeOkx()
    fake.balance = [{"ccy": "USDT", "cashBal": "10", "availBal": "10", "frozenBal": "0"}]
    _okx_sync(conn, fake, {"ETH": mkt("ETH", price=2000)}, now=2000)
    pos = conn.execute("SELECT * FROM my_positions").fetchone()
    assert pos["status"] == "open" and pos["exchange_check"] == "missing"


def test_old_binance_data_is_cleared_on_upgrade(tmp_path):
    path = tmp_path / "old.db"
    old = db.connect(path)
    db.save_markets(old, {"BTC": Market("BTC", "BTCUSDT", 100, 100, 100, 1e9)}, ts=1)  # Binance-style pair
    db.set_pref(old, "exchange", "binance")
    old.close()
    conn = db.connect(path)  # upgrade runs on connect
    assert db.load_markets(conn) == {} and db.get_pref(conn, "exchange", "") == ""


def test_upgrading_an_old_database(tmp_path):
    """A database from before accounts existed (no user columns) upgrades in place and keeps its rows."""
    import sqlite3 as _sqlite

    path = tmp_path / "old.db"
    old = _sqlite.connect(path)
    old.executescript("""
        CREATE TABLE my_positions (id INTEGER PRIMARY KEY, opened_at INTEGER NOT NULL, market_key TEXT NOT NULL,
            symbol TEXT NOT NULL, direction TEXT NOT NULL, price_key TEXT NOT NULL, entry_price REAL NOT NULL,
            size_usd REAL NOT NULL, binance_check TEXT);
        INSERT INTO my_positions (opened_at, market_key, symbol, direction, price_key, entry_price, size_usd, binance_check)
            VALUES (1, 'perp:BTC', 'BTC', 'long', 'perp:BTC', 100, 50, 'ok');
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, position_id INTEGER NOT NULL, kind TEXT NOT NULL,
            level TEXT NOT NULL, message TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0, UNIQUE (position_id, kind));
        CREATE TABLE account_holdings (coin TEXT PRIMARY KEY);
    """)
    old.commit()
    old.close()
    conn = db.connect(path)
    row = conn.execute("SELECT * FROM my_positions").fetchone()
    assert row["symbol"] == "BTC" and row["user_id"] is None and row["exchange_check"] == "ok"
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'my_positions_user'").fetchone()
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'account_holdings'").fetchone()


def test_tables_of_dropped_strategies_are_removed_on_upgrade(tmp_path):
    path = tmp_path / "old.db"
    old = db.connect(path)
    old.executescript("CREATE TABLE movers (coin TEXT); CREATE TABLE pumps (coin TEXT); CREATE TABLE positioning (x);")
    db.set_pref(old, "scan_at", "1")
    old.close()
    conn = db.connect(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not names & {"movers", "pumps", "positioning"} and db.get_pref(conn, "scan_at", "") == ""


# --- swing copies ----------------------------------------------------------------------------------------

def _copies(conn, now):
    markets = {"SOL": mkt("SOL", price=110), "ETH": mkt("ETH", price=110)}
    return swing.candidates(conn, markets, {("hl", "a"): 1.0, ("hl", "b"): 2.0}, now, 1000, 0.002, 0.0005, 1e6, 0.01)


def test_copies_need_a_position_held_12_hours(conn):
    now = 100 * 3600
    _hold(conn, "a", first_seen=now - 13 * 3600)
    _hold(conn, "b", first_seen=now - 2 * 3600, key="perp:ETH")  # too fresh
    [c] = _copies(conn, now)
    assert c.symbol == "SOL" and c.strength == "Copy" and c.features["copy_address"] == "a"
    assert c.size_usd == 50 and c.stop_pct == -0.25


def test_copies_are_half_size_when_btc_is_below_its_50_day_average(conn):
    now = 100 * 3600
    _hold(conn, "a", first_seen=now - 13 * 3600)
    down = swing.market_regime({"BTC": mkt("BTC", uptrend=False)})
    assert down["risk_on"] is False and swing.market_regime({"BTC": mkt("BTC")})["risk_on"] is True
    [c] = swing.candidates(conn, {"SOL": mkt("SOL", price=110)}, {("hl", "a"): 1.0}, now, 1000, 0.002, 0.0005, 1e6, 0.01,
                           risk_on=False)
    assert c.size_usd == 25


def test_copies_skip_coins_too_thin_to_trade(conn):
    now = 100 * 3600
    _hold(conn, "a", first_seen=now - 13 * 3600)
    thin = {"SOL": mkt("SOL", price=110, volume=1e5)}
    assert swing.candidates(conn, thin, {("hl", "a"): 1.0}, now, 1000, 0.002, 0.0005, 1e6, 0.01) == []


def test_one_copy_per_coin_follows_the_best_trader(conn):
    now = 100 * 3600
    _hold(conn, "a", first_seen=now - 20 * 3600)
    _hold(conn, "b", first_seen=now - 30 * 3600)
    [c] = _copies(conn, now)
    assert c.features["copy_address"] == "b"


def test_copy_sells_when_the_trader_halves_the_position(conn):
    now = 100 * 3600
    log_id = _hold(conn, "a", first_seen=now - 13 * 3600)
    [c] = _copies(conn, now)
    tracker.open_trades(conn, [c], {"SOL": mkt("SOL", price=110)}, now)
    assert tracker.update_trades(conn, {"SOL": mkt("SOL", price=111)}, now + 60) == 0
    with conn:
        conn.execute("UPDATE position_log SET reduced_at = ? WHERE id = ?", (now + 100, log_id))
    assert tracker.update_trades(conn, {"SOL": mkt("SOL", price=115)}, now + 120) == 1
    row = conn.execute("SELECT * FROM pick_trades").fetchone()
    assert row["style"] == "copy" and row["exit_reason"] == "trader_closed" and row["net_return"] > 0
