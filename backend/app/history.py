"""Long price history for backtests, cached in its own database (data/history.db).

- OKX daily candles (bar "1Dutc"): the venue the app trades on. OKX only serves pairs it still lists, so coins
  that died are missing (survivorship bias): a backtest on them flatters any strategy that buys coins that rose.
- Binance daily candles (bar "binance-1d"), for research only: Binance still serves ~200 pairs it delisted
  (LUNA, FTT, SRM...), so the "most traded coins" can be picked as they were at the time, dead ones included.
  Prices of liquid coins are nearly the same on both exchanges.

Only finished candles are stored, and each update fetches just the days since the last one stored.
"""

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

from .market import OkxSpot, Pacer

DAY = 86400
FIRST_DAY = 1_514_764_800  # 2018-01-01: OKX's history API reaches back to about here
BINANCE = "https://api.binance.com"
BINANCE_BAR = "binance-1d"
INTERVAL_S = {"1d": DAY, "4h": 4 * 3600, "1h": 3600, "5m": 300}
# Not coins to trade: stablecoins, fiat, and leveraged tokens.
NOT_COINS = {"USDC", "BUSD", "TUSD", "PAX", "USDP", "USDS", "USDSB", "FDUSD", "DAI", "UST", "USTC", "EUR", "GBP", "AUD",
             "TRY", "BRL", "RUB", "UAH", "BIDR", "IDRT", "NGN", "ZAR", "PLN", "RON", "ARS", "JPY", "MXN", "COP", "CZK",
             "SUSD", "USDSOLD", "AEUR", "EURI", "USDE", "USD1", "RLUSD", "XUSD", "BFUSD", "U", "PAXG"}
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")
SCHEMA = """
-- Daily average of the 8-hour funding rate on Binance's perpetual futures (public data, since 2019-09).
CREATE TABLE IF NOT EXISTS funding (
    pair TEXT NOT NULL, day INTEGER NOT NULL, rate REAL NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (pair, day)
);
CREATE TABLE IF NOT EXISTS candles (
    pair TEXT NOT NULL, bar TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume_usd REAL NOT NULL,
    PRIMARY KEY (pair, bar, ts)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def last_stored(conn: sqlite3.Connection, bar: str) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT pair, MAX(ts) FROM candles WHERE bar = ? GROUP BY pair", (bar,))}


async def top_pairs(client: httpx.AsyncClient, spot: OkxSpot, n: int) -> list[str]:
    """Today's `n` most traded OKX spot USDT pairs (stablecoins excluded), BTC and ETH always included."""
    markets = await spot.markets(client)
    ranked = sorted(markets.values(), key=lambda m: -m.volume_usd)
    return list(dict.fromkeys(["BTC-USDT", "ETH-USDT", *(m.pair for m in ranked[:n])]))


async def update(conn: sqlite3.Connection, pairs: list[str] | None = None, n: int = 40, bar: str = "1Dutc",
                 region: str = "eea", now: int | None = None, log=print) -> list[str]:
    """Fetch what's missing for `pairs` (default: the `n` most traded) and store it. Returns the pairs updated."""
    now = now or int(time.time())
    spot, pacer = OkxSpot(region), Pacer()
    have = last_stored(conn, bar)
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "copy-signals/0.4"}) as client:
        pairs = pairs or await top_pairs(client, spot, n)
        sem = asyncio.Semaphore(4)

        async def one(pair: str):
            async with sem:
                rows = await spot.history(client, pair, bar, have.get(pair, FIRST_DAY - DAY) + 1, now, pacer)
                done = [r[:6] for r in rows if r[6]]
                with conn:
                    conn.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     [(pair, bar, *r) for r in done])
                log(f"  {pair}: +{len(done)} candles")
                return pair

        results = await asyncio.gather(*(one(p) for p in pairs), return_exceptions=True)
    for p, r in zip(pairs, results):
        if isinstance(r, BaseException):
            log(f"  {p}: failed ({type(r).__name__}: {r})")
    return [r for r in results if isinstance(r, str)]


def load(conn: sqlite3.Connection, bar: str = "1Dutc", pairs: list[str] | None = None) -> dict[str, list[tuple]]:
    """{pair: [(ts, open, high, low, close, volume_usd), ...]} oldest first."""
    sql, args = "SELECT pair, ts, open, high, low, close, volume_usd FROM candles WHERE bar = ?", [bar]
    if pairs:
        sql += f" AND pair IN ({','.join('?' * len(pairs))})"
        args += pairs
    out: dict[str, list[tuple]] = {}
    for pair, *row in conn.execute(sql + " ORDER BY pair, ts", args):
        out.setdefault(pair, []).append(tuple(row))
    return out


