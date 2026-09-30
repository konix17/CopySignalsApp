"""Backtests and the trend bot."""

import pytest

from app import backtest, db, trendbot
from app.backtest import Hold, Panel, TrendEnsemble, TrendFilter, split_at_gaps

DAY = 86400
D0 = 1_600_000_000 - 1_600_000_000 % DAY  # a UTC midnight


def series(closes, start=D0, volume=1e6):
    return [(start + i * DAY, c, c, c, c, volume) for i, c in enumerate(closes)]


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


# --- backtest engine ---------------------------------------------------------------------------------

def test_hold_compounds_the_price_and_pays_once():
    p = Panel({"BTC-USDT": series([100, 110, 121])})
    r = backtest.run(p, Hold(), D0, cost=0.01)
    assert r.equity[-1] == pytest.approx(0.99 * 1.21)
    assert r.entries == 1 and r.turnover == pytest.approx(1.0)


def test_decisions_use_only_past_closes():
    # Up 10% on day 2: a filter can only be in from day 2's close, so it misses that day's gain.
    p = Panel({"BTC-USDT": series([100, 100, 110, 121])})
    r = backtest.run(p, TrendFilter("BTC-USDT", 2), D0, cost=0)
    assert r.equity == pytest.approx([1, 1, 1, 1.1])


def test_trend_filter_sits_in_cash_below_the_average():
    p = Panel({"BTC-USDT": series([100, 90, 80, 70])})
    assert backtest.run(p, TrendFilter("BTC-USDT", 2), D0, cost=0.01).equity[-1] == 1.0


def test_ensemble_weight_is_the_share_of_averages_the_price_is_above():
    p = Panel({"BTC-USDT": series([100] * 6 + [80, 90]), "ETH-USDT": series([10] * 8)})
    s = TrendEnsemble(("BTC-USDT", "ETH-USDT"), (2, 4, 8))
    t = len(p.days) - 1  # 90: above the 2-day average (85), below the 4-day (92.5) and 8-day (96.25) ones
    assert s.strength(p, "BTC-USDT", t) == pytest.approx(1 / 3)
    assert s.weights(p, t) == {"BTC-USDT": pytest.approx(1 / 6)}  # half the account times 1/3; ETH is flat


def test_relistings_become_separate_pairs():
    rows = series([1, 2]) + series([50, 60], start=D0 + 30 * DAY)
    parts = split_at_gaps({"LUNA-USDT": rows})
    assert [len(parts["LUNA-USDT"]), len(parts["LUNA-USDT#2"])] == [2, 2]


def test_metrics_report_drawdown_and_years():
    p = Panel({"BTC-USDT": series([100, 50, 100])})
    m = backtest.run(p, Hold(), D0, cost=0).metrics()
    assert m["max_drawdown"] == pytest.approx(-0.5) and m["total"] == pytest.approx(0)
    assert list(m["by_year"]) == [2020]


# --- trend bot ------------------------------------------------------------------------------------------

def bull_panel(days=200):
    up = [100 * 1.01 ** i for i in range(days)]
    return Panel({"BTC-USDT": series(up), "ETH-USDT": series([x / 20 for x in up])})


def test_decision_day_waits_for_the_daily_close():
    assert trendbot.decision_day(D0 + 60) == D0 - DAY
    assert trendbot.decision_day(D0 + trendbot.CHECK_AFTER_S) == D0


def test_signal_uses_yesterdays_close_and_refuses_stale_data():
    p = bull_panel()
    today = p.days[-1] + DAY
    sig = trendbot.signal(p, today)
    assert sig["weights"] == {"BTC": 0.5, "ETH": 0.5} and sig["close_of"] == p.days[-1]
    with pytest.raises(trendbot.StaleData):
        trendbot.signal(p, today + DAY)


def test_plan_sells_first_and_skips_tiny_changes():
    orders = trendbot.plan(cash=0, held={"BTC": 1.0, "ETH": 0.0}, prices={"BTC": 1000, "ETH": 100},
                           weights={"BTC": 0.2, "ETH": 0.5})
    assert orders == [("BTC", "sell", 800), ("ETH", "buy", 500)]
    assert trendbot.plan(1000, {}, {"BTC": 10}, {"BTC": 0.005}) == []
    assert trendbot.plan(1000, {"BTC": 0.01}, {"BTC": 10}, {}) == [("BTC", "sell", pytest.approx(0.1))]  # closes fully


