"""Backtest of the Rising now rules (early movers and pump rides) on Binance 5-minute candles. Research only.

    .venv/bin/python research/rising_backtest.py [--months 6] [--cost 0.0065] [--mode live|all_early] [--no-fetch]

Downloads (once, then only what's new) 5-minute candles for every coin that was "smaller but liquid" at some point:
ranked 21-150 by 30-day volume with at least $3M a day, from Binance daily history in data/history.db (run
`python -m app.manage backtest` first), dead coins included. Stored in data/research.db (about 1 GB for 6 months).

Result on 2026-03-24 to 2026-09-30: 3,683 flags, -0.64% per trade after costs, every month negative, no better than
random entries (-0.48%), so automatic demo trading no longer buys Early and Pump picks by default.

Detection mirrors movers.detect on 5-minute bars: +2% over 15 min, +3% over 1 h, 15-min volume >= 3x the normal
pace, close within 1.5% of the 15-min high, not up more than 40% in 24 h. The app's own score_move and
pumps.analyze decide the score and Early vs Pump ride. Exits mirror the live rules: stop, target, trailing stop
(pump), pump topping/dumping, hold time. One trade per coin at a time. The live scanner uses 1-minute candles every
15 seconds and also scores smart-trader holdings, which this can't replay.
"""
import argparse
import asyncio
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import backtest as bt, history, movers, pumps  # noqa: E402
from app.market import Market, trend_from_closes  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--months", type=float, default=6)
ap.add_argument("--cost", type=float, default=0.0065, help="round trip: 2 x (0.20%% fee + 0.075%% half spread + 0.05%% slippage)")
ap.add_argument("--mode", choices=["live", "all_early"], default="live", help="all_early: no pump rides")
ap.add_argument("--no-fetch", action="store_true")
args = ap.parse_args()
COST, MODE = args.cost, args.mode
BAR = 300
DAY = 86400
ROOT = Path(__file__).resolve().parents[1]

hist = history.connect(ROOT / "data" / "history.db")
daily = bt.Panel(history.load(hist, "binance-1d"))
res = history.connect(ROOT / "data" / "research.db")
t_start = int(time.time() - args.months * 30.5 * DAY)

# Universe: coins ranked 21-150 by 30-day volume with >= $3M a day, per day (point in time)
allowed: dict[str, set[int]] = defaultdict(set)
for t in range(daily.index_of(t_start), len(daily.days)):
    ranked = sorted((c for c in daily.pairs if daily.age(c, t) >= 30), key=lambda c: -daily.avg_volume(c, t, 30))
    for c in ranked[20:150]:
        if daily.avg_volume(c, t, 30) >= 3e6:
            allowed[c.split("#")[0]].add(daily.days[t])
if not args.no_fetch:
    coins = sorted({p.split("-")[0] for p in allowed})
    print(f"Fetching 5-minute candles for {len(coins)} coins…", flush=True)
    asyncio.run(history.update_binance(res, coins=coins, interval="5m", since=t_start - 7 * DAY))
pairs = [r[0] for r in res.execute("SELECT DISTINCT pair FROM candles WHERE bar = 'binance-5m'")]
t_first = t_start


COLS = defaultdict(list)
for _p in daily.pairs:
    COLS[_p.split("#")[0]].append(_p)


def day_info(pair: str, day: int):
    """ma20, ma50, daily vol from closes before `day` (as the live app has at that time)."""
    col = next((p for p in COLS[pair] if daily.close[p][daily.index_of(day)] is not None), None)
    if col is None:
        return None, None, None
    t = daily.index_of(day) - 1
    closes = [x for x in daily.close[col][max(0, t - 60): t + 1] if x]
    ma20, ma50, _, vol = trend_from_closes(closes)
    return ma20, ma50, vol


