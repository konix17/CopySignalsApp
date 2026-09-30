"""Download daily market data for every coin, for the pattern search (research/daily_patterns.py). Research only.

    .venv/bin/python research/collect_market.py

Into data/market.db (re-running only fetches what's missing):
- spot_1d: Binance spot daily candles of every USDT pair, delisted ones included, with taker-buy volume (how much
  was bought by market orders, i.e. buying pressure) and the number of trades
- fut_1d: Binance USDT perpetual daily candles (listed and settled contracts), with taker-buy volume
- funding: Binance perpetual funding rates, averaged per day
- oi_1d: Bybit daily open interest (money in open futures bets) for its USDT perpetuals
- dvol: Deribit's BTC and ETH implied volatility index (what options traders expect)
"""
import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DAY_MS = 86_400_000
START_MS = 1_483_228_800_000  # 2017-01-01
db = sqlite3.connect(ROOT / "data" / "market.db", timeout=120)
db.executescript("""
CREATE TABLE IF NOT EXISTS spot_1d (coin TEXT, day INTEGER, open REAL, high REAL, low REAL, close REAL, quote_vol REAL,
  taker_buy_quote REAL, trades INTEGER, PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS fut_1d (coin TEXT, day INTEGER, close REAL, quote_vol REAL, taker_buy_quote REAL,
  PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS funding (coin TEXT, day INTEGER, rate REAL, n INTEGER, PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS oi_1d (coin TEXT, day INTEGER, oi REAL, PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS dvol (coin TEXT, day INTEGER, close REAL, PRIMARY KEY (coin, day));
CREATE TABLE IF NOT EXISTS done (what TEXT PRIMARY KEY, at INTEGER);
""")
NOT_COINS = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "EUR", "GBP", "AUD", "TRY", "BRL", "PAX", "USDS", "USD1",
             "AEUR", "EURI", "XUSD", "BFUSD", "U", "RLUSD", "PAXG", "WBTC", "WBETH", "BETH", "BNSOL"}
now_ms = int(time.time() * 1000)


def is_done(what):
    return db.execute("SELECT 1 FROM done WHERE what = ?", (what,)).fetchone() is not None


def mark(what):
    db.execute("INSERT OR REPLACE INTO done VALUES (?, ?)", (what, int(time.time())))
    db.commit()


async def get(client, url, sem, **params):
    async with sem:
        for attempt in range(6):
            try:
                r = await client.get(url, params=params)
            except httpx.HTTPError:
                await asyncio.sleep(2 + attempt * 2)
                continue
            if r.status_code in (418, 429):
                await asyncio.sleep(30)
                continue
            if r.status_code == 400:
                return None
            r.raise_for_status()
            return r.json()
        return None


