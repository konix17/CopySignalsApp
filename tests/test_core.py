import sqlite3

import pytest

from app import activity, db, portfolio, tracker, users
from app.accounts import average_cost
from app.exchanges import _strip_multiplier
from app.market import Market, trend_from_closes
from app.models import PERP, Flow, Position, Positioning, Signal, TraderStat
from app.picks import PickParams, build_picks, round_trip_cost, stop_distance
from app.scoring import max_drawdown, percentile_ranks, score_stats, top_traders
from app.signals import build_signals, meaningful_positions
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


def signal(key="perp:BTC", direction="long", n=8, opp=0, agreement=1.0, conviction=500.0, price=100.0, move=0.0):
    return Signal(market_key=key, asset_class=PERP, symbol=key.split(":")[1], title=key, direction=direction,
                  price_key=key, conviction=conviction, agreement=agreement, n_traders=n, n_opposing=opp,
                  total_size_usd=1e6, avg_entry=price, mark_price=price, move_since_entry=move, sources=["hl"])


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

def test_signals_merge_venues_and_net_out_opposition():
    positions = [pos("a", source="hl"), pos("b", source="gmx"), pos("c", direction="short"),
                 pos("a", key="perp:ETH", size=1000)]
    scores = {("hl", "a"): 80, ("gmx", "b"): 60, ("hl", "c"): 50}
    btc = {s.market_key: s for s in build_signals(positions, scores)}["perp:BTC"]
    assert btc.direction == "long" and btc.n_traders == 2 and btc.n_opposing == 1
    assert btc.sources == ["gmx", "hl"]
    # a: 80 * (0.5 + 0.5 * 0.5) = 60, b: 60 * 1.0 = 60, c: 50 * 1.0 = 50
    assert btc.conviction == pytest.approx(60 + 60 - 50)


def test_dust_positions_are_ignored():
    positions = [pos("a", size=10_000), pos("a", key="perp:ETH", size=10)]
    assert [s.market_key for s in build_signals(positions, {("hl", "a"): 50})] == ["perp:BTC"]
    assert [p.market_key for p in meaningful_positions(positions)] == ["perp:BTC"]


def test_move_since_entry_is_signed_for_shorts():
    long_ = build_signals([pos("a", entry=100, mark=90)], {("hl", "a"): 1})[0]
    short = build_signals([pos("a", direction="short", entry=100, mark=90)], {("hl", "a"): 1})[0]
    assert long_.move_since_entry == pytest.approx(-0.1)
    assert short.move_since_entry == pytest.approx(0.1)


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
    assert _strip_multiplier("1000PEPE") == "PEPE" and _strip_multiplier("1INCH") == "1INCH"


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


def test_position_log_tracks_entries_and_exits(conn):
    _follow(conn, "a", "b")
    stale = 1800
    # First sight: existing positions are baseline, not buys.
    activity.sync_position_log(conn, "hl", [pos("a")], {"a", "b"}, ts=1000, stale_after_s=stale)
    assert activity.flows(conn, now=1000)[("perp:BTC", "long")].buyers == 0
    # b opens a position while watched: a buy. a closes: a sell.
    activity.sync_position_log(conn, "hl", [pos("b")], {"a", "b"}, ts=1600, stale_after_s=stale)
    f = activity.flows(conn, now=1600)[("perp:BTC", "long")]
    assert (f.buyers, f.sellers) == (1, 1)


def test_failed_fetch_is_not_a_sell(conn):
    _follow(conn, "a")
    activity.sync_position_log(conn, "hl", [pos("a")], {"a"}, ts=1000, stale_after_s=1800)
    activity.sync_position_log(conn, "hl", [], set(), ts=1600, stale_after_s=1800)  # a's request failed
    assert activity.flows(conn, now=1600)[("perp:BTC", "long")].sellers == 0


def test_cutting_half_the_position_counts_as_selling(conn):
    _follow(conn, "a")
    activity.sync_position_log(conn, "hl", [pos("a", size=1000)], {"a"}, ts=1000, stale_after_s=1800)
    activity.sync_position_log(conn, "hl", [pos("a", size=400)], {"a"}, ts=1600, stale_after_s=1800)
    assert activity.flows(conn, now=1600)[("perp:BTC", "long")].sellers == 1


def test_hold_times_need_three_known_trades(conn):
    _follow(conn, "a", "b", "c")
    day = 86400
    opened = [pos(x, opened_at=0) for x in "abc"]
    activity.sync_position_log(conn, "hl", opened, set("abc"), ts=100, stale_after_s=1800)
    activity.sync_position_log(conn, "hl", [], set("abc"), ts=4 * day, stale_after_s=1800)
    assert activity.hold_times(conn, now=4 * day)[("perp:BTC", "long")] == pytest.approx(4.0)


