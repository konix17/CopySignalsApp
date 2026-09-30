"""Daily market data for the long/short model (lsmodel.py), kept in the history database.

Per coin and day: Binance spot candles with taker-buy volume and trade counts (delisted coins kept), Binance USDT
perpetual volume and funding, Bybit open interest; plus Deribit's BTC implied volatility. All public, no keys.
`update` fetches only the days that are missing, so the daily run is a few hundred small requests; a coin seen for the
first time gets its full history. `import_research` seeds everything from research/collect_market.py's download.
"""

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
DAY_MS = 86_400_000
START_MS = 1_483_228_800_000  # 2017-01-01
SPOT = "https://api.binance.com"
FUT = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
NOT_COINS = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "EUR", "GBP", "AUD", "TRY", "BRL", "PAX", "USDS", "USD1",
             "AEUR", "EURI", "XUSD", "BFUSD", "U", "RLUSD", "PAXG", "WBTC", "WBETH", "BETH", "BNSOL"}
TABLES = """
CREATE TABLE IF NOT EXISTS ls_spot (coin TEXT, day INTEGER, open REAL, high REAL, low REAL, close REAL,
  quote_vol REAL, taker_buy_quote REAL, trades INTEGER, PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS ls_fut (coin TEXT, day INTEGER, close REAL, quote_vol REAL, taker_buy_quote REAL,
  PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS ls_funding (coin TEXT, day INTEGER, rate REAL, n INTEGER, PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS ls_oi (coin TEXT, day INTEGER, oi REAL, PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS ls_dvol (coin TEXT, day INTEGER, close REAL, PRIMARY KEY (coin, day));
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(TABLES)


def last_days(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    return {c: d for c, d in conn.execute(f"SELECT coin, MAX(day) FROM {table} GROUP BY coin")}


def latest_day(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(day) FROM ls_spot WHERE coin = 'BTC'").fetchone()
    return row[0] if row else None


def import_research(conn: sqlite3.Connection, path: Path) -> int:
    """Copy research/collect_market.py's data (data/market.db) in; returns the spot rows added."""
    if not path.exists():
        return 0
    before = conn.execute("SELECT COUNT(*) FROM ls_spot").fetchone()[0]
    conn.execute("ATTACH DATABASE ? AS research", (str(path),))
    try:
        with conn:
            for src, dst in (("spot_1d", "ls_spot"), ("fut_1d", "ls_fut"), ("funding", "ls_funding"),
                             ("oi_1d", "ls_oi"), ("dvol", "ls_dvol")):
                conn.execute(f"INSERT OR IGNORE INTO {dst} SELECT * FROM research.{src}")
    finally:
        conn.execute("DETACH DATABASE research")
    return conn.execute("SELECT COUNT(*) FROM ls_spot").fetchone()[0] - before


async def _get(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, **params):
    async with sem:
        for attempt in range(5):
            try:
                r = await client.get(url, params=params)
            except httpx.HTTPError:
                await asyncio.sleep(2 + 3 * attempt)
                continue
            if r.status_code in (418, 429):
                await asyncio.sleep(30)
                continue
            if r.status_code == 400:
                return None
            r.raise_for_status()
            return r.json()
        raise httpx.HTTPError(f"gave up on {url}")


async def _klines(client, sem, base, path, symbol, since_ms, now_ms) -> list:
    out, t = [], since_ms
    while t < now_ms:
        k = await _get(client, sem, base + path, symbol=symbol, interval="1d", startTime=t, limit=1000)
        if not k:
            break
        out += [x for x in k if int(x[0]) + DAY_MS <= now_ms]  # finished days only
        t = int(k[-1][0]) + DAY_MS
        if len(k) < 1000:
            break
    return out


def _since(last: dict[str, int], coin: str) -> int:
    """Re-read the last stored day (it may have been fetched unfinished), or everything for a new coin."""
    return last[coin] * 1000 if coin in last else START_MS


