"""Live OKX prices over a websocket (public tickers channel, pushed up to every 100 ms, no key).

The pipeline tells the stream which pairs matter (open positions and paper trades, today's picks, BTC). The
stream keeps one connection, subscribes and unsubscribes as that list changes, pings to stay connected, and
reconnects with backoff. Readers only get prices that are fresh; anything else falls back to REST.
"""

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger(__name__)

# Accounts opened in Europe use the EEA servers; public market data is the same on both.
WS_URLS = {"eea": "wss://wseea.okx.com:8443/ws/v5/public", "global": "wss://ws.okx.com:8443/ws/v5/public"}
PING_S = 20  # OKX drops connections that are quiet for 30 seconds
FRESH_S = 30  # a price older than this isn't used
BATCH = 50  # pairs per subscribe message
MAX_BACKOFF = 60


class TickerStream:
    def __init__(self, region: str = "eea"):
        self.url = WS_URLS.get(region, WS_URLS["eea"])
        self.wanted: set[str] = set()
        self.ticks: dict[str, tuple[float, float]] = {}  # pair -> (last price, received at)
        self.connected = False
        self.connected_at: float | None = None
        self._subscribed: set[str] = set()

    def want(self, pairs) -> None:
        """Set the pairs to stream; the connection catches up within a second."""
        self.wanted = set(pairs)

    def prices(self, pairs, max_age: float = FRESH_S, now: float | None = None) -> dict[str, float]:
        now = now or time.time()
        out = {}
        for p in pairs:
            tick = self.ticks.get(p)
            if tick and now - tick[1] <= max_age:
                out[p] = tick[0]
        return out

    def status(self) -> dict:
        return {"connected": self.connected, "since": int(self.connected_at) if self.connected_at else None,
                "pairs": len(self._subscribed), "fresh": len(self.prices(self._subscribed))}

    def handle(self, raw: str, now: float | None = None) -> None:
        if raw == "pong":
            return
        msg = json.loads(raw)
        if msg.get("event") == "error":
            log.warning("OKX stream: %s", msg.get("msg"))
            return
        if (msg.get("arg") or {}).get("channel") != "tickers":
            return
        for d in msg.get("data") or []:
            if d.get("last"):
                self.ticks[d["instId"]] = (float(d["last"]), now or time.time())

    async def _sync(self, ws) -> None:
        wanted = set(self.wanted)
        for op, pairs in (("unsubscribe", self._subscribed - wanted), ("subscribe", wanted - self._subscribed)):
            pairs = sorted(pairs)
            for i in range(0, len(pairs), BATCH):
                await ws.send(json.dumps({"op": op, "args": [{"channel": "tickers", "instId": p}
                                                             for p in pairs[i:i + BATCH]]}))
        for p in self._subscribed - wanted:
            self.ticks.pop(p, None)
        self._subscribed = wanted

    async def _session(self) -> None:
        async with websockets.connect(self.url, open_timeout=15, ping_interval=None, close_timeout=5) as ws:
            self._subscribed = set()
            self.connected, self.connected_at = True, time.time()
            log.info("OKX price stream connected")
            last_sent = time.monotonic()
            while True:
                if self.wanted != self._subscribed:
                    await self._sync(ws)
                    last_sent = time.monotonic()
                try:
                    self.handle(await asyncio.wait_for(ws.recv(), timeout=1))
                except TimeoutError:
                    pass
                if time.monotonic() - last_sent >= PING_S:
                    await ws.send("ping")
                    last_sent = time.monotonic()

    async def run(self) -> None:
        backoff = 1
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("OKX price stream dropped: %s", e)
            finally:
                self.connected = False
            if time.monotonic() - started > 60:
                backoff = 1  # it was up for a while: reconnect quickly
            await asyncio.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)
