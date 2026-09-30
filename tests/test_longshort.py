"""The long/short paper test: picks, the paper book, the model's signals and data, and the daily run."""

import asyncio
import sqlite3

import httpx
import numpy as np
import pytest

from app import db, longshort, lsdata, lsmodel
from app.config import Settings

DAY = 86400


@pytest.fixture(autouse=True)
def small_training_sets(monkeypatch):
    monkeypatch.setattr(lsmodel, "MIN_ROWS", 2000)  # the synthetic data is much smaller than 8 years of coins


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "t.db")
    longshort.init(c)
    return c


# --- picks ----------------------------------------------------------------------------------------------

RANKED = [f"C{i}" for i in range(25)]  # best first


def test_pick_takes_the_best_and_worst_fifth():
    longs, shorts = longshort.pick(RANKED, set(), set())
    assert longs == RANKED[:5] and shorts == RANKED[::-1][:5]


def test_held_coins_stay_while_they_are_near_their_end():
    # C6 was bought earlier and is now 7th best: still within 1.5 fifths (7 coins), so it's kept instead of C4.
    longs, shorts = longshort.pick(RANKED, {"C6", "C12"}, {"C18"})
    assert longs == ["C6", "C0", "C1", "C2", "C3"]  # C12 fell to the middle: sold
    assert shorts[0] == "C18" and "C24" in shorts and len(shorts) == 5  # C18 is 7th worst: kept


def test_pick_never_holds_a_coin_on_both_sides_and_needs_enough_coins():
    longs, shorts = longshort.pick(RANKED[:10], set(RANKED[:10]), set(RANKED[:10]))
    assert not set(longs) & set(shorts) and len(longs) == len(shorts) == 2
    assert longshort.pick(RANKED[:9], set(), set()) == ([], [])


def test_eligible_needs_a_perpetual_a_price_and_volume():
    scores = {"A": 0.3, "B": 0.1, "C": -0.2, "D": 0.5, "E": 0.0}
    volume = {"A": 5e8, "B": 4e8, "C": 3e8, "D": 2e8, "E": 1e8}
    ranked = longshort.eligible(scores, volume, has_perp={"A", "B", "C", "E"}, prices={"A": 1, "B": 1, "C": 1, "D": 1},
                                top=2)
    assert ranked == ["A", "B"]  # D can't be shorted, E has no price, C isn't in the 2 most traded


# --- the paper book ---------------------------------------------------------------------------------------

def _prices(**moves):
    p = {c: 100.0 for c in RANKED} | {"BTC": 50_000.0}
    return p | {c: 100.0 * (1 + m) for c, m in moves.items()}


def test_first_run_opens_half_long_half_short_and_pays_fees(conn):
    longshort.start(conn, 10_000, now=0, btc_price=50_000)
    scores = {c: 1 - i / 25 for i, c in enumerate(RANKED)}
    r = longshort.rebalance(conn, day=DAY, ranked=RANKED, scores=scores, prices=_prices(), funding_day={}, now=DAY)
    pos = longshort.positions(conn)
    assert sorted(c for c, p in pos.items() if p["qty"] > 0) == RANKED[:5]
    assert sorted(c for c, p in pos.items() if p["qty"] < 0) == sorted(RANKED[-5:])
    assert all(abs(p["qty"]) * 100 == pytest.approx(1000) for p in pos.values())  # $5,000 a side, 5 coins each
    assert r["fees_usd"] == pytest.approx(10_000 * longshort.COST) and len(r["trades"]) == 10
    v = longshort.value(conn, _prices())
    assert v["equity"] == pytest.approx(10_000 - r["fees_usd"])
    assert v["long_value"] == pytest.approx(5000) and v["short_value"] == pytest.approx(5000)