# --- picks -----------------------------------------------------------------

def mkt(coin="BTC", price=100.0, uptrend=True, volume=1e9, spread_bps=1, vol=0.03):
    half = price * spread_bps / 20000
    return Market(coin, f"{coin}-USDT", price, price - half, price + half, volume,
                  ma20=price * (0.95 if uptrend else 1.05), ma50=price * (0.9 if uptrend else 1.1),
                  ret30=0.2 if uptrend else -0.2, daily_vol=vol)


def params(**kw):
    base = dict(bankroll=1000, open_exposure_usd=0, fee_rate=0.001, slippage=0.0005, min_volume_usd=2e7,
                max_spread=0.002, risk_strong=0.0125, risk_good=0.0075, max_position_pct=0.10, max_total_pct=0.40,
                max_picks=5, max_picks_downtrend=2, min_net_reward_risk=1.5)
    return PickParams(**{**base, **kw})


def _picks(signals, markets=None, flows=None, positioning=None, **kw):
    markets = markets if markets is not None else {s.symbol: mkt(s.symbol) for s in signals} | {"BTC": mkt("BTC")}
    return build_picks(signals, {}, flows or {}, positioning or {}, markets, {}, params(**kw))


def test_strong_pick_has_levels_costs_and_checks():
    picks, _, regime = _picks([signal("perp:SOL")])
    p = picks[0]
    assert regime["risk_on"] and p.strength == "Strong" and all(c.passed for c in p.checks)
    assert p.stop_price < p.price < p.target_price
    assert p.target_pct == pytest.approx(-2 * p.stop_pct)
    assert p.cost_pct == pytest.approx(2 * (0.001 + 0.00005 + 0.0005), rel=0.01)
    assert p.net_win_usd == pytest.approx(p.size_usd * (p.target_pct - p.cost_pct), abs=0.01)
    assert p.net_loss_usd < 0


def test_smart_money_check_is_required():
    assert _picks([signal(n=2)])[0] == []                      # too few traders
    assert _picks([signal(agreement=0.55, opp=8)])[0] == []    # contested


def test_downtrending_coin_is_only_good_and_needs_other_checks():
    down = {"SOL": mkt("SOL", uptrend=False), "BTC": mkt("BTC")}
    p = _picks([signal("perp:SOL")], markets=down)[0][0]
    assert p.strength == "Good" and not p.checks[1].passed
    crowded = {"SOL": [Positioning("binance", "SOL", 0.85, 0.80, 0.0001)]}
    assert _picks([signal("perp:SOL")], markets=down, positioning=crowded)[0] == []


def test_crowding_is_a_warning():
    crowded = {"SOL": [Positioning("binance", "SOL", 0.80, 0.78, 0.0001)]}
    p = _picks([signal("perp:SOL")], positioning=crowded)[0][0]
    assert p.strength == "Good" and "already long" in p.checks[2].detail


def test_untradable_coins_are_skipped():
    assert _picks([signal("perp:XMR")], markets={"BTC": mkt("BTC")})[0] == []            # not on Binance spot
    thin = {"SOL": mkt("SOL", volume=1e6), "BTC": mkt("BTC")}
    assert _picks([signal("perp:SOL")], markets=thin)[0] == []
    wide = {"SOL": mkt("SOL", spread_bps=50), "BTC": mkt("BTC")}
    assert _picks([signal("perp:SOL")], markets=wide)[0] == []


def test_shorts_are_never_picked():
    assert _picks([signal(direction="short")])[0] == []


def test_market_downtrend_means_fewer_smaller_picks():
    sigs = [signal(f"perp:C{i}") for i in range(6)]
    up = {s.symbol: mkt(s.symbol) for s in sigs} | {"BTC": mkt("BTC")}
    down = up | {"BTC": mkt("BTC", uptrend=False)}
    picks_up, _, _ = _picks(sigs, markets=up)
    picks_down, _, regime = _picks(sigs, markets=down)
    assert len(picks_up) == 5 and len(picks_down) == 2 and not regime["risk_on"]
    assert picks_down[0].size_usd < picks_up[0].size_usd


def test_total_exposure_cap():
    sigs = [signal(f"perp:C{i}") for i in range(5)]
    picks, _, _ = _picks(sigs, open_exposure_usd=350)  # 400 cap - 350 held = 50 left
    assert sum(p.size_usd for p in picks) <= 55


