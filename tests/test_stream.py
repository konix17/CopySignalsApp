"""OKX's live price stream (stream.py)."""

import asyncio
import json

from app.stream import TickerStream


def test_stream_keeps_fresh_ticker_prices_only():
    s = TickerStream("eea")
    s.handle(json.dumps({"arg": {"channel": "tickers", "instId": "BTC-USDT"},
                         "data": [{"instId": "BTC-USDT", "last": "84000.5"}]}), now=1000)
    s.handle("pong")
    s.handle(json.dumps({"event": "subscribe", "arg": {"channel": "tickers", "instId": "ETH-USDT"}}))
    assert s.prices(["BTC-USDT", "ETH-USDT"], now=1010) == {"BTC-USDT": 84000.5}
    assert s.prices(["BTC-USDT"], now=1000 + 60) == {}


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))


def test_stream_subscribes_to_changes_in_batches():
    s, ws = TickerStream("eea"), FakeWs()
    s.want([f"C{i}-USDT" for i in range(60)])
    asyncio.run(s._sync(ws))
    assert [m["op"] for m in ws.sent] == ["subscribe", "subscribe"]
    assert sum(len(m["args"]) for m in ws.sent) == 60
    s.ticks["C0-USDT"] = (1.0, 0)
    ws.sent.clear()
    s.want(["C1-USDT", "NEW-USDT"])
    asyncio.run(s._sync(ws))
    assert [m["op"] for m in ws.sent] == ["unsubscribe", "unsubscribe", "subscribe"]
    assert sum(len(m["args"]) for m in ws.sent[:2]) == 59
    assert ws.sent[2] == {"op": "subscribe", "args": [{"channel": "tickers", "instId": "NEW-USDT"}]}
    assert "C0-USDT" not in s.ticks