def test_shorts_gain_when_the_coin_falls_and_funding_is_settled(conn):
    longshort.start(conn, 10_000, now=0, btc_price=50_000)
    scores = {c: 1 - i / 25 for i, c in enumerate(RANKED)}
    longshort.rebalance(conn, DAY, RANKED, scores, _prices(), {}, DAY)
    moved = _prices(C0=0.10, C24=-0.20)  # a long up 10%, a short down 20%
    v = longshort.value(conn, moved)
    by = {p["coin"]: p for p in v["positions"]}
    assert by["C0"]["pnl"] == pytest.approx(100) and by["C24"]["pnl"] == pytest.approx(200)
    assert by["C24"]["side"] == "short" and by["C24"]["pnl_pct"] == pytest.approx(0.20)
    # Next day, same ranking: funding of +0.1% on everything: longs pay it, shorts receive it.
    before = v["equity"]
    r = longshort.rebalance(conn, 2 * DAY, RANKED, scores, moved, {c: 0.001 for c in RANKED}, 2 * DAY)
    longs_value = sum(10 * moved[c] for c in RANKED[:5])
    shorts_value = sum(10 * moved[c] for c in RANKED[-5:])
    assert r["funding_usd"] == pytest.approx(0.001 * (longs_value - shorts_value))
    assert set(r["longs"]) == set(RANKED[:5])  # nothing changed sides, only sizes were evened out
    after = longshort.value(conn, moved)["equity"]
    assert after == pytest.approx(before - r["funding_usd"] - r["fees_usd"])
    assert r["fees_usd"] < 10_000 * longshort.COST * 0.2  # small resizing trades only


def test_days_are_logged_and_due_once_a_day(conn):
    assert not longshort.due(conn, 10 * DAY)  # no account yet
    longshort.start(conn, 10_000, now=0, btc_price=None)
    now = 10 * DAY + longshort.RUN_AFTER_S + 60
    assert longshort.decision_day(now) == 9 * DAY and longshort.due(conn, now)
    longshort.rebalance(conn, 9 * DAY, RANKED, {c: 0.0 for c in RANKED}, _prices(), {}, now)
    assert not longshort.due(conn, now) and not longshort.due(conn, 11 * DAY)  # 11 * DAY: before 00:15
    assert longshort.due(conn, 11 * DAY + longshort.RUN_AFTER_S)
    row = conn.execute("SELECT * FROM ls_days").fetchone()
    assert row["day"] == 9 * DAY and len(eval(row["longs"])) == 5
    assert longshort.snapshot(conn, _prices(), now) and not longshort.snapshot(conn, _prices(), now + 60)


# --- signals and model ------------------------------------------------------------------------------------

def _synthetic(n_coins=60, n_days=400, seed=1) -> lsmodel.Data:
    """Random-walk coins where calm coins drift up relative to wild ones (so there's a pattern to learn)."""
    rng = np.random.default_rng(seed)
    vol = np.linspace(0.01, 0.08, n_coins)
    drift = 0.004 - 0.1 * vol  # calm: +0.3%/day, wild: -0.4%/day
    rets = rng.normal(drift, vol, (n_days, n_coins))
    close = 100 * np.exp(np.cumsum(rets, axis=0))
    shape = close.shape
    volume = rng.uniform(1e6, 1e8, shape) * (np.arange(n_coins) + 1)
    days = np.arange(n_days) * DAY + 1_600_000_000 // DAY * DAY
    coins = ["BTC"] + [f"X{i}" for i in range(1, n_coins)]
    return lsmodel.Data(days, coins, close, close * 1.02, close * 0.98, close, volume, volume * 0.5,
                        np.full(shape, 1000.0), volume * 2, volume, np.full(shape, 1e-4), np.full(shape, 3e-4),
                        np.full(shape, 1e6), np.full(n_days, 50.0))


def test_signals_only_use_the_past():
    d = _synthetic(n_coins=30, n_days=260)
    univ, vol30 = lsmodel.universe(d)
    F, M = lsmodel.signals(d, univ, vol30)
    t = 200
    later = lsmodel.Data(**{**d.__dict__, "close": d.close.copy(), "high": d.high.copy(), "low": d.low.copy()})
    later.close[t + 1:] *= 3  # change everything after day t
    later.high[t + 1:] *= 3
    later.low[t + 1:] *= 3
    F2, M2 = lsmodel.signals(later, univ, vol30)
    for name in F:
        np.testing.assert_array_equal(np.nan_to_num(F[name][t]), np.nan_to_num(F2[name][t]), err_msg=name)
    for name in M:
        assert np.nan_to_num(M[name][t]) == np.nan_to_num(M2[name][t]), name