def test_heavy_selling_moves_coin_to_exiting():
    picks, exiting, _ = _picks([signal("perp:ETH")], flows={("perp:ETH", "long"): Flow(buyers=0, sellers=3)})
    assert picks == [] and exiting[0]["symbol"] == "ETH"


def test_late_entry_can_fail_smart_money_check():
    assert _picks([signal(move=0.0)])[0]
    assert _picks([signal(n=3, move=0.40)])[0] == []


def test_stop_distance_and_costs():
    assert stop_distance(0.02, 7) < stop_distance(0.06, 7) <= 0.20
    assert stop_distance(0.001, 1) == 0.03
    assert round_trip_cost(mkt(spread_bps=10), 0.001, 0.0005) > round_trip_cost(mkt(spread_bps=1), 0.001, 0.0005)


# --- portfolio -------------------------------------------------------------

def _open(conn, pick=None, entry=100.0):
    pid = portfolio.open_position(conn, user_id=1, market_key="perp:BTC", symbol="BTC", entry_price=entry, size_usd=100,
                                  pick=pick, now=0)
    return conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()


def _pick(n=8):
    return _picks([signal(n=n)])[0][0]


def test_hold_while_traders_stay(conn):
    p = _open(conn, _pick())
    f = portfolio.evaluate(p, 101, signal(n=8), Flow(), [], None, now=3600)
    assert portfolio.advice_of(f) == "HOLD"


def test_sell_when_traders_leave(conn):
    p = _open(conn, _pick(n=8))
    f = portfolio.evaluate(p, 101, signal(n=3), Flow(sellers=5), [], None, now=3600)
    assert portfolio.advice_of(f) == "SELL"
    assert {x.kind for x in f} >= {"exited", "selling"}


def test_sell_when_traders_flip(conn):
    p = _open(conn, _pick())
    f = portfolio.evaluate(p, 101, signal(direction="short", n=9, opp=2, agreement=0.8), Flow(), [], None, now=3600)
    assert "flipped" in {x.kind for x in f}


def test_stop_and_target(conn):
    p = _open(conn, _pick())
    assert portfolio.advice_of(portfolio.evaluate(p, p["stop_price"] * 0.99, signal(), Flow(), [], None, 1)) == "SELL"
    assert portfolio.advice_of(portfolio.evaluate(p, p["target_price"] * 1.01, signal(), Flow(), [], None, 1)) == "TAKE_PROFIT"


def test_broken_uptrend_is_a_sell(conn):
    p = _open(conn, _pick())
    below50 = Market("BTC", "BTC-USDT", 99, 99, 99, 1e9, ma20=105, ma50=100, ret30=0.1, daily_vol=0.03)
    f = portfolio.evaluate(p, 99, signal(), Flow(), [], below50, now=1)
    assert "trend" in {x.kind for x in f} and portfolio.advice_of(f) == "SELL"


def test_exchange_traders_cutting_is_a_warning(conn):
    p = _open(conn, _pick())
    f = portfolio.evaluate(p, 101, signal(), Flow(), [Positioning("binance", "BTC", 0.55, 0.65)], None, now=1)
    assert portfolio.advice_of(f) == "WATCH"


def _followed(conn, *addresses):
    with conn:
        conn.executemany("INSERT INTO trader_stats (source, address, window, score, followed, updated_at) "
                         "VALUES ('hl', ?, 'week', 1, 1, 0)", [(a,) for a in addresses])


def _sold(conn, address, first_seen, closed_at, key="perp:BTC"):
    with conn:
        conn.execute("INSERT INTO position_log (source, address, market_key, direction, first_seen, exact, baseline, "
                     "last_seen, size_usd, peak_size_usd, closed_at) VALUES ('hl', ?, ?, 'long', ?, 1, 0, ?, 1, 1, ?)",
                     (address, key, first_seen, closed_at, closed_at))


def test_alerts_fire_once_per_reason(conn):
    _open(conn, _pick(n=8))  # opened at now=0
    _followed(conn, *"abcdef")
    for a in "abcdef":
        _sold(conn, a, first_seen=-5000, closed_at=5)
    sigs, markets = {"perp:BTC": signal(n=2)}, {"BTC": mkt("BTC", price=101)}
    first = portfolio.update_positions(conn, sigs, {}, markets, now=10)
    again = portfolio.update_positions(conn, sigs, {}, markets, now=20)
    assert first and not again
    assert conn.execute("SELECT advice FROM my_positions").fetchone()[0] == "SELL"


