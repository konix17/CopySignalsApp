"""Do the Fear & Greed index or futures funding rates improve the trend bot? Research only.

    .venv/bin/python research/sentiment_funding.py

Daily BTC+ETH from data/history.db (OKX), Fear & Greed from alternative.me (free, since 2018), 8-hour funding rates
from Binance's public futures API (since 2019-09). Each overlay is judged on 2019-10 to 2022 and must hold on
2023 to now. Costs 0.25% per unit traded, as in backtest.py.
"""
import calendar
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import backtest as bt, history  # noqa: E402

DAY = 86400
fg = {int(d["timestamp"]) // DAY * DAY: int(d["value"])
      for d in httpx.get("https://api.alternative.me/fng/?limit=0&format=json", timeout=30).json()["data"]}


def funding(symbol: str) -> dict[int, float]:
    """Daily average 8-hour funding rate."""
    out, start = {}, 1567296000000
    with httpx.Client(timeout=30) as c:
        while True:
            rows = c.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": symbol, "startTime": start, "limit": 1000}).json()
            for r in rows:
                out.setdefault(r["fundingTime"] // 1000 // DAY * DAY, []).append(float(r["fundingRate"]))
            if len(rows) < 1000:
                break
            start = rows[-1]["fundingTime"] + 1
            time.sleep(0.3)
    return {d: statistics.fmean(v) for d, v in out.items()}


fund = {"BTC-USDT": funding("BTCUSDT"), "ETH-USDT": funding("ETHUSDT")}
p = bt.Panel(history.load(history.connect(ROOT / "data" / "history.db"), "1Dutc", ["BTC-USDT", "ETH-USDT"]))
base = bt.TrendEnsemble(("BTC-USDT", "ETH-USDT"), (50, 100, 150))


def avg_funding(pair, day, n=3):
    xs = [fund[pair].get(day - i * DAY) for i in range(n)]
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


@dataclass
class Overlay:
    name: str
    rule: str
    level: float
    state: dict = field(default_factory=dict)

    def weights(self, P, t):
        w = base.weights(P, t)
        day = P.days[t]
        f = fg.get(day)
        if self.rule == "greed_trim" and f is not None and f >= self.level:
            return {k: v / 2 for k, v in w.items()}
        if self.rule == "fear_buy" and f is not None and f <= self.level:
            return {pr: max(w.get(pr, 0), 1 / 6) for pr in base.pairs}  # hold at least a third of each half
        if self.rule == "funding_trim":
            return {k: v / 2 if (avg_funding(k, day) or 0) >= self.level else v for k, v in w.items()}
        if self.rule == "funding_neg_buy_3d":  # same, on the 7-day average instead of 3 days (robustness)
            out = {}
            for pr in base.pairs:
                f = avg_funding(pr, day, 7)
                out[pr] = max(w.get(pr, 0), 1 / 6) if f is not None and f <= self.level else w.get(pr, 0)
            return {k: v for k, v in out.items() if v}
        if self.rule == "funding_neg_buy":
            out = {}
            for pr in base.pairs:
                f = avg_funding(pr, day)  # no data: no signal
                out[pr] = max(w.get(pr, 0), 1 / 6) if f is not None and f <= self.level else w.get(pr, 0)
            return {k: v for k, v in out.items() if v}
        return w


tests = [Overlay("Trend bot (no overlay)", "none", 0)]
tests += [Overlay(f"+ halve when Fear&Greed >= {x}", "greed_trim", x) for x in (75, 80, 90)]
tests += [Overlay(f"+ buy 1/3 when Fear&Greed <= {x}", "fear_buy", x) for x in (10, 15, 25)]
tests += [Overlay(f"+ halve when funding >= {x:.3%}/8h", "funding_trim", x) for x in (0.0003, 0.0005, 0.001)]
tests += [Overlay(f"+ buy 1/3 when funding <= {x:.3%}/8h", "funding_neg_buy", x) for x in (0.0, -0.0001, 0.00005)]
tests += [Overlay("+ same, 7-day average funding <= 0", "funding_neg_buy_3d", 0.0)]

is_start, split = calendar.timegm((2019, 10, 1, 0, 0, 0)), calendar.timegm((2023, 1, 1, 0, 0, 0))
print(f"{'strategy':42} | {'2019-10..2022: a year, worst':>28} | {'2023..now: a year, worst':>26}")
for s in tests:
    a = bt.run(p, s, is_start, split).metrics()
    s.state.clear()
    b = bt.run(p, s, split).metrics()
    print(f"{s.name:42} | {a['cagr']:+8.1%} {a['max_drawdown']:7.0%} Sharpe {a['sharpe']:4.2f} | "
          f"{b['cagr']:+8.1%} {b['max_drawdown']:6.0%} Sharpe {b['sharpe']:4.2f}")
