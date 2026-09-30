"""GMX v2 perps on Arbitrum. Reads the public Subsquid indexer + GMX REST API.

All USD amounts in the indexer are 30-decimal fixed point; token prices are
scaled by 10^(30 - token decimals).
"""

import asyncio
import time

import httpx

from ..config import settings
from ..models import PERP, Position, TraderStat
from ..symbols import crypto_symbol

SQUID_URL = "https://gmx.squids.live/gmx-synthetics-arbitrum:prod/api/graphql"
REST_URL = "https://arbitrum-api.gmxinfra.io"
USD = 10**30
PAGE = 1000
MAX_PAGES = 15
DAY = 86400
# Long windows are slow to query (all-time pages take ~15s each) and barely move: reuse them.
CACHE_S = {"day": 0, "week": 0, "month": 3600, "allTime": 6 * 3600}


def window_starts(now: float) -> dict[str, int]:
    """The indexer requires `from` on a UTC midnight, so "day" spans yesterday + today."""
    midnight = int(now) // DAY * DAY
    return {"day": midnight - DAY, "week": midnight - 7 * DAY, "month": midnight - 30 * DAY, "allTime": 0}


def market_symbol(name: str) -> str | None:
    """'ETH/USD [WETH-USDC]' -> 'ETH'. Swap-only and non-crypto markets return None."""
    if "/" not in name:
        return None
    return crypto_symbol(name.split("/")[0])


class Gmx:
    name = "gmx"

    def __init__(self):
        self._markets: dict[str, tuple[str, str]] | None = None  # market addr -> (symbol, index token addr)
        self._decimals: dict[str, int] = {}
        self._stats_cache: dict[str, tuple[float, list[TraderStat]]] = {}

    async def _gql(self, client: httpx.AsyncClient, query: str, attempts: int = 3) -> dict:
        # The indexer sometimes drops the connection on slow queries (all-time stats); retry those.
        for attempt in range(attempts):
            try:
                resp = await client.post(SQUID_URL, json={"query": query}, timeout=60)
                break
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(body["errors"][0]["message"])
        return body["data"]

    async def _load_markets(self, client: httpx.AsyncClient) -> None:
        if self._markets is not None:
            return
        markets = (await client.get(f"{REST_URL}/markets")).json()["markets"]
        tokens = (await client.get(f"{REST_URL}/tokens")).json()["tokens"]
        self._decimals = {t["address"].lower(): int(t["decimals"]) for t in tokens}
        self._markets = {}
        for m in markets:
            symbol = market_symbol(m["name"])
            if symbol:
                self._markets[m["marketToken"].lower()] = (symbol, m["indexToken"].lower())

    async def _index_prices(self, client: httpx.AsyncClient) -> dict[str, float]:
        """index token addr -> USD price."""
        tickers = (await client.get(f"{REST_URL}/prices/tickers")).json()
        out = {}
        for t in tickers:
            addr = t["tokenAddress"].lower()
            if addr in self._decimals:
                mid = (int(t["minPrice"]) + int(t["maxPrice"])) / 2
                out[addr] = mid * 10 ** self._decimals[addr] / USD
        return out

    async def fetch_stats(self, client: httpx.AsyncClient) -> list[TraderStat]:
        now = time.time()
        out = []
        for window, start in window_starts(now).items():
            cached = self._stats_cache.get(window)
            if cached and now - cached[0] < CACHE_S[window]:
                rows = cached[1]
            else:
                rows = await self._window_stats(client, window, start)
                self._stats_cache[window] = (now, rows)
            # Fresh objects: scoring mutates .score in place.
            out += [TraderStat(**vars(r)) for r in rows]
        return out

    async def _window_stats(self, client: httpx.AsyncClient, window: str, start: int) -> list[TraderStat]:
        min_capital = str(int(settings.gmx_min_capital_usd * USD))
        out = []
        for page in range(MAX_PAGES):
            data = await self._gql(
                client,
                f'{{ periodAccountStats(limit: {PAGE}, offset: {page * PAGE}, '
                f'where: {{from: {start}, maxCapital_gte: "{min_capital}"}}) '
                f"{{ id realizedPnl maxCapital volume wins losses closedCount }} }}",
            )
            rows = data["periodAccountStats"]
            for r in rows:
                if r["closedCount"] == 0:  # no closed trades in this window
                    continue
                capital = int(r["maxCapital"]) / USD
                pnl = int(r["realizedPnl"]) / USD
                decided = r["wins"] + r["losses"]
                out.append(
                    TraderStat(
                        source=self.name,
                        address=r["id"],
                        window=window,
                        pnl=pnl,
                        roi=pnl / capital if capital else None,
                        volume=int(r["volume"]) / USD,
                        account_value=capital,
                        win_rate=r["wins"] / decided if decided >= 3 else None,
                    )
                )
            if len(rows) < PAGE:
                break
        return out

    async def fetch_positions(self, client: httpx.AsyncClient, addresses: list[str]) -> tuple[list[Position], set[str]]:
        await self._load_markets(client)
        prices = await self._index_prices(client)
        out = []
        for i in range(0, len(addresses), 100):
            batch = ", ".join(f'"{a}"' for a in addresses[i : i + 100])
            data = await self._gql(
                client,
                f'{{ positions(limit: 1000, where: {{account_in: [{batch}], isSnapshot_eq: false, sizeInUsd_gt: "0"}}) '
                f"{{ account market isLong sizeInUsd entryPrice leverage openedAt }} }}",
            )
            for p in data["positions"]:
                market = self._markets.get(p["market"].lower())
                if not market or not p["entryPrice"]:
                    continue
                symbol, index_token = market
                if index_token not in prices:
                    continue
                entry = int(p["entryPrice"]) * 10 ** self._decimals[index_token] / USD
                mark = prices[index_token]
                size = int(p["sizeInUsd"]) / USD
                sign = 1 if p["isLong"] else -1
                out.append(
                    Position(
                        source=self.name,
                        address=p["account"],
                        market_key=f"perp:{symbol}",
                        asset_class=PERP,
                        symbol=symbol,
                        title=f"{symbol} perpetual",
                        direction="long" if p["isLong"] else "short",
                        size_usd=size,
                        entry_price=entry,
                        mark_price=mark,
                        price_key=f"perp:{symbol}",
                        leverage=int(p["leverage"]) / 10_000 if p["leverage"] else None,
                        unrealized_pnl=size * (mark / entry - 1) * sign if entry else None,
                        opened_at=p["openedAt"],
                        url="https://app.gmx.io/#/trade",
                    )
                )
        # One query per batch: any failure raises, so reaching here means every address was read.
        return out, set(addresses)

    async def fetch_prices(self, client: httpx.AsyncClient, price_keys: set[str]) -> dict[str, float]:
        await self._load_markets(client)
        prices = await self._index_prices(client)
        out = {}
        for symbol, index_token in self._markets.values():
            key = f"perp:{symbol}"
            if key in price_keys and index_token in prices:
                out[key] = prices[index_token]
        return out
