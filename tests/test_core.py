import asyncio
import sqlite3

import httpx
import pytest

from app import db
from app.accounts import OkxAccount, UnsafeKeyError, average_cost
from app.market import Market
from app.symbols import crypto_symbol, normalize_coin


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    return db.connect(tmp_path / "t.db")


def mkt(coin="BTC", price=100.0, volume=1e9, spread_bps=1):
    half = price * spread_bps / 20000
    return Market(coin, f"{coin}-USDT", price, price - half, price + half, volume)


# --- symbols ---------------------------------------------------------------

def test_only_crypto_passes():
    assert crypto_symbol("BTC") == "BTC"
    assert crypto_symbol("xyz:TSLA") is None
    assert crypto_symbol("GOLD") is None and crypto_symbol("XAUT.v2") is None
    assert crypto_symbol("APE_deprecated (deprecated)") is None
    assert crypto_symbol("SPX6900") == "SPX6900"  # a memecoin, not the index
    assert normalize_coin("kPEPE") == ("PEPE", 0.001)
    assert normalize_coin("xyz:NVDA") == (None, 1.0)


# --- OKX account mirror --------------------------------------------------------------

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


def test_okx_key_with_withdrawals_is_refused(conn):
    with pytest.raises(UnsafeKeyError):
        _okx_sync(conn, FakeOkx(perm="read_only,withdraw"), {"SOL": mkt("SOL", 110)}, now=2000)


def test_okx_account_sync(conn):
    fake = FakeOkx()
    result = _okx_sync(conn, fake, {"SOL": mkt("SOL", 110), "BTC": mkt("BTC", 100)}, now=2000)
    assert result["exchange"] == "okx" and result["fee_rate"] == 0.0008 and result["funding_usd"] == 50
    assert result["total_usd"] == pytest.approx(300 + 2 * 110) and not result["can_trade"] and result["holdings"] == 1
    h = conn.execute("SELECT * FROM user_holdings WHERE user_id = 1").fetchone()
    # 2.002 bought, 0.002 paid as fee in SOL -> 2 held, cost $100.1 each
    assert h["coin"] == "SOL" and h["qty"] == pytest.approx(2) and h["avg_cost"] == pytest.approx(100.1, rel=1e-3)
    assert h["stop_price"] == 90 and h["stop_kind"] == "stop" and h["target_price"] == 130  # the OCO's two legs
    assert conn.execute("SELECT COUNT(*) FROM user_trades WHERE user_id = 1").fetchone()[0] == 1

    # A trailing stop instead of an OCO is recognised too; new fills are added, old ones aren't duplicated.
    fake.algos = {"conditional,oco": [], "move_order_stop": [{"instId": "SOL-USDT", "algoId": "t1", "ordType": "move_order_stop",
                                                               "side": "sell", "sz": "2", "callbackRatio": "0.05", "cTime": "1"}]}
    _okx_sync(conn, fake, {"SOL": mkt("SOL", 110)}, now=2600)
    assert conn.execute("SELECT stop_kind FROM user_holdings").fetchone()[0] == "trailing"

    # Sold: the holding disappears from the mirror, the trade is recorded.
    fake.balance = [{"ccy": "USDT", "cashBal": "540", "availBal": "540", "frozenBal": "0"}]
    fake.algos = {"conditional,oco": [], "move_order_stop": []}
    fake.fills.append({"instId": "SOL-USDT", "billId": "1002", "ordId": "10", "fillPx": "120", "fillSz": "2",
                       "side": "sell", "fee": "-0.24", "feeCcy": "USDT", "ts": "3000000"})
    result = _okx_sync(conn, fake, {"SOL": mkt("SOL", 110)}, now=4000)
    assert result["holdings"] == 0 and result["total_usd"] == 540
    assert conn.execute("SELECT COUNT(*) FROM user_holdings").fetchone()[0] == 0


def test_okx_market_data_is_converted():
    from app.market import OkxSpot

    def okx(request):
        p = request.url.path
        if p == "/api/v5/public/instruments":
            data = [{"instId": "DOGE-USDT", "baseCcy": "DOGE", "quoteCcy": "USDT", "state": "live"},
                    {"instId": "DOGE-EUR", "baseCcy": "DOGE", "quoteCcy": "EUR", "state": "live"}]
        else:
            data = [{"instId": "DOGE-USDT", "last": "0.11", "bidPx": "0.1099", "askPx": "0.1101", "open24h": "0.10",
                     "volCcy24h": "5000000"}]
        return httpx.Response(200, json={"code": "0", "data": data, "msg": ""})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(okx)) as client:
            return await OkxSpot().markets(client)
    m = asyncio.run(run())
    assert list(m) == ["DOGE"] and m["DOGE"].volume_usd == 5e6 and m["DOGE"].change_24h == pytest.approx(0.10)


# --- database upgrades --------------------------------------------------------------------------------------

def test_swing_copy_tables_are_backed_up_then_removed(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE pick_trades (id INTEGER PRIMARY KEY, symbol TEXT);
        INSERT INTO pick_trades (symbol) VALUES ('SOL');
        CREATE TABLE my_positions (id INTEGER PRIMARY KEY);
        CREATE TABLE user_settings (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (user_id, key));
        INSERT INTO user_settings VALUES (1, 'demo_mode', 'true'), (1, 'fee_taker', '0.002');
    """)
    old.commit()
    old.close()
    conn = db.connect(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not names & set(db.REMOVED_TABLES) and "bot_accounts" in names
    assert [r[0] for r in conn.execute("SELECT key FROM user_settings")] == ["fee_taker"]
    backups = list((tmp_path / "backups").glob("old-before-removing-copies-*.db"))
    assert len(backups) == 1
    assert sqlite3.connect(backups[0]).execute("SELECT symbol FROM pick_trades").fetchone()[0] == "SOL"
    conn.close()
    db.connect(path)  # nothing left to remove: no second backup
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
