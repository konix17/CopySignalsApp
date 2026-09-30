import asyncio
from typing import Protocol

import httpx

from ..models import Position, TraderStat


class Source(Protocol):
    """A public venue whose traders' performance and positions can be read.

    To add a venue: implement these three methods and register it in
    sources/__init__.py.
    """

    name: str

    async def fetch_stats(self, client: httpx.AsyncClient) -> list[TraderStat]:
        """Performance for every trader worth considering, one row per window."""
        ...

    async def fetch_positions(self, client: httpx.AsyncClient, addresses: list[str]) -> tuple[list[Position], set[str]]:
        """Current open positions for the given traders, plus the addresses that were
        actually read. An address missing from that set failed to load: its positions
        must not be treated as closed."""
        ...

    async def fetch_prices(self, client: httpx.AsyncClient, price_keys: set[str]) -> dict[str, float]:
        """Current prices for any of `price_keys` this source can price."""
        ...


async def gather_limited(coros, limit: int = 8):
    """Run coroutines with bounded concurrency; exceptions are returned, not raised."""
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