FUNDING_START = 1_567_296_000  # 2019-09-01, when Binance's USDT perpetual funding history begins


async def update_funding(conn: sqlite3.Connection, pairs: list[str], now: int | None = None) -> None:
    """Fetch funding rates since the last stored day (re-reading that day, which may have been incomplete) and store
    their daily averages. `pairs` like "BTC-USDT"."""
    now = now or int(time.time())
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "copy-signals/0.4"}) as client:
        for pair in pairs:
            last = conn.execute("SELECT MAX(day) FROM funding WHERE pair = ?", (pair,)).fetchone()[0]
            start = (last if last is not None else FUNDING_START) * 1000
            days: dict[int, list[float]] = {}
            while start < now * 1000:
                resp = await client.get("https://fapi.binance.com/fapi/v1/fundingRate",
                                        params={"symbol": pair.replace("-", ""), "startTime": start, "limit": 1000})
                resp.raise_for_status()
                rows = resp.json()
                for r in rows:
                    ts = r["fundingTime"] // 1000
                    days.setdefault(ts - ts % DAY, []).append(float(r["fundingRate"]))
                if len(rows) < 1000:
                    break
                start = rows[-1]["fundingTime"] + 1
            with conn:
                conn.executemany("INSERT OR REPLACE INTO funding VALUES (?, ?, ?, ?)",
                                 [(pair, d, sum(v) / len(v), len(v)) for d, v in days.items()])


def load_funding(conn: sqlite3.Connection) -> dict[str, dict[int, float]]:
    """{pair: {day: average 8-hour funding rate}}"""
    out: dict[str, dict[int, float]] = {}
    for pair, day, rate in conn.execute("SELECT pair, day, rate FROM funding"):
        out.setdefault(pair, {})[day] = rate
    return out


def _binance_coins(symbols: list[dict]) -> list[str]:
    out = []
    for x in symbols:
        base = x["baseAsset"]
        if x["quoteAsset"] != "USDT" or base in NOT_COINS or (base.endswith(LEVERAGED) and len(base) > 4):
            continue
        out.append(base)
    return out


async def update_binance(conn: sqlite3.Connection, now: int | None = None, log=print, coins: list[str] | None = None,
                         interval: str = "1d", since: int = FIRST_DAY) -> int:
    """Candles (bar "binance-<interval>") for `coins`, default every USDT pair Binance lists or delisted (research
    only). Returns pairs updated."""
    now = now or int(time.time())
    step, bar = INTERVAL_S[interval], f"binance-{interval}"
    pacer = Pacer(0.07)
    have = last_stored(conn, bar)
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "copy-signals/0.4"}) as client:
        if coins is None:
            info = (await client.get(f"{BINANCE}/api/v3/exchangeInfo", params={"permissions": "SPOT"})).json()
            coins = _binance_coins(info["symbols"])
        sem = asyncio.Semaphore(6)

        async def one(coin: str) -> int:
            pair, start = f"{coin}-USDT", have.get(f"{coin}-USDT", since - step) + step
            added = 0
            async with sem:
                while start < now - step:
                    await pacer.wait()
                    resp = await client.get(f"{BINANCE}/api/v3/klines", params={
                        "symbol": f"{coin}USDT", "interval": interval, "startTime": start * 1000, "limit": 1000})
                    if resp.status_code == 429:
                        await asyncio.sleep(30)
                        continue
                    resp.raise_for_status()
                    rows = [r for r in resp.json() if int(r[6]) // 1000 < now]  # closed days only
                    if not rows:
                        break
                    with conn:
                        conn.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                         [(pair, bar, int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]),
                                           float(r[4]), float(r[7])) for r in rows])
                    added += len(rows)
                    start = int(rows[-1][0]) // 1000 + step
                    if len(rows) < 1000:
                        break
            return added

        results = await asyncio.gather(*(one(c) for c in coins), return_exceptions=True)
    failed = [c for c, r in zip(coins, results) if isinstance(r, BaseException)]
    if failed:
        log(f"  failed: {', '.join(failed[:20])}{'…' if len(failed) > 20 else ''}")
    return sum(1 for r in results if isinstance(r, int) and r > 0)