def test_universe_needs_history_and_volume():
    d = _synthetic(n_coins=30, n_days=200)
    d.close[:150, 5] = np.nan  # listed on day 150: too young until day 240
    univ, _ = lsmodel.universe(d, top=10)
    assert univ[120:].sum(axis=1).max() == 10 and not univ[:, 5].any()
    assert not univ[:89].any()  # nobody has 90 days yet


def test_model_learns_a_planted_pattern_without_looking_ahead():
    d = _synthetic()
    model = lsmodel.train(d, until=int(d.days[300]))
    assert len(model.estimators) == lsmodel.ENSEMBLE and model.trained_through == int(d.days[300 - 1 - lsmodel.HORIZON])
    scores = lsmodel.scores_for_day(model, d, i=350)
    calm, wild = np.mean([scores[f"X{i}"] for i in range(1, 15)]), np.mean([scores[f"X{i}"] for i in range(45, 60)])
    assert calm > wild


def test_walk_forward_reports_a_summary():
    out = lsmodel.walk_forward(_synthetic(), start=int(_synthetic().days[330]), every_days=40, n_models=2)
    assert out["days"] == 69 and set(out) >= {"cagr", "max_drawdown", "sharpe", "by_year", "points", "btc_cagr"}


def test_model_file_round_trip(tmp_path):
    model = lsmodel.train(_synthetic(), until=int(_synthetic().days[300]), n_models=2)
    path = tmp_path / "m.pkl"
    lsmodel.save(model, path)
    assert lsmodel.load_model(path).trained_through == model.trained_through
    path.write_bytes(b"not a model")
    assert lsmodel.load_model(path) is None and lsmodel.load_model(tmp_path / "missing.pkl") is None


# --- data -----------------------------------------------------------------------------------------------

def test_research_download_is_imported(tmp_path):
    research = sqlite3.connect(tmp_path / "market.db")
    research.executescript("""
        CREATE TABLE spot_1d (coin, day, open, high, low, close, quote_vol, taker_buy_quote, trades);
        CREATE TABLE fut_1d (coin, day, close, quote_vol, taker_buy_quote);
        CREATE TABLE funding (coin, day, rate, n); CREATE TABLE oi_1d (coin, day, oi); CREATE TABLE dvol (coin, day, close);
        INSERT INTO spot_1d VALUES ('BTC', 86400, 1, 2, 0.5, 1.5, 100, 60, 10);
        INSERT INTO funding VALUES ('BTC', 86400, 0.0001, 3);
    """)
    research.commit()
    hist = sqlite3.connect(tmp_path / "history.db")
    lsdata.init(hist)
    assert lsdata.import_research(hist, tmp_path / "market.db") == 1
    assert lsdata.import_research(hist, tmp_path / "market.db") == 0  # already there
    assert lsdata.latest_day(hist) == 86400 and lsdata.import_research(hist, tmp_path / "none.db") == 0


