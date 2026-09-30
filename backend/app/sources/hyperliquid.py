"""Hyperliquid perps. Public leaderboard + per-account state, no auth.

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
"""

import httpx

from ..config import settings
from ..models import PERP, Position, TraderStat
from ..scoring import max_drawdown
from ..symbols import crypto_symbol
from .base import gather_limited

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def normalize_coin(coin: str) -> tuple[str | None, float]:
    """Map a Hyperliquid coin to a cross-venue symbol and a price multiplier.

    "kPEPE" is quoted per 1000 PEPE, so its price is divided by 1000.
    Non-crypto markets (builder-deployed "xyz:TSLA" etc.) map to None.
    """
    if len(coin) > 1 and coin[0] == "k" and coin[1:].isupper():
        return crypto_symbol(coin[1:]), 1 / 1000
    return crypto_symbol(coin), 1.0


class Hyperliquid:
    name = "hyperliquid"

    async def fetch_stats(self, client: httpx.AsyncClient) -> list[TraderStat]:
        resp = await client.get(LEADERBOARD_URL, timeout=90)
        resp.raise_for_status()
        out = []
        for row in resp.json()["leaderboardRows"]:
            account_value = float(row["accountValue"])
            if account_value < settings.hl_min_account_usd:
                continue
            for window, perf in row["windowPerformances"]:
                volume = float(perf["vlm"])
                if volume <= 0:  # didn't trade in this window
                    continue
                out.append(
                    TraderStat(
                        source=self.name,
                        address=row["ethAddress"],
                        window=window,
                        pnl=float(perf["pnl"]),
                        roi=float(perf["roi"]),
                        volume=volume,
                        account_value=account_value,
                        name=row.get("displayName"),
                    )
                )
        return out

    async def _state(self, client: httpx.AsyncClient, address: str) -> list[Position]:
        resp = await client.post(INFO_URL, json={"type": "clearinghouseState", "user": address})
        resp.raise_for_status()
        out = []
        for ap in resp.json().get("assetPositions", []):
            p = ap["position"]
            size = float(p["szi"])
            if size == 0:
                continue
            symbol, mult = normalize_coin(p["coin"])
            if symbol is None:
                continue
            value = float(p["positionValue"])
            out.append(
                Position(
                    source=self.name,
                    address=address,
                    market_key=f"perp:{symbol}",
                    asset_class=PERP,
                    symbol=symbol,
                    title=f"{symbol} perpetual",
                    direction="long" if size > 0 else "short",
                    size_usd=value,
                    entry_price=float(p["entryPx"]) * mult,
                    mark_price=value / abs(size) * mult,
                    price_key=f"perp:{symbol}",
                    leverage=float(p["leverage"]["value"]),
                    unrealized_pnl=float(p["unrealizedPnl"]),
                    url=f"https://app.hyperliquid.xyz/trade/{p['coin']}",
                )
            )
        return out

    async def fetch_positions(self, client: httpx.AsyncClient, addresses: list[str]) -> tuple[list[Position], set[str]]:
        # clearinghouseState costs 2 weight of the 1200/min budget; a few hundred calls is fine.
        results = await gather_limited([self._state(client, a) for a in addresses], limit=6)
        ok = {a for a, r in zip(addresses, results) if not isinstance(r, BaseException)}
        return [p for r in results if not isinstance(r, BaseException) for p in r], ok

    async def fetch_prices(self, client: httpx.AsyncClient, price_keys: set[str]) -> dict[str, float]:
        resp = await client.post(INFO_URL, json={"type": "allMids"})
        resp.raise_for_status()
        out = {}
        for coin, mid in resp.json().items():
            if coin.startswith("@"):  # spot pairs
                continue
            symbol, mult = normalize_coin(coin)
            if symbol is None:
                continue
            out[f"perp:{symbol}"] = float(mid) * mult
        return {k: v for k, v in out.items() if k in price_keys}

    async def _drawdown(self, client: httpx.AsyncClient, address: str) -> float:
        resp = await client.post(INFO_URL, json={"type": "portfolio", "user": address})
        resp.raise_for_status()
        month = dict(resp.json())["month"]
        pnl = [float(v) for _, v in month["pnlHistory"]]
        equity = [float(v) for _, v in month["accountValueHistory"]]
        return max_drawdown(pnl, equity)

    async def fetch_drawdowns(self, client: httpx.AsyncClient, addresses: list[str]) -> dict[str, float]:
        """Last month's max drawdown per trader. The portfolio call is heavy, so callers cache and batch it."""
        results = await gather_limited([self._drawdown(client, a) for a in addresses], limit=3)
        return {a: r for a, r in zip(addresses, results) if isinstance(r, float)}
