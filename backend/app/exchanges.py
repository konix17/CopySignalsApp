"""Centralized exchanges: how each exchange's top traders are positioned per coin.

Uses only official public market-data endpoints (no keys, no scraping):
- Binance USDⓈ-M futures: top-trader long/short *position* ratio, funding
- OKX swaps: top-trader long/short position ratio

"Top traders" is each exchange's own definition (e.g. Binance: top 20% of
accounts by margin balance). This is aggregate positioning, not individual
trades. It's used as a crowding check: when top traders are already heavily
long, a coin is more exposed to a long squeeze.
"""

import asyncio

import httpx

from .models import Positioning
from .sources.base import gather_limited

BINANCE = "https://fapi.binance.com"
OKX = "https://www.okx.com"
HOURS = 25  # hourly points: now and 24h ago


def _strip_multiplier(base: str) -> str:
    """Binance lists some small-cap coins per 1000 or per million units: 1000PEPE, 1000000MOG, 1MBABYDOGE."""
    for prefix in ("1000000", "1000", "1M"):
        if base.startswith(prefix) and len(base) > len(prefix) and base[len(prefix)].isalpha():
            return base[len(prefix):]
    return base


class Binance:
    name = "binance"

    def __init__(self):
        self._symbols: dict[str, str] | None = None  # coin -> Binance symbol

    async def symbols(self, client: httpx.AsyncClient) -> dict[str, str]:
        if self._symbols is None:
            resp = await client.get(f"{BINANCE}/fapi/v1/exchangeInfo")
            resp.raise_for_status()
            out: dict[str, str] = {}
            for s in resp.json()["symbols"]:
                if s["status"] == "TRADING" and s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT":
                    coin = _strip_multiplier(s["baseAsset"])
                    if coin not in out or s["baseAsset"] == coin:  # prefer the exact listing
                        out[coin] = s["symbol"]
            self._symbols = out
        return self._symbols

    async def _ratio(self, client: httpx.AsyncClient, coin: str, symbol: str, funding: dict) -> Positioning:
        resp = await client.get(
            f"{BINANCE}/futures/data/topLongShortPositionRatio", params={"symbol": symbol, "period": "1h", "limit": HOURS}
        )
        resp.raise_for_status()
        rows = resp.json()  # oldest first
        return Positioning(
            exchange=self.name,
            symbol=coin,
            long_share=float(rows[-1]["longAccount"]),
            long_share_24h=float(rows[0]["longAccount"]) if len(rows) >= HOURS else None,
            funding=funding.get(symbol),
        )

    async def fetch_positioning(self, client: httpx.AsyncClient, coins: list[str]) -> list[Positioning]:
        listed = await self.symbols(client)
        resp = await client.get(f"{BINANCE}/fapi/v1/premiumIndex")
        resp.raise_for_status()
        funding = {r["symbol"]: float(r["lastFundingRate"]) for r in resp.json()}
        wanted = [(c, listed[c]) for c in coins if c in listed]
        results = await gather_limited([self._ratio(client, c, s, funding) for c, s in wanted], limit=5)
        return [r for r in results if isinstance(r, Positioning)]


class Okx:
    name = "okx"

    def __init__(self):
        self._instruments: set[str] | None = None

    async def fetch_positioning(self, client: httpx.AsyncClient, coins: list[str]) -> list[Positioning]:
        if self._instruments is None:
            resp = await client.get(f"{OKX}/api/v5/public/instruments", params={"instType": "SWAP"})
            resp.raise_for_status()
            self._instruments = {i["instId"] for i in resp.json()["data"]}
        out = []
        for coin in coins:
            inst = f"{coin}-USDT-SWAP"
            if inst not in self._instruments:
                continue
            await asyncio.sleep(0.45)  # rubik endpoints allow 5 requests / 2s
            try:
                resp = await client.get(
                    f"{OKX}/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader",
                    params={"instId": inst, "period": "1H", "limit": HOURS},
                )
                rows = resp.json().get("data") or []  # newest first: [ts, long/short ratio]
            except (httpx.HTTPError, ValueError):
                continue
            if not rows:
                continue
            share = lambda r: float(r[1]) / (1 + float(r[1]))  # noqa: E731
            out.append(
                Positioning(self.name, coin, share(rows[0]), share(rows[-1]) if len(rows) >= HOURS else None)
            )
        return out


REGISTRY = {"binance": Binance, "okx": Okx}


def build_exchanges(names: tuple[str, ...]) -> list:
    unknown = set(names) - REGISTRY.keys()
    if unknown:
        raise ValueError(f"Unknown exchanges {sorted(unknown)}; available: {sorted(REGISTRY)}")
    return [REGISTRY[n]() for n in names]