async def binance_klines(client, sem, base, path, symbol, coin, table):
    what = f"{table}:{coin}"
    if is_done(what):
        return
    t, rows = START_MS, []
    while t < now_ms:
        k = await get(client, base + path, sem, symbol=symbol, interval="1d", startTime=t, limit=1000)
        if not k:
            break
        for x in k:
            if int(x[0]) + DAY_MS > now_ms:
                continue  # today's unfinished day
            if table == "spot_1d":
                rows.append((coin, int(x[0]) // 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                             float(x[7]), float(x[10]), int(x[8])))
            else:
                rows.append((coin, int(x[0]) // 1000, float(x[4]), float(x[7]), float(x[10])))
        t = int(k[-1][0]) + DAY_MS
        if len(k) < 1000:
            break
    marks = ",".join("?" * len(rows[0])) if rows else ""
    if rows:
        db.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({marks})", rows)
    mark(what)


async def binance_funding(client, sem, symbol, coin):
    what = f"funding:{coin}"
    if is_done(what):
        return
    t, per_day = START_MS, {}
    while t < now_ms:
        f = await get(client, "https://fapi.binance.com/fapi/v1/fundingRate", sem, symbol=symbol, startTime=t, limit=1000)
        if not f:
            break
        for x in f:
            d = int(x["fundingTime"]) // DAY_MS * 86400
            s, n = per_day.get(d, (0.0, 0))
            per_day[d] = (s + float(x["fundingRate"] or 0), n + 1)
        t = int(f[-1]["fundingTime"]) + 1
        if len(f) < 1000:
            break
    db.executemany("INSERT OR REPLACE INTO funding VALUES (?, ?, ?, ?)",
                   [(coin, d, s / n, n) for d, (s, n) in per_day.items()])
    mark(what)


async def bybit_oi(client, sem, symbol, coin):
    what = f"oi:{coin}"
    if is_done(what):
        return
    end, rows = now_ms, []
    while True:
        r = await get(client, "https://api.bybit.com/v5/market/open-interest", sem, category="linear", symbol=symbol,
                      intervalTime="1d", limit=200, endTime=end)
        lst = (r or {}).get("result", {}).get("list") or []
        if not lst:
            break
        rows += [(coin, int(x["timestamp"]) // 1000, float(x["openInterest"])) for x in lst]
        oldest = min(int(x["timestamp"]) for x in lst)
        if len(lst) < 200 or oldest <= START_MS:
            break
        end = oldest - 1
    db.executemany("INSERT OR REPLACE INTO oi_1d VALUES (?, ?, ?)", rows)
    mark(what)


async def main():
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "copy-signals-research"}) as client:
        sem = asyncio.Semaphore(6)
        info = await get(client, "https://api.binance.com/api/v3/exchangeInfo", sem)
        spot = sorted({s["baseAsset"] for s in info["symbols"]
                       if s["quoteAsset"] == "USDT" and s["baseAsset"] not in NOT_COINS
                       and not (s["baseAsset"].endswith(("UP", "DOWN", "BULL", "BEAR")) and len(s["baseAsset"]) > 4)})
        print(f"spot: {len(spot)} coins", flush=True)
        for i in range(0, len(spot), 40):
            await asyncio.gather(*(binance_klines(client, sem, "https://api.binance.com", "/api/v3/klines", f"{c}USDT", c,
                                                  "spot_1d") for c in spot[i:i + 40]))
            print(f"  spot {min(i + 40, len(spot))}/{len(spot)}", flush=True)

        finfo = await get(client, "https://fapi.binance.com/fapi/v1/exchangeInfo", sem)
        perps = sorted({(s["symbol"], s["baseAsset"]) for s in finfo["symbols"]
                        if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"})
        print(f"futures: {len(perps)} perpetuals", flush=True)
        fsem = asyncio.Semaphore(4)
        for i in range(0, len(perps), 20):
            chunk = perps[i:i + 20]
            await asyncio.gather(*(binance_klines(client, fsem, "https://fapi.binance.com", "/fapi/v1/klines", s, c,
                                                  "fut_1d") for s, c in chunk))
            await asyncio.gather(*(binance_funding(client, fsem, s, c) for s, c in chunk))
            print(f"  futures {min(i + 20, len(perps))}/{len(perps)}", flush=True)

        by = await get(client, "https://api.bybit.com/v5/market/instruments-info", sem, category="linear", limit=1000)
        linear = sorted({(x["symbol"], x["baseCoin"]) for x in by["result"]["list"]
                         if x["quoteCoin"] == "USDT" and x.get("contractType") == "LinearPerpetual"})
        print(f"bybit: {len(linear)} perpetuals", flush=True)
        bsem = asyncio.Semaphore(5)
        for i in range(0, len(linear), 25):
            await asyncio.gather(*(bybit_oi(client, bsem, s, c) for s, c in linear[i:i + 25]))
            print(f"  open interest {min(i + 25, len(linear))}/{len(linear)}", flush=True)

        for cur in ("BTC", "ETH"):
            if is_done(f"dvol:{cur}"):
                continue
            rows, end = [], now_ms
            while True:
                r = await get(client, "https://www.deribit.com/api/v2/public/get_volatility_index_data", sem,
                              currency=cur, start_timestamp=START_MS, end_timestamp=end, resolution="1D")
                data = (r or {}).get("result", {}).get("data") or []
                if not data:
                    break
                rows += [(cur, int(x[0]) // 1000 // 86400 * 86400, float(x[4])) for x in data]
                cont = r["result"].get("continuation")
                if not cont or cont >= end:
                    break
                end = cont
            db.executemany("INSERT OR REPLACE INTO dvol VALUES (?, ?, ?)", rows)
            mark(f"dvol:{cur}")
    for t in ("spot_1d", "fut_1d", "funding", "oi_1d", "dvol"):
        n, k = db.execute(f"SELECT COUNT(*), COUNT(DISTINCT coin) FROM {t}").fetchone()
        print(f"{t}: {n:,} rows, {k} coins")


asyncio.run(main())
