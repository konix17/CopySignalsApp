"""Buy the dip while the market rises: the one pattern the copy-trading search pointed at (copy_patterns.py), tested
without traders on 8 years of daily prices. Research only.

    .venv/bin/python research/dip_backtest.py [--cost 0.005]

Binance daily candles of 675 USDT pairs, delisted coins (LUNA, FTT…) included, so there's no survivor bias. Each day
the 50 most traded coins with 100+ days of history are candidates. A coin that has fallen at least X% over 7 days is
bought at the close, if BTC is above its 50-day average (or always, for comparison), and sold K days later.
Settings are chosen on 2018-09..2022 and then judged on 2023..now. Results per trade are averaged per month first
(dips come in waves on the same days), with a 95% range from resampling months. "vs random" = the same coin held K
days from a random day in the same period.
"""
import argparse
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import backtest, history  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--cost", type=float, default=0.005)
args = ap.parse_args()
random.seed(5)
conn = history.connect(ROOT / "data" / "history.db")
panel = backtest.Panel(history.load(conn, "binance-1d"))
BTC = "BTC-USDT"
day_of = {d: i for i, d in enumerate(panel.days)}
start = panel.index_of(1_535_760_000)  # 2018-09-01
split = panel.index_of(1_672_531_200)  # 2023-01-01
end = len(panel.days) - 1
universe = {t: panel.universe(t, 50, 100) for t in range(start, end)}


def btc_up(t):
    m = panel.sma(BTC, t, 50)
    return m is not None and panel.close[BTC][t] > m


def trades(drop, hold, need_up, lo, hi):
    out = []
    for t in range(lo, min(hi, end - hold)):
        if need_up and not btc_up(t):
            continue
        for p in universe[t]:
            r7 = panel.ret(p, t, 7)
            a, b = panel.close[p][t], panel.close[p][t + hold]
            if r7 is None or r7 > -drop or not (a and b):
                continue
            s = random.randint(lo, min(hi, end - hold) - 1)  # the same coin from a random day
            pa, pb = panel.close[p][s], panel.close[p][s + hold]
            out.append({"month": time.strftime("%Y-%m", time.gmtime(panel.days[t])), "net": b / a - 1 - args.cost,
                        "vs_random": (b / a - 1) - (pb / pa - 1) if pa and pb else None})
    return out


def by_month(xs, key):
    per = defaultdict(list)
    for x in xs:
        if x[key] is not None:
            per[x["month"]].append(x[key])
    means = [statistics.fmean(v) for v in per.values()]
    if len(means) < 6:
        return float("nan"), float("nan"), float("nan"), len(means)
    boots = sorted(statistics.fmean(random.choices(means, k=len(means))) for _ in range(2000))
    return statistics.fmean(means), boots[50], boots[1950], len(means)


def line(label, xs):
    n, lo, hi, m = by_month(xs, "net")
    v, vlo, vhi, _ = by_month(xs, "vs_random")
    won = sum(x["net"] > 0 for x in xs) / len(xs) if xs else float("nan")
    return (f"{label:40} {len(xs):6,} buys in {m:3} months, won {won:4.0%} | net {n:+6.2%} [{lo:+.2%}, {hi:+.2%}] | "
            f"vs random days {v:+6.2%} [{vlo:+.2%}, {vhi:+.2%}]"), n


grid = [(d, k, up) for d in (0.10, 0.20, 0.30) for k in (3, 7, 14) for up in (True, False)]
print(f"Buy a top-50 coin after a 7-day fall, hold K days (after {args.cost:.2%} costs; per month, [95% range])\n")
print("Learning period 2018-09 .. 2022 (choose the settings here):")
scored = []
for d, k, up in grid:
    text, n = line(f"fall {d:.0%}+, hold {k:2}d, {'BTC uptrend only' if up else 'any market'}",
                   trades(d, k, up, start, split))
    print("  " + text)
    scored.append((n, d, k, up))
best = sorted(scored, reverse=True)[:3]
print("\nTest period 2023 .. now, the 3 best settings from the learning period:")
for _, d, k, up in best:
    text, _ = line(f"fall {d:.0%}+, hold {k:2}d, {'BTC uptrend only' if up else 'any market'}", trades(d, k, up, split, end))
    print("  " + text)
print("\nFor reference, the same test period, every setting:")
for d, k, up in grid:
    text, _ = line(f"fall {d:.0%}+, hold {k:2}d, {'BTC uptrend only' if up else 'any market'}", trades(d, k, up, split, end))
    print("  " + text)