async def update(conn: sqlite3.Connection, now: int | None = None, progress=None) -> dict:
    """Fetch every missing finished day for the coins trading now. Returns counts per table."""
    now_ms = int((now or time.time()) * 1000)
    init(conn)
    sem = asyncio.Semaphore(6)
    counts = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "copy-signals/0.5"}) as client:
        info = await _get(client, sem, SPOT + "/api/v3/exchangeInfo")
        spot = sorted({s["baseAsset"] for s in info["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
                       and s["baseAsset"] not in NOT_COINS
                       and not (s["baseAsset"].endswith(("UP", "DOWN", "BULL", "BEAR")) and len(s["baseAsset"]) > 4)})
        finfo = await _get(client, sem, FUT + "/fapi/v1/exchangeInfo")
        perps = sorted({(s["symbol"], s["baseAsset"]) for s in finfo["symbols"] if s["quoteAsset"] == "USDT"
                        and s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"})
        binfo = await _get(client, sem, BYBIT + "/v5/market/instruments-info", category="linear", limit=1000)
        linear = sorted({(x["symbol"], x["baseCoin"]) for x in binfo["result"]["list"]
                         if x["quoteCoin"] == "USDT" and x.get("contractType") == "LinearPerpetual"
                         and x.get("status") == "Trading"})

        last = last_days(conn, "ls_spot")

        async def spot_one(coin):
            k = await _klines(client, sem, SPOT, "/api/v3/klines", f"{coin}USDT", _since(last, coin), now_ms)
            return [(coin, int(x[0]) // 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7]),
                     float(x[10]), int(x[8])) for x in k]

        rows = [r for part in await asyncio.gather(*(spot_one(c) for c in spot)) for r in part]
        with conn:
            conn.executemany("INSERT OR REPLACE INTO ls_spot VALUES (?,?,?,?,?,?,?,?,?)", rows)
        counts["spot"] = len(rows)
        if progress:
            progress("spot", len(spot))

        last = last_days(conn, "ls_fut")
        last_f = last_days(conn, "ls_funding")

        async def fut_one(symbol, coin):
            k = await _klines(client, sem, FUT, "/fapi/v1/klines", symbol, _since(last, coin), now_ms)
            per_day, t = {}, _since(last_f, coin)
            while t < now_ms:
                f = await _get(client, sem, FUT + "/fapi/v1/fundingRate", symbol=symbol, startTime=t, limit=1000)
                if not f:
                    break
                for x in f:
                    d = int(x["fundingTime"]) // DAY_MS * 86400
                    s, n = per_day.get(d, (0.0, 0))
                    per_day[d] = (s + float(x["fundingRate"] or 0), n + 1)
                t = int(f[-1]["fundingTime"]) + 1
                if len(f) < 1000:
                    break
            return ([(coin, int(x[0]) // 1000, float(x[4]), float(x[7]), float(x[10])) for x in k],
                    [(coin, d, s / n, n) for d, (s, n) in per_day.items() if (d + 86400) * 1000 <= now_ms])

        parts = await asyncio.gather(*(fut_one(s, c) for s, c in perps))
        with conn:
            conn.executemany("INSERT OR REPLACE INTO ls_fut VALUES (?,?,?,?,?)", [r for a, _ in parts for r in a])
            conn.executemany("INSERT OR REPLACE INTO ls_funding VALUES (?,?,?,?)", [r for _, b in parts for r in b])
        counts["futures"] = sum(len(a) for a, _ in parts)
        if progress:
            progress("futures", len(perps))

        last = last_days(conn, "ls_oi")

        async def oi_one(symbol, coin):
            out, end = [], now_ms
            stop = last.get(coin, START_MS // 1000) * 1000
            while True:
                r = await _get(client, sem, BYBIT + "/v5/market/open-interest", category="linear", symbol=symbol,
                               intervalTime="1d", limit=200, endTime=end)
                lst = (r or {}).get("result", {}).get("list") or []
                if not lst:
                    break
                out += [(coin, int(x["timestamp"]) // 1000, float(x["openInterest"])) for x in lst]
                oldest = min(int(x["timestamp"]) for x in lst)
                if len(lst) < 200 or oldest <= stop:
                    break
                end = oldest - 1
            return out

        rows = [r for part in await asyncio.gather(*(oi_one(s, c) for s, c in linear)) for r in part]
        with conn:
            conn.executemany("INSERT OR REPLACE INTO ls_oi VALUES (?,?,?)", rows)
        counts["open_interest"] = len(rows)

        r = await _get(client, sem, "https://www.deribit.com/api/v2/public/get_volatility_index_data", currency="BTC",
                       start_timestamp=now_ms - 20 * DAY_MS, end_timestamp=now_ms, resolution="1D")
        rows = [("BTC", int(x[0]) // 1000 // 86400 * 86400, float(x[4]))
                for x in (r or {}).get("result", {}).get("data") or [] if int(x[0]) + DAY_MS <= now_ms]
        with conn:
            conn.executemany("INSERT OR REPLACE INTO ls_dvol VALUES (?,?,?)", rows)
        counts["implied_vol"] = len(rows)
    return counts


async def prices(client: httpx.AsyncClient, coins: list[str]) -> dict[str, float]:
    """Live Binance spot prices for `coins` (one request for all)."""
    r = await client.get(SPOT + "/api/v3/ticker/price")
    r.raise_for_status()
    want = {f"{c}USDT": c for c in coins}
    return {want[x["symbol"]]: float(x["price"]) for x in r.json() if x["symbol"] in want and float(x["price"]) > 0}