def test_only_sells_after_the_buy_trigger_the_selling_exit(conn):
    """The churn bug: a coin picked while 2 traders had sold it earlier that day was sold minutes later on those same
    sells, then picked again. Sells from before the trade opened no longer count."""
    tracker.open_trades(conn, [_pick(n=3)], {"BTC": mkt("BTC", price=100)}, now=10_000)
    _followed(conn, "a", "b", "c", "d")
    _sold(conn, "a", first_seen=1000, closed_at=5000)  # both sold hours before the pick
    _sold(conn, "b", first_seen=1000, closed_at=6000)
    sig, markets = {"perp:BTC": signal(n=3)}, {"BTC": mkt("BTC", price=100)}
    assert tracker.update_trades(conn, sig, {}, markets, now=10_600) == 0
    assert portfolio.flow_since_entry(conn, conn.execute("SELECT * FROM pick_trades").fetchone(), 10_600).sellers == 0
    _sold(conn, "c", first_seen=1000, closed_at=11_000)  # two more sell after the buy
    _sold(conn, "d", first_seen=1000, closed_at=11_500)
    assert tracker.update_trades(conn, sig, {}, markets, now=12_000) == 1
    assert conn.execute("SELECT exit_reason FROM pick_trades").fetchone()[0] == "selling"


# --- tracker ---------------------------------------------------------------

def _track(conn, price_path, sig=None, hold_days=None):
    """Open a paper trade for a BTC pick at 100, then replay prices; returns the closed row or None."""
    p = _pick()
    if hold_days is not None:
        p.hold_days = hold_days
    tracker.open_trades(conn, [p], {"BTC": mkt("BTC", price=100)}, now=0)
    for i, px in enumerate(price_path, 1):
        tracker.update_trades(conn, {"perp:BTC": sig or signal()}, {}, {"BTC": mkt("BTC", price=px)}, now=i * 600)
    return conn.execute("SELECT * FROM pick_trades WHERE status = 'closed'").fetchone()


def test_pick_opens_once(conn):
    p = _pick()
    assert tracker.open_trades(conn, [p], {"BTC": mkt()}, now=0) == 1
    assert tracker.open_trades(conn, [p], {"BTC": mkt()}, now=600) == 0


def test_stop_exit_counts_the_loss_net_of_costs(conn):
    t = _track(conn, [99, 95, 80])  # gaps through the stop
    assert t["exit_reason"] == "stop" and t["exit_price"] == 80
    assert t["net_return"] == pytest.approx(-0.20 - t["cost_pct"])


def test_target_exit(conn):
    t = _track(conn, [105, 150])
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(t["target_price"])
    assert t["net_return"] > 0


def test_traders_leaving_closes_the_trade(conn):
    t = _track(conn, [101], sig=signal(n=2))
    assert t["exit_reason"] == "exited"


def test_hold_time_exit_and_btc_benchmark(conn):
    t = _track(conn, [101, 102], hold_days=0.01)
    assert t["exit_reason"] == "time"
    assert t["btc_return"] is not None


def test_performance_summary(conn):
    _track(conn, [150])
    perf = tracker.performance(conn, {})
    assert perf["overall"]["trades"] == 1 and perf["overall"]["win_rate"] == 1.0
    assert perf["by_strength"]["Strong"]["trades"] == 1


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

def _demo(conn, hold_days=7.0, size=100.0):
    p = _pick()
    p.hold_days = hold_days
    pid = portfolio.open_position(conn, user_id=1, market_key=p.market_key, symbol=p.symbol, entry_price=100, size_usd=size,
                                  pick=p, now=0, source="demo", btc_entry=100)
    return pid, p


def _settle(conn, price, now, sig=None):
    return portfolio.settle_demo_trades(conn, {"perp:BTC": sig or signal()}, {}, {"BTC": mkt("BTC", price=price)}, now)


def test_demo_trade_runs_until_its_plan_ends(conn):
    pid, p = _demo(conn)
    assert _settle(conn, 101, now=3600) == []  # still running
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "open" and row["last_price"] == 101

    done = _settle(conn, 200, now=7200)  # target hit
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["exit_reason"] == "target"
    assert row["net_return"] == pytest.approx(p.target_pct - p.cost_pct)
    assert done[0]["level"] == "success" and "successful" in done[0]["message"]


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
    from app.pipeline import open_exposure

    _demo(conn, size=500)
    assert open_exposure(conn, 1) == 0
    assert portfolio.update_positions(conn, {"perp:BTC": signal(n=1)}, {}, {"BTC": mkt("BTC")}, now=10) == []
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
    assert a["best_case"] == pytest.approx(1000 * (p.target_pct - cost))
    assert a["worst_case"] == pytest.approx(1000 * (p.stop_pct - cost))
    assert (a["finished"], a["successful"], a["running"]) == (1, 1, 1)


