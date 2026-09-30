"""OKX spot market data: what's buyable, at what price, and how liquid.

Public endpoints only (no key). Everything here is per coin traded against USDT.
"""

import asyncio
import time
from dataclasses import dataclass

import httpx

UNIVERSE_TTL = 24 * 3600
HISTORY_PAGE = 100  # OKX history-candles returns at most 100 rows per call
HISTORY_GAP = 0.12  # OKX allows 20 history-candle requests per 2 seconds
STABLES = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP", "USDE", "USD1", "EUR", "TRY", "BRL"}


@dataclass
class Market:
    coin: str
    pair: str
    price: float
    bid: float
    ask: float
    volume_usd: float  # 24h quote volume
    change_24h: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ret30: float | None = None
    daily_vol: float | None = None

    @property
    def spread(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid if mid else 1.0


class Pacer:
    """Keeps requests at least `gap` seconds apart across all workers sharing it."""

    def __init__(self, gap: float = HISTORY_GAP):
        self.gap = gap
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def wait(self) -> None:
        async with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


# (API domain, website) per OKX region. Accounts opened in Europe live on my.okx.com and their API keys only
# work on eea.okx.com; public market data is the same on both.
OKX_DOMAINS = {"eea": ("https://eea.okx.com", "https://my.okx.com"),
               "global": ("https://www.okx.com", "https://www.okx.com")}


class OkxSpot:
    """OKX spot market data. Candles are returned as
    [open time, open, high, low, close, volume, close time (unused), quote volume, finished], oldest first."""

    name = "okx"
    label = "OKX"

    def __init__(self, region: str = "eea"):
        self.api, self.site = OKX_DOMAINS.get(region, OKX_DOMAINS["eea"])
        self._pairs: dict[str, str] = {}
        self._pairs_at = 0.0

    def url(self, coin: str) -> str:
        return f"{self.site}/trade-spot/{coin.lower()}-usdt"

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> list:
        resp = await client.get(f"{self.api}{path}", params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != "0":
            raise RuntimeError(f"OKX {path}: {body.get('msg')}")
        return body["data"]

    async def pairs(self, client: httpx.AsyncClient) -> dict[str, str]:
        if not self._pairs or time.time() - self._pairs_at > UNIVERSE_TTL:
            rows = await self._get(client, "/api/v5/public/instruments", {"instType": "SPOT"})
            self._pairs = {r["baseCcy"]: r["instId"] for r in rows
                           if r["quoteCcy"] == "USDT" and r["state"] == "live" and r["baseCcy"] not in STABLES}
            self._pairs_at = time.time()
        return self._pairs

    async def markets(self, client: httpx.AsyncClient) -> dict[str, Market]:
        pairs = await self.pairs(client)
        by_pair = {p: c for c, p in pairs.items()}
        out = {}
        for t in await self._get(client, "/api/v5/market/tickers", {"instType": "SPOT"}):
            coin = by_pair.get(t["instId"])
            if not coin or not t["last"] or float(t["last"]) <= 0:
                continue
            last, open24 = float(t["last"]), float(t["open24h"] or 0)
            out[coin] = Market(coin, t["instId"], last, float(t["bidPx"] or last), float(t["askPx"] or last),
                               float(t["volCcy24h"] or 0),  # spot: 24h volume in the quote coin (USDT)
                               change_24h=last / open24 - 1 if open24 else None)
        return out

    async def prices(self, client: httpx.AsyncClient, pairs: list[str]) -> dict[str, float]:
        if not pairs:
            return {}
        wanted = set(pairs)
        rows = await self._get(client, "/api/v5/market/tickers", {"instType": "SPOT"})
        return {r["instId"]: float(r["last"]) for r in rows if r["instId"] in wanted and r["last"]}

    async def history(self, client: httpx.AsyncClient, pair: str, bar: str, start: int, end: int,
                      pacer: Pacer) -> list[tuple]:
        """Candles from `start` to `end` (seconds), oldest first, as (open time in seconds, open, high, low, close,
        quote volume, finished). Walks OKX's history endpoint backwards a page at a time, retrying brief errors."""
        rows: dict[int, tuple] = {}
        after = (end + 1) * 1000
        while True:
            for attempt in range(4):
                await pacer.wait()
                try:
                    page = await self._get(client, "/api/v5/market/history-candles",
                                           {"instId": pair, "bar": bar, "after": str(after), "limit": str(HISTORY_PAGE)})
                    break
                except (httpx.HTTPError, RuntimeError):
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1 + attempt)
            if not page:
                break
            for r in page:
                ts = int(r[0]) // 1000
                rows[ts] = (ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]), r[8] == "1")
            oldest = int(page[-1][0]) // 1000
            if oldest <= start or len(page) < HISTORY_PAGE:
                break
            after = oldest * 1000
        return [rows[t] for t in sorted(rows) if t >= start]
