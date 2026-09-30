"""Search for a small-coin strategy that works: pumps, breakouts, dumps. Research only.

    .venv/bin/python research/pump_dump_explore.py [--cost 0.0065]

Uses data/research.db (5-minute Binance candles from research/rising_backtest.py) and the point-in-time universe
(coins ranked 21-150 by volume, >= $3M a day). Every event family below has a small, fixed grid of settings. Rules are
judged on April-July (in-sample) and must then hold on August-September (out-of-sample), which they never saw.
Returns are per trade after `cost` (round trip), entering at the close of the signal bar and exiting at the close
after the holding time. One event per coin per holding period.
"""
import argparse
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import backtest as bt, history  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--cost", type=float, default=0.0065)
args = ap.parse_args()
DAY, BAR = 86400, 300
SPLIT = 1785542400  # 2026-08-01 00:00 UTC: in-sample before, out-of-sample after
HOLDS = {"15m": 3, "1h": 12, "4h": 48, "24h": 288}

hist = history.connect(ROOT / "data" / "history.db")
daily = bt.Panel(history.load(hist, "binance-1d"))
res = history.connect(ROOT / "data" / "research.db")
t_first = res.execute("SELECT MIN(ts) FROM candles WHERE bar='binance-5m'").fetchone()[0]

allowed: dict[str, set[int]] = defaultdict(set)
for t in range(daily.index_of(t_first), len(daily.days)):
    ranked = sorted((c for c in daily.pairs if daily.age(c, t) >= 30), key=lambda c: -daily.avg_volume(c, t, 30))
    for c in ranked[20:150]:
        if daily.avg_volume(c, t, 30) >= 3e6:
            allowed[c.split("#")[0]].add(daily.days[t])
cols = defaultdict(list)
for p in daily.pairs:
    cols[p.split("#")[0]].append(p)


def ma50_ok(pair: str, day: int, price: float) -> bool | None:
    col = next((p for p in cols[pair] if daily.close[p][daily.index_of(day)] is not None), None)
    if col is None:
        return None
    m = daily.sma(col, daily.index_of(day) - 1, 50)
    return None if m is None else price > m


btc_up = {}
for t in range(daily.index_of(t_first), len(daily.days)):
    m = daily.sma("BTC-USDT", t - 1, 50)
    btc_up[daily.days[t]] = m is not None and daily.close["BTC-USDT"][t - 1] > m

# rule name -> hold -> list of (ts, net)
results: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))

pairs = [r[0] for r in res.execute("SELECT DISTINCT pair FROM candles WHERE bar='binance-5m'")]
t0 = time.time()
for n, pair in enumerate(pairs):
    if pair not in allowed:
        continue
    rows = res.execute("SELECT ts, open, high, low, close, volume_usd FROM candles WHERE bar='binance-5m' AND pair=? "
                       "ORDER BY ts", (pair,)).fetchall()
    if len(rows) < 3000:
        continue
    ts = [r[0] for r in rows]; o = [r[1] for r in rows]; h = [r[2] for r in rows]
    lo = [r[3] for r in rows]; c = [r[4] for r in rows]; q = [r[5] for r in rows]
    qs = [0.0]
    for x in q:
        qs.append(qs[-1] + x)
    last_event: dict[tuple[str, str], int] = {}
    trend_cache = {}
    for t in range(2016, len(rows) - 289):
        day = ts[t] - ts[t] % DAY
        if day not in allowed[pair]:
            continue
        vol24 = qs[t + 1] - qs[t - 287]
        if vol24 <= 0:
            continue
        pace = vol24 / 288  # normal 5-minute volume
        surge1h = (qs[t + 1] - qs[t - 11]) / (pace * 12)
        surge15 = (qs[t + 1] - qs[t - 2]) / (pace * 3)
        ch15 = c[t] / o[t - 2] - 1
        ch1h = c[t] / o[t - 11] - 1
        ch24 = c[t] / c[t - 288] - 1
        events = []
        # 1. pump continuation: the live Rising now trigger, and a stronger version
        if ch15 >= 0.02 and ch1h >= 0.03 and surge15 >= 3 and c[t] >= max(h[t - 2: t + 1]) * 0.985 and ch24 <= 0.4:
            events.append("pump: live trigger")
            if surge15 >= 10 and ch1h >= 0.06:
                events.append("pump: strong (10x vol, +6%/1h)")
        # 2. breakout: new 7-day high on heavy volume
        if surge1h >= 5 and c[t] > max(h[t - 2016: t - 12]) and ch1h >= 0.02:
            events.append("breakout: 7-day high, 5x vol")
        # 3. dump reversal: fast crash on heavy volume
        for drop in (0.08, 0.12, 0.20):
            if ch1h <= -drop and surge1h >= 3:
                events.append(f"dump: -{drop:.0%} in 1h, buy bounce")
        # 4. after a pump collapses: up 20%+ in the last 24h at some point, now 15%+ off the 24h high
        peak24 = max(h[t - 287: t + 1])
        if peak24 / min(lo[t - 287: t + 1]) - 1 >= 0.20 and c[t] <= peak24 * 0.85 and ch15 < 0:
            events.append("after pump: -15% from 24h peak")
        if not events:
            continue
        if day not in trend_cache:
            trend_cache[day] = (btc_up.get(day), ma50_ok(pair, day, c[t]))
        btc_ok, coin_ok = trend_cache[day]
        for e in events:
            names = [e]
            if btc_ok and coin_ok:
                names.append(e + " | uptrend")
            for hold, k in HOLDS.items():
                for name in names:
                    key = (name, hold)
                    if t < last_event.get(key, -1):
                        continue
                    last_event[key] = t + k
                    net = c[t + k] / c[t] - 1 - args.cost
                    results[name][hold].append((ts[t], net))
    if n % 60 == 0:
        print(f"  {n}/{len(pairs)} pairs, {time.time() - t0:.0f}s", flush=True)


def stats(xs):
    if len(xs) < 20:
        return None
    m = statistics.fmean(xs)
    se = statistics.pstdev(xs) / math.sqrt(len(xs))
    return m, se, len(xs)


print(f"\ncost {args.cost:.2%} round trip; in-sample Apr-Jul, out-of-sample Aug-Sep; avg per trade (± 2 s.e.), n")
print(f"{'rule':44} {'hold':>4} | {'in-sample':>22} | {'out-of-sample':>22}")
passed = []
for name in sorted(results):
    for hold in HOLDS:
        xs = results[name][hold]
        a = stats([x for ts_, x in xs if ts_ < SPLIT])
        b = stats([x for ts_, x in xs if ts_ >= SPLIT])
        if not a or not b:
            continue
        fa = f"{a[0]:+.2%} ±{2 * a[1]:.2%} n={a[2]}"
        fb = f"{b[0]:+.2%} ±{2 * b[1]:.2%} n={b[2]}"
        flag = " <= positive in both" if a[0] - 2 * a[1] > 0 and b[0] > 0 else ""
        if flag:
            passed.append((name, hold))
        print(f"{name:44} {hold:>4} | {fa:>22} | {fb:>22}{flag}")
print("\nRules clearly positive in-sample AND positive out-of-sample:", passed or "none")