def test_live_tick_closes_demo_trade_on_target_within_seconds(conn):
    from app.config import Settings
    from app.pipeline import MarketView, Pipeline

    pid, p = _demo(conn)
    pipe = Pipeline(conn, Settings())
    pipe.last_view = MarketView([], [], {}, {"perp:BTC": signal()}, {}, {}, {"BTC": mkt("BTC", price=100)}, {})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(okx_prices({"BTC-USDT": p.target_price * 1.01}))) as client:
            await pipe.live_tick(client)
    asyncio.run(run())
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["exit_reason"] == "target" and pipe.live_at


def test_real_account_and_results(conn):
    p = _pick()
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
    assert a["best_case"] > 0 > a["worst_case"]
    # "I sold": estimated fees, BTC comparison
    portfolio.close_position(conn, pid, 120, now=10, reason="sold", btc_price=110)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["net_return"] == pytest.approx(0.20 - p.cost_pct) and row["btc_return"] == pytest.approx(0.10)
    a = portfolio.real_account(conn, 1, None)
    assert (a["finished"], a["successful"], a["running"]) == (1, 1, 0)


def test_live_tick_alerts_real_positions_without_selling(conn):
    from app.config import Settings
    from app.pipeline import MarketView, Pipeline

    pid = portfolio.open_position(conn, user_id=1, market_key="perp:BTC", symbol="BTC", entry_price=100, size_usd=100,
                                  pick=_pick(), now=0)
    stop = conn.execute("SELECT stop_price FROM my_positions WHERE id = ?", (pid,)).fetchone()[0]
    pipe = Pipeline(conn, Settings())
    pipe.last_view = MarketView([], [], {}, {"perp:BTC": signal()}, {}, {}, {"BTC": mkt("BTC", price=100)}, {})

    async def run():
        transport = httpx.MockTransport(okx_prices({"BTC-USDT": stop * 0.99}))
        async with httpx.AsyncClient(transport=transport) as client:
            await pipe.live_tick(client)
    asyncio.run(run())
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "open" and row["advice"] == "SELL" and row["last_price"] == pytest.approx(stop * 0.99)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM alerts WHERE position_id = ?", (pid,))}
    assert "stop" in kinds


# --- rising now ------------------------------------------------------------

from app import movers  # noqa: E402


def _universe(n=30):
    """25 big coins plus a few smaller ones; the 20 most traded are excluded from scanning."""
    ms = {f"BIG{i}": mkt(f"BIG{i}", volume=1e9 - i) for i in range(20)}
    ms |= {"SMALL": mkt("SMALL", volume=2e7), "TINY": mkt("TINY", volume=1e6), "WIDE": mkt("WIDE", volume=2e7, spread_bps=80)}
    return ms


def _windows(coin="SMALL", ch15=0.03, ch60=0.06, surge=5.0, off_high=0.0, day_vol=2e7):
    last = 100.0
    w15 = {f"{coin}-USDT": {"open": last / (1 + ch15), "high": last * (1 + off_high), "last": last,
                           "quote_volume": surge * day_vol / 96}}
    w60 = {f"{coin}-USDT": {"open": last / (1 + ch60), "high": last, "last": last, "quote_volume": 0}}
    return w15, w60


def test_scanner_universe_is_small_but_tradable():
    assert [m.coin for m in movers.universe(_universe())] == ["SMALL"]


def test_scanner_flags_a_coin_starting_to_rise():
    ms = _universe()
    moves = movers.detect(movers.universe(ms), *_windows())
    assert [m.coin for m in moves] == ["SMALL"] and moves[0].volume_surge == pytest.approx(5.0)


@pytest.mark.parametrize("kw", [dict(ch15=0.005), dict(ch60=0.01), dict(surge=1.5), dict(off_high=0.05)])
def test_scanner_ignores_weak_or_fading_moves(kw):
    assert movers.detect(movers.universe(_universe()), *_windows(**kw)) == []


def test_scanner_skips_coins_already_pumped_today():
    ms = _universe()
    ms["SMALL"].change_24h = 0.55
    assert movers.detect(movers.universe(ms), *_windows()) == []