def test_update_fetches_finished_days_only(tmp_path):
    base = lsdata.START_MS // 1000 // DAY  # day numbers from 2017-01-01
    now = (base + 10) * DAY + 3600  # an hour into day 10: days up to 9 are finished

    def k(day, close):
        ms = (base + day) * 86_400_000
        return [ms, "1", "2", "0.5", str(close), "10", ms, "1000", 7, "5", "600", "0"]

    def handler(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": [
                {"baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                {"baseAsset": "USDC", "quoteAsset": "USDT", "status": "TRADING"}]})  # a stablecoin: skipped
        if p == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json={"symbols": [
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"}]})
        if p == "/v5/market/instruments-info":
            return httpx.Response(200, json={"result": {"list": [
                {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "contractType": "LinearPerpetual", "status": "Trading"}]}})
        if p in ("/api/v3/klines", "/fapi/v1/klines"):
            assert q["symbol"] == "BTCUSDT"
            return httpx.Response(200, json=[k(d, 100 + d) for d in range(8, 11)])  # day 10 is unfinished
        if p == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=[{"fundingTime": (base + d) * 86_400_000 + h * 3_600_000, "fundingRate": "0.0001"}
                                             for d in (9, 10) for h in (0, 8, 16)])
        if p == "/v5/market/open-interest":
            return httpx.Response(200, json={"result": {"list": [{"timestamp": str((base + 10) * 86_400_000), "openInterest": "5"}]}})
        if "volatility" in p:
            return httpx.Response(200, json={"result": {"data": [[(base + 9) * 86_400_000, 1, 2, 0.5, 55.0]]}})
        return httpx.Response(404)

    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "transport": httpx.MockTransport(handler)})

    hist = sqlite3.connect(tmp_path / "history.db")
    httpx.AsyncClient = Mocked
    try:
        counts = asyncio.run(lsdata.update(hist, now))
    finally:
        httpx.AsyncClient = real
    assert counts["spot"] == 2 and lsdata.latest_day(hist) == (base + 9) * DAY
    row = hist.execute("SELECT close, quote_vol, taker_buy_quote, trades FROM ls_spot WHERE day = ?",
                       ((base + 9) * DAY,)).fetchone()
    assert row == (109.0, 1000.0, 600.0, 7)
    assert hist.execute("SELECT day, rate, n FROM ls_funding").fetchall() == [((base + 9) * DAY, 0.0001, 3)]  # 10 unfinished
    assert hist.execute("SELECT close FROM ls_dvol").fetchone()[0] == 55.0


# --- the daily run ---------------------------------------------------------------------------------------------

def test_daily_run_opens_the_account_and_rebalances_once(tmp_path, monkeypatch):
    from app import users
    from app.pipeline import Pipeline

    cfg = Settings(db_path=tmp_path / "t.db", secret_key_path=tmp_path / "k", log_dir=tmp_path / "logs",
                   history_path=tmp_path / "history.db", research_market_db=tmp_path / "none.db")
    conn = db.connect(cfg.db_path)
    users.init_secrets(__import__("app.security", fromlist=["SecretBox"]).SecretBox(cfg.secret_key_path))
    pipe = Pipeline(conn, cfg)
    now = 100 * DAY + longshort.RUN_AFTER_S + 5
    day = longshort.decision_day(now)
    scores = {c: 1 - i / 25 for i, c in enumerate(RANKED)}

    async def no_update(*a, **k):
        return {"spot": 0}

    async def prices(client, coins):
        return {c: 100.0 for c in coins} | {"BTC": 50_000.0}
    monkeypatch.setattr(lsdata, "update", no_update)
    monkeypatch.setattr(lsdata, "latest_day", lambda conn: day)
    monkeypatch.setattr(lsdata, "prices", prices)
    monkeypatch.setattr(pipe, "_load_or_train", lambda d: "model")
    monkeypatch.setattr(pipe, "_score", lambda m, d: (scores, {c: 1e9 - i for i, c in enumerate(RANKED)}, set(RANKED)))
    monkeypatch.setattr("time.time", lambda: now)

    async def run():
        async with httpx.AsyncClient() as client:
            await pipe.ls_tick(client)
            await pipe.ls_tick(client)  # same day: nothing more
    asyncio.run(run())
    assert pipe.ls_state == "waiting" and pipe.ls_error is None
    assert conn.execute("SELECT COUNT(*) FROM ls_days").fetchone()[0] == 1
    assert len(longshort.positions(conn)) == 10 and longshort.account(conn)["last_run_day"] == day
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["ls.started", "ls.rebalance"]


def test_funding_covers_every_day_the_app_was_off(tmp_path):
    from app import users
    from app.pipeline import Pipeline
    from app.security import SecretBox

    cfg = Settings(db_path=tmp_path / "t.db", secret_key_path=tmp_path / "k", log_dir=tmp_path / "logs",
                   history_path=tmp_path / "history.db", research_market_db=tmp_path / "none.db")
    users.init_secrets(SecretBox(cfg.secret_key_path))
    pipe = Pipeline(db.connect(cfg.db_path), cfg)
    with pipe.history:
        pipe.history.executemany("INSERT INTO ls_funding VALUES ('SOL', ?, 0.0001, 3)", [(d * DAY,) for d in range(1, 6)])
    assert pipe._funding_for(4 * DAY, 5 * DAY) == {"SOL": pytest.approx(0.0003)}  # ran yesterday: one day
    assert pipe._funding_for(1 * DAY, 5 * DAY) == {"SOL": pytest.approx(0.0012)}  # off for 3 days: four days