def test_rebalance_pays_fees_and_slippage(conn):
    trendbot.start(conn, 1, 1000, D0, 100)
    trades = trendbot.rebalance(conn, 1, {"BTC": 0.5}, {"BTC": 100}, fee_rate=0.002, slippage=0.001, now=D0)
    [t] = trades
    assert t.side == "buy" and t.price == pytest.approx(100.1) and t.fee_usd == pytest.approx(500 - 500 / 1.002)
    v = trendbot.value(conn, 1, {"BTC": 100})
    assert v["cash"] == pytest.approx(500) and v["total"] == pytest.approx(500 + 500 / 1.002 / 100.1 * 100)
    trendbot.rebalance(conn, 1, {}, {"BTC": 110}, 0.002, 0.001, D0 + DAY)
    v = trendbot.value(conn, 1, {"BTC": 110})
    assert v["positions"] == [] and v["pnl"] > 0
    assert conn.execute("SELECT COUNT(*) FROM bot_trades").fetchone()[0] == 2


def test_run_due_trades_once_per_day_and_only_when_on(conn):
    p = bull_panel()
    today = p.days[-1] + DAY
    now = today + trendbot.CHECK_AFTER_S + 10
    trendbot.start(conn, 1, 10_000, now, 100)
    trendbot.start(conn, 2, 10_000, now, 100)
    trendbot.set_enabled(conn, 2, False)
    sig = trendbot.signal(p, today)
    prices = {"BTC": p.close["BTC-USDT"][-1], "ETH": p.close["ETH-USDT"][-1]}
    done = trendbot.run_due(conn, sig, prices, lambda uid: 0.002, 0.0005, now)
    assert list(done) == [1] and len(done[1]) == 2
    assert trendbot.run_due(conn, sig, prices, lambda uid: 0.002, 0.0005, now + 3600) == {}
    v = trendbot.value(conn, 1, prices)
    assert v["cash"] == pytest.approx(0) and v["total"] == pytest.approx(10_000 / 1.002 / 1.0005)  # fees and slippage
    assert v["targets"] == {"BTC": 0.5, "ETH": 0.5}
    with pytest.raises(trendbot.StaleData):
        trendbot.run_due(conn, sig, prices, lambda uid: 0.002, 0.0005, now + DAY)


def test_snapshots_are_hourly(conn):
    trendbot.start(conn, 1, 1000, D0, 100)
    for dt in (0, 600, 3600):
        trendbot.snapshot(conn, {"BTC": 100, "ETH": 5}, D0 + dt)
    assert conn.execute("SELECT COUNT(*) FROM bot_snapshots").fetchone()[0] == 2


def test_backtest_summary_has_chart_points():
    p = bull_panel(400)
    trendbot_from = trendbot.BACKTEST_FROM
    trendbot.BACKTEST_FROM = p.days[160]
    try:
        s = trendbot.backtest_summary(p)
    finally:
        trendbot.BACKTEST_FROM = trendbot_from
    assert s["columns"] == ["day", "bot", "BTC", "ETH"] and s["points"][0][1:] == [1.0, 1.0, 1.0]
    assert s["strategy"]["total"] > 0 and s["hold"]["BTC"]["total"] > s["strategy"]["total"]


def test_negative_funding_keeps_a_third_of_the_coin():
    down = [100 - i * 0.3 for i in range(200)]  # below every average: no trend weight
    p = Panel({"BTC-USDT": series(down), "ETH-USDT": series(down)})
    t = len(p.days) - 1
    last = p.days[t]
    funding = {"BTC-USDT": {last - i * DAY: -0.0001 for i in range(3)},
               "ETH-USDT": {last - i * DAY: 0.0002 for i in range(3)}}
    s = TrendEnsemble(("BTC-USDT", "ETH-USDT"), (50, 100, 150), funding=funding)
    assert s.weights(p, t) == {"BTC-USDT": pytest.approx(1 / 6)}  # a third of BTC's half; ETH funding is positive
    assert TrendEnsemble(("BTC-USDT", "ETH-USDT"), (50, 100, 150)).weights(p, t) == {}
    assert TrendEnsemble(("BTC-USDT",), (50,), funding={}).weights(p, t) == {}  # no data, no signal


def test_signal_explains_the_funding_rule():
    p = bull_panel()
    today = p.days[-1] + DAY
    funding = {"BTC-USDT": {p.days[-1]: -0.0002}}
    sig = trendbot.signal(p, today, funding)
    assert sig["coins"]["BTC"]["funding_floor"] is True and sig["coins"]["ETH"]["funding"] is None
    assert sig["weights"] == {"BTC": 0.5, "ETH": 0.5}  # already fully in: the floor changes nothing