def test_mover_pick_has_flags_plan_and_small_size():
    ms = _universe()
    ms["SMALL"].change_24h = 0.30
    mv = movers.detect(movers.universe(ms), *_windows())[0]
    sig = {"perp:SMALL": signal("perp:SMALL", n=3)}
    p = movers.build([mv], ms, sig, {"SMALL": 90.0}, {}, budget=200, budget_free=200, share=0.25,
                     fee_rate=0.001, slippage=0.0005)[0]
    names = {c.name: c.passed for c in p.checks}
    assert p.strength == "Early" and names["Smart traders hold it"] and names["New 7-day high"]
    assert names["Already up a lot today"] is False  # a warning, shown with "!"
    assert 0.04 <= -p.stop_pct <= 0.12 and p.target_pct == pytest.approx(-2 * p.stop_pct) and p.hold_days == 2
    assert p.size_usd == 50  # 25% of the $200 high-risk budget
    capped = movers.build([mv], ms, sig, {}, {}, 200, 10, 0.25, 0.001, 0.0005)[0]
    assert capped.size_usd == 10  # only $10 of the budget left


def test_mover_flags_are_recorded_once_and_followed(conn):
    ms = _universe()
    mv = movers.detect(movers.universe(ms), *_windows())[0]
    p = movers.build([mv], ms, {}, {}, {}, 200, 200, 0.25, 0.001, 0.0005)[0]
    assert movers.save(conn, [p], {}, now=1000) == [p]
    assert movers.save(conn, [p], {}, now=1060) == []            # still the same move
    movers.save(conn, [], {"SMALL": 110.0}, now=1500)              # no longer rising, price keeps being followed
    rising, earlier, _ = movers.load(conn, now=1500)
    assert rising == [] and earlier[0]["since_flag"] == pytest.approx(0.10)


def test_early_trades_ignore_the_50_day_trend_rule(conn):
    ms = _universe()
    mv = movers.detect(movers.universe(ms), *_windows())[0]
    p = movers.build([mv], ms, {}, {}, {}, 200, 200, 0.25, 0.001, 0.0005)[0]
    pid = portfolio.open_position(conn, user_id=1, market_key=p.market_key, symbol="SMALL", entry_price=100, size_usd=40,
                                  pick=p, now=0, source="demo")
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    below50 = Market("SMALL", "SMALL-USDT", 101, 101, 101, 2e7, ma20=120, ma50=130, ret30=-0.2, daily_vol=0.05)
    kinds = {f.kind for f in portfolio.evaluate(row, 101, None, None, [], below50, now=10)}
    assert row["style"] == "early" and "trend" not in kinds


def test_pump_rides_ignore_the_50_day_trend_rule(conn):
    """A pump ride below its 50-day average used to be sold at the very next check."""
    p = _pick()
    p.strength, p.trail_pct = "Pump", 0.06
    pid = portfolio.open_position(conn, user_id=1, market_key=p.market_key, symbol="BTC", entry_price=100, size_usd=40,
                                  pick=p, now=0, source="demo")
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    below50 = Market("BTC", "BTC-USDT", 101, 101, 101, 2e7, ma20=120, ma50=130, ret30=-0.2, daily_vol=0.05)
    kinds = {f.kind for f in portfolio.evaluate(row, 101, None, None, [], below50, now=10)}
    assert row["style"] == "pump" and "trend" not in kinds


# --- pumps -------------------------------------------------------------------

from app import pumps  # noqa: E402

NORMAL_5M = 2e7 / 288  # 5-minute volume for a coin trading $20M a day


def k5(t, o, h, l, c, vol_x=1.0):
    """A Binance 5m kline; volume given as a multiple of normal."""
    return [t * 300_000, str(o), str(h), str(l), str(c), "0", 0, str(vol_x * NORMAL_5M)]


def quiet(n, price=1.0):
    return [k5(i, price, price * 1.004, price * 0.996, price) for i in range(n)]


def test_no_pump_without_an_ignition_candle():
    assert pumps.analyze("X", quiet(20), 2e7) is None


def test_pump_phases():
    base = quiet(10)
    ignition = [k5(10, 1.00, 1.04, 1.00, 1.04, vol_x=12)]
    starting = pumps.analyze("X", base + ignition + [k5(11, 1.04, 1.07, 1.03, 1.06, vol_x=8)], 2e7)
    assert starting.phase == "starting" and starting.gain_now == pytest.approx(0.06)
    assert 0.04 <= starting.trail_pct <= 0.10

    running = base + ignition + [k5(11, 1.04, 1.20, 1.04, 1.20, vol_x=10), k5(12, 1.20, 1.25, 1.19, 1.24, vol_x=9)]
    assert pumps.analyze("X", running, 2e7).phase == "running"

    topping = running + [k5(13, 1.24, 1.24, 1.15, 1.16, vol_x=2)]
    assert pumps.analyze("X", topping, 2e7).phase == "topping"

    dumping = running + [k5(13, 1.24, 1.24, 1.00, 1.02, vol_x=6)]
    assert pumps.analyze("X", dumping, 2e7).phase == "dumping"
    thin = pumps.analyze("X", dumping, 1.5e7)  # same candles on a thinner coin: a coordinated-looking pump
    assert thin.pump_like and "coordinated" in thin.summary