trades = []
baseline = []
t0 = time.time()
for n, pair in enumerate(pairs):
    coin = pair.split("-")[0]
    if pair not in allowed:
        continue
    rows = res.execute("SELECT ts, open, high, low, close, volume_usd FROM candles WHERE bar='binance-5m' AND pair=? "
                       "AND ts >= ? ORDER BY ts", (pair, t_start - 2 * DAY)).fetchall()
    if len(rows) < 400:
        continue
    ts = [r[0] for r in rows]; o = [r[1] for r in rows]; h = [r[2] for r in rows]
    lo = [r[3] for r in rows]; c = [r[4] for r in rows]; q = [r[5] for r in rows]
    qsum = [0.0]
    for x in q:
        qsum.append(qsum[-1] + x)
    info_cache = {}
    busy_until = 0
    prev_flag = False
    for t in range(288, len(rows) - 1):
        day = ts[t] - ts[t] % DAY
        if day not in allowed[pair]:
            prev_flag = False
            continue
        vol24 = qsum[t + 1] - qsum[t - 287]
        if vol24 <= 0:
            continue
        price = c[t]
        ch15 = price / o[t - 2] - 1
        ch60 = price / o[t - 11] - 1
        surge = (qsum[t + 1] - qsum[t - 2]) / (vol24 / 96)
        high15 = max(h[t - 2: t + 1])
        ch24 = price / c[t - 288] - 1
        flag = (ch15 >= movers.MIN_CHANGE_15M and ch60 >= movers.MIN_CHANGE_1H and surge >= movers.MIN_VOLUME_SURGE
                and price >= high15 * movers.NEAR_HIGH and ch24 <= movers.MAX_DAY_CHANGE)
        new = flag and not prev_flag
        prev_flag = flag
        # A no-signal baseline: every 500th bar, same exits as an early mover
        if not new and t % 500 == 0 and ts[t] >= busy_until:
            kind = "baseline"
        elif new and ts[t] >= busy_until:
            kind = "signal"
        else:
            continue
        if day not in info_cache:
            info_cache[day] = day_info(pair, day)
        ma20, ma50, dvol = info_cache[day]
        m = Market(coin, pair, price, price, price, vol24, ch24, ma20, ma50, None, dvol)
        k5 = [[ts[i] * 1000, o[i], h[i], lo[i], c[i], 0, 0, q[i], "1"] for i in range(max(0, t - 35), t + 1)]
        pump = pumps.analyze(coin, k5, vol24, now_ms=(ts[t] + BAR) * 1000)
        if kind == "signal" and pump and pump.phase in ("topping", "dumping"):
            continue  # not offered live
        riding = kind == "signal" and pump is not None and pump.phase == "starting" and MODE == "live"
        if riding:
            stop_pct, target_pct, hold, trail = pump.trail_pct, 3 * pump.trail_pct, pumps.RIDE_HOURS * 3600, pump.trail_pct
        else:
            stop_pct = max(movers.MIN_STOP, min(movers.MAX_STOP, 1.5 * (dvol or 0.06)))
            target_pct, hold, trail = 2 * stop_pct, int(movers.HOLD_DAYS * DAY), None
        score = None
        if kind == "signal":
            mv = movers.Move(coin, pair, price, ch15, ch60, surge, high15)
            week_high = max(h[max(0, t - 2016): max(1, t - 11)]) if t > 12 else None
            score = movers.score_move(mv, m, 0, week_high is not None and price > week_high) + (10 if riding else 0)
        entry, stop, target, peak = price, price * (1 - stop_pct), price * (1 + target_pct), price
        exit_price, reason, j = None, None, t
        for j in range(t + 1, len(rows)):
            if ts[j] >= ts[t] + hold:
                exit_price, reason = c[j - 1], "time"
                break
            eff = max(stop, peak * (1 - trail)) if trail else stop
            if lo[j] <= eff:
                exit_price, reason = min(o[j], eff), "stop"
                break
            if h[j] >= target:
                exit_price, reason = target, "target"
                break
            if trail:
                peak = max(peak, h[j])
            # pump topping/dumping exits (risky styles), trend exit for pump rides
            k5 = [[ts[i] * 1000, o[i], h[i], lo[i], c[i], 0, 0, q[i], "1"] for i in range(max(0, j - 35), j + 1)]
            v24 = qsum[j + 1] - qsum[max(0, j - 287)]
            ps = pumps.analyze(coin, k5, v24, now_ms=(ts[j] + BAR) * 1000)
            if ps and ps.phase in ("topping", "dumping"):
                exit_price, reason = c[j], "pump_dump" if ps.phase == "dumping" else "pump_fading"
                break
        if exit_price is None:
            continue  # still open at the end of the data
        net = exit_price / entry - 1 - COST
        rec = {"pair": pair, "ts": ts[t], "style": "pump" if riding else "early", "score": score, "net": net,
               "reason": reason, "hours": (ts[j] - ts[t]) / 3600, "above_ma50": bool(ma50 and price > ma50),
               "surge": surge, "ch24": ch24}
        if kind == "signal":
            trades.append(rec)
            busy_until = ts[j]
        else:
            baseline.append(rec)
    if n % 40 == 0:
        print(f"  {n}/{len(pairs)} pairs, {len(trades)} trades, {time.time() - t0:.0f}s", flush=True)


def summary(xs, label):
    if not xs:
        return f"{label:34} n=0"
    nets = [x["net"] for x in xs]
    m, s = statistics.fmean(nets), statistics.pstdev(nets)
    se = s / math.sqrt(len(nets))
    return (f"{label:34} n={len(nets):5}  win {sum(v > 0 for v in nets) / len(nets):4.0%}  avg {m:+.2%} "
            f"(±{2 * se:.2%})  median {statistics.median(nets):+.2%}  avg hold {statistics.fmean(x['hours'] for x in xs):5.1f}h")


print(f"\nmode {MODE}, cost {COST:.2%} round trip, {len(pairs)} pairs, {time.strftime('%Y-%m-%d', time.gmtime(t_first))} onwards")
print(summary(baseline, "Baseline: random entry, same exits"))
print(summary(trades, "All flags"))
for style in ("early", "pump"):
    print(summary([x for x in trades if x["style"] == style], f"  {style}"))
print("By score:")
for lo_, hi_ in ((0, 50), (50, 70), (70, 101)):
    print(summary([x for x in trades if lo_ <= x["score"] < hi_], f"  score {lo_}-{hi_}"))
print("By trend:")
print(summary([x for x in trades if x["above_ma50"]], "  above 50-day average"))
print(summary([x for x in trades if not x["above_ma50"]], "  below 50-day average"))
print("By exit:")
for r in sorted({x["reason"] for x in trades}):
    print(summary([x for x in trades if x["reason"] == r], f"  {r}"))
print("By month:")
for mth in sorted({time.strftime("%Y-%m", time.gmtime(x["ts"])) for x in trades}):
    print(summary([x for x in trades if time.strftime("%Y-%m", time.gmtime(x["ts"])) == mth], f"  {mth}"))