def test_pump_phase_is_stored_and_cleared(conn):
    s = pumps.analyze("X", quiet(10) + [k5(10, 1, 1.04, 1, 1.04, vol_x=12)], 2e7)
    pumps.save(conn, {"X": s}, {"X"}, now=1000)
    assert pumps.phases(conn, now=1000)["X"]["phase"] == "starting"
    pumps.save(conn, {}, {"X"}, now=1060)  # checked again: no pump any more
    assert pumps.phases(conn, now=1060) == {}


def test_starting_pump_becomes_a_pump_ride_and_topping_is_not_offered():
    ms = _universe()
    mv = movers.detect(movers.universe(ms), *_windows())[0]
    state = pumps.analyze("SMALL", quiet(10) + [k5(10, 1, 1.04, 1, 1.04, vol_x=12)], 2e7)
    p = movers.build([mv], ms, {}, {}, {"SMALL": state}, 200, 200, 0.25, 0.001, 0.0005)[0]
    assert p.strength == "Pump" and p.trail_pct == state.trail_pct and p.hold_days == pytest.approx(0.25)
    assert p.target_pct == pytest.approx(3 * p.trail_pct) and p.checks[0].name == "Pump starting"
    state.phase = "topping"
    assert movers.build([mv], ms, {}, {}, {"SMALL": state}, 200, 200, 0.25, 0.001, 0.0005) == []


def _pump_ride(conn, trail=0.05):
    ms = _universe()
    mv = movers.detect(movers.universe(ms), *_windows())[0]
    state = pumps.analyze("SMALL", quiet(10) + [k5(10, 1, 1.04, 1, 1.04, vol_x=12)], 2e7)
    state.trail_pct = trail
    p = movers.build([mv], ms, {}, {}, {"SMALL": state}, 200, 200, 0.25, 0.001, 0.0005)[0]
    pid = portfolio.open_position(conn, user_id=1, market_key=p.market_key, symbol="SMALL", entry_price=100, size_usd=50,
                                  pick=p, now=0, source="demo")
    return pid


def _settle_small(conn, price, now):
    return portfolio.settle_demo_trades(conn, {}, {}, {"SMALL": mkt("SMALL", price=price)}, now)


def test_trailing_stop_follows_the_price_up(conn):
    pid = _pump_ride(conn, trail=0.05)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["style"] == "pump" and row["peak_price"] == 100
    _settle_small(conn, 112, now=60)        # climbs: stop moves up to 112 * 0.95 = 106.4
    _settle_small(conn, 108, now=120)       # dips but stays above the trailing stop
    assert conn.execute("SELECT status, peak_price FROM my_positions WHERE id = ?", (pid,)).fetchone()[:] == ("open", 112)
    _settle_small(conn, 106, now=180)       # falls through it
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["exit_reason"] == "stop"
    assert row["exit_price"] == pytest.approx(106) and row["net_return"] > 0  # locked in a gain


def test_pump_dump_is_a_sell_for_risky_trades(conn):
    pid = _pump_ride(conn)
    dumping = pumps.analyze("SMALL", quiet(10) + [k5(10, 1.00, 1.04, 1.00, 1.04, vol_x=12),
                                                  k5(11, 1.04, 1.25, 1.04, 1.24, vol_x=10),
                                                  k5(12, 1.24, 1.24, 1.00, 1.02, vol_x=6)], 2e7)
    pumps.save(conn, {"SMALL": dumping}, {"SMALL"}, now=100)
    done = _settle_small(conn, 101, now=100)
    row = conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["exit_reason"] == "pump_dump" and done


def test_high_risk_budget_is_separate_from_picks(conn):
    from app.config import Settings
    from app.pipeline import bankroll_info, open_exposure

    users.set_setting(conn, 1, "bankroll", 1000)
    b = bankroll_info(conn, Settings(), now=0, user_id=1)
    assert b["risk_budget"] == pytest.approx(100) and b["risk_budget_free"] == pytest.approx(100)  # default 10%
    users.set_setting(conn, 1, "risk_budget", 300)
    ms = _universe()
    mv = movers.detect(movers.universe(ms), *_windows())[0]
    p = movers.build([mv], ms, {}, {}, {}, 300, 300, 0.25, 0.001, 0.0005)[0]
    portfolio.open_position(conn, user_id=1, market_key=p.market_key, symbol="SMALL", entry_price=100, size_usd=75, pick=p, now=0)
    b = bankroll_info(conn, Settings(), now=0, user_id=1)
    assert b["risk_budget_used"] == 75 and b["risk_budget_free"] == 225
    assert open_exposure(conn, 1) == 0  # risky trades don't eat into the normal picks' limit



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
    # A SOL pick was live when the buy happened: its plan should carry over.
    tracker.open_trades(conn, [_picks([signal("perp:SOL")])[0][0]], {"SOL": mkt("SOL", price=100)}, now=900)
    result = _okx_sync(conn, fake, {"SOL": okx_mkt(), "BTC": okx_mkt("BTC", 100)}, now=2000)
    assert result["exchange"] == "okx" and result["fee_rate"] == 0.0008 and result["funding_usd"] == 50
    assert result["total_usd"] == pytest.approx(300 + 2 * 110) and not result["can_trade"]
    pos = conn.execute("SELECT * FROM my_positions").fetchone()
    # 2.002 bought, 0.002 paid as fee in SOL -> 2 held at $100 each
    assert pos["source"] == "synced" and pos["qty"] == pytest.approx(2) and pos["entry_price"] == pytest.approx(100.1, rel=1e-3)
    assert pos["stop_order_price"] == 90 and pos["stop_order_kind"] == "stop"  # the OCO's stop-loss leg
    assert pos["traders_at_entry"] == 8 and pos["exchange_check"] == "ok"

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
            w15, w60 = await s.windows(client, ["DOGE-USDT"])
            candles = await s.candles_5m(client, "DOGE-USDT")
        return m, w15, w60, candles
    m, w15, w60, candles = asyncio.run(run())
    assert list(m) == ["DOGE"] and m["DOGE"].volume_usd == 5e6 and m["DOGE"].change_24h == pytest.approx(0.10)
    assert w15["DOGE-USDT"]["quote_volume"] == 15 * 7 and w60["DOGE-USDT"]["open"] == 1.0
    assert int(candles[0][0]) < int(candles[-1][0])  # oldest first, like Binance


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


# --- swing copies ----------------------------------------------------------------------------------------

def _hold(conn, address, first_seen, key="perp:SOL", closed_at=None, reduced_at=None, score=1.0, entry=100.0):
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


def _copies(conn, now):
    from app import swing
    markets = {"SOL": mkt("SOL", price=110), "ETH": mkt("ETH", price=110)}
    return swing.candidates(conn, markets, {("hl", "a"): 1.0, ("hl", "b"): 2.0}, now, 1000, 0.002, 0.0005, 1e6, 0.01)


def test_copies_need_a_position_held_12_hours(conn):
    now = 100 * 3600
    _hold(conn, "a", first_seen=now - 13 * 3600)
    _hold(conn, "b", first_seen=now - 2 * 3600, key="perp:ETH")  # too fresh
    [c] = _copies(conn, now)
    assert c.symbol == "SOL" and c.strength == "Copy" and c.features["copy_address"] == "a"
    assert c.size_usd == 50 and c.stop_pct == -0.25


def test_one_copy_per_coin_follows_the_best_trader(conn):
    now = 100 * 3600
    _hold(conn, "a", first_seen=now - 20 * 3600)
    _hold(conn, "b", first_seen=now - 30 * 3600)
    [c] = _copies(conn, now)
    assert c.features["copy_address"] == "b"


def test_copy_sells_when_the_trader_sells_and_ignores_the_crowd(conn):
    now = 100 * 3600
    log_id = _hold(conn, "a", first_seen=now - 13 * 3600)
    [c] = _copies(conn, now)
    tracker.open_trades(conn, [c], {"SOL": mkt("SOL", price=110)}, now)
    against = signal("perp:SOL", direction="short", n=9, opp=1, agreement=0.9)  # the crowd turned: a pick would sell
    assert tracker.update_trades(conn, {"perp:SOL": against}, {}, {"SOL": mkt("SOL", price=111)}, now + 60) == 0
    with conn:
        conn.execute("UPDATE position_log SET closed_at = ? WHERE id = ?", (now + 100, log_id))
    assert tracker.update_trades(conn, {}, {}, {"SOL": mkt("SOL", price=115)}, now + 120) == 1
    row = conn.execute("SELECT * FROM pick_trades").fetchone()
    assert row["style"] == "copy" and row["exit_reason"] == "trader_closed" and row["net_return"] > 0
