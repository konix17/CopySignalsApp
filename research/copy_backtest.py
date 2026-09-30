"""Would copying Hyperliquid traders' longs on spot have made money, and how much does delay cost? Research only.

    .venv/bin/python research/copy_backtest.py [--cost 0.005]

Input: fills from research/hl_fetch_fills.py (a random sample of active traders, plus the ones the app follows).
Each trader's long positions are rebuilt from their fills (open -> fully closed). Traders are ranked on the first
half of the history only (days 90-45 ago); their long trades in the second half (last 45 days) are then copied:
bought on spot at their entry time plus a delay, sold at their exit time plus the same delay, paying `cost` round
trip (0.2% fee each way plus spread and slippage). Prices: Binance 1-minute candles (close of the minute the copy
happens in). "Their result" is the trader's own entry/exit price move, unlevered and before fees.
"""
import argparse
import asyncio
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.sources.hyperliquid import normalize_coin  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--cost", type=float, default=0.005)
ap.add_argument("--max-trades", type=int, default=6000, help="cap on copied trades priced (2 price requests each)")
args = ap.parse_args()
DELAYS = (0, 1, 5, 10)  # minutes
db = sqlite3.connect(ROOT / "data" / "research.db", timeout=120)
db.execute("CREATE TABLE IF NOT EXISTS binance_1m (pair TEXT, minute INTEGER, close REAL, PRIMARY KEY (pair, minute))")
app = sqlite3.connect(ROOT / "data" / "trading.db")
okx_coins = {r[0] for r in app.execute("SELECT coin FROM markets")}

now_ms = db.execute("SELECT MAX(time) FROM hl_fills").fetchone()[0]
start_ms = db.execute("SELECT MIN(time) FROM hl_fills").fetchone()[0]
split_ms = (start_ms + now_ms) // 2
groups = dict(db.execute("SELECT address, grp FROM hl_traders"))


# --- rebuild long positions ---------------------------------------------------------------------------
def episodes(fills):
    """Long positions per coin: first opening fill -> the fill that takes the position to zero or short."""
    out, open_ = [], {}
    for coin, px, sz, side, t, start_pos, pnl, fee in fills:
        after = start_pos + (sz if side == "B" else -sz)
        if start_pos <= 0 < after:
            open_[coin] = (t, px)
        elif start_pos > 0 >= after and coin in open_:
            t0, p0 = open_.pop(coin)
            out.append((coin, t0, p0, t, px))
    return out


by_trader = defaultdict(list)
pnl_a = defaultdict(float)
n_a = defaultdict(int)
for addr, coin, px, sz, side, t, start_pos, pnl, fee in db.execute(
        "SELECT address, coin, px, sz, side, time, start_pos, closed_pnl, fee FROM hl_fills ORDER BY address, time"):
    by_trader[addr].append((coin, px, sz, side, t, start_pos, pnl, fee))
    if t < split_ms:
        pnl_a[addr] += pnl - fee
        n_a[addr] += pnl != 0

# Rank on the first half only: profitable, at least 10 closing fills, top third by profit among those.
ranked = sorted((a for a in by_trader if n_a[a] >= 10 and pnl_a[a] > 0), key=lambda a: -pnl_a[a])
top = set(ranked[: max(1, len(ranked) // 3)])
sample = {a for a, g in groups.items() if g == "sample"}
followed = {a for a, g in groups.items() if g == "followed"}
print(f"{len(by_trader)} traders with fills ({len(sample)} random sample, {len(followed)} followed by the app); "
      f"{len(ranked)} profitable in the first half, top third = {len(top)}")

trades = []  # (addr, coin, pair, t0, p0, t1, p1)
for addr, fills in by_trader.items():
    for coin, t0, p0, t1, p1 in episodes(fills):
        if t0 < split_ms or t1 - t0 < 60_000:
            continue  # only second-half trades, held at least a minute
        sym, mult = normalize_coin(coin)
        if sym is None or sym not in okx_coins:
            continue  # not buyable on OKX spot
        trades.append((addr, coin, f"{sym}USDT", t0, p0, t1, p1))
print(f"{len(trades)} copyable long trades in the second half ({time.strftime('%Y-%m-%d', time.gmtime(split_ms / 1000))} on)")


# --- prices ---------------------------------------------------------------------------------------------
async def fetch_prices(needed: set[tuple[str, int]]):
    have = {(p, m) for p, m in db.execute("SELECT pair, minute FROM binance_1m")}
    todo = sorted({(p, m) for p, m in needed if (p, m) not in have})
    windows = sorted({(p, m - m % 12) for p, m in todo})  # one request covers 12 minutes
    sem = asyncio.Semaphore(8)
    bad: set[str] = set()

    async def one(client, pair, m0):
        if pair in bad:
            return
        async with sem:
            await asyncio.sleep(0.05)
            r = await client.get("https://api.binance.com/api/v3/klines",
                                 params={"symbol": pair, "interval": "1m", "startTime": m0 * 60_000, "limit": 12})
            if r.status_code == 400:
                bad.add(pair)  # not listed on Binance
                return
            r.raise_for_status()
            db.executemany("INSERT OR REPLACE INTO binance_1m VALUES (?, ?, ?)",
                           [(pair, int(k[0]) // 60_000, float(k[4])) for k in r.json()])

    async with httpx.AsyncClient(timeout=20) as client:
        for i in range(0, len(windows), 400):
            await asyncio.gather(*(one(client, p, m) for p, m in windows[i:i + 400]), return_exceptions=True)
            db.commit()
            print(f"  prices {min(i + 400, len(windows))}/{len(windows)}", flush=True)


def minute(ms, delay):
    return ms // 60_000 + delay


trades = trades[: args.max_trades]
needed = {(pair, minute(t, d)) for _, _, pair, t0, _, t1, _ in trades for t in (t0, t1) for d in DELAYS}
asyncio.run(fetch_prices(needed))
close = {(p, m): c for p, m, c in db.execute("SELECT pair, minute, close FROM binance_1m")}

# --- results ----------------------------------------------------------------------------------------------
rows = []
for addr, coin, pair, t0, p0, t1, p1 in trades:
    rec = {"addr": addr, "hours": (t1 - t0) / 3.6e6, "theirs": p1 / p0 - 1}
    for d in DELAYS:
        a, b = close.get((pair, minute(t0, d))), close.get((pair, minute(t1, d)))
        rec[d] = b / a - 1 - args.cost if a and b else None
    if all(rec[d] is not None for d in DELAYS):
        rows.append(rec)


def line(label, xs):
    if len(xs) < 20:
        return f"{label:38} n={len(xs)} (too few)"
    parts = [f"theirs {statistics.fmean(x['theirs'] for x in xs):+.2%}"]
    for d in DELAYS:
        v = [x[d] for x in xs]
        se = statistics.pstdev(v) / math.sqrt(len(v))
        parts.append(f"{d}m {statistics.fmean(v):+.2%}±{2 * se:.2%}")
    return f"{label:38} n={len(xs):5}  " + "  ".join(parts)


print(f"\nAverage per copied trade, after {args.cost:.2%} round-trip spot costs (theirs: before fees, unlevered)")
print(line("All traders", rows))
print(line("Random sample", [x for x in rows if x["addr"] in sample]))
print(line("Top third on the first half", [x for x in rows if x["addr"] in top]))
print(line("Rest (not top)", [x for x in rows if x["addr"] not in top]))
print(line("Followed by the app now (hindsight)", [x for x in rows if x["addr"] in followed]))
print("\nTop third, by how long they held:")
for lo, hi, name in ((0, 1, "< 1 hour"), (1, 24, "1-24 hours"), (24, 1e9, "over a day")):
    print(line(f"  {name}", [x for x in rows if x["addr"] in top and lo <= x["hours"] < hi]))
# Waiting filter (no hindsight): copy a position only once it's still open `age` hours after the trader opened it,
# buy then, and sell 1 minute after they close. Positions closed sooner are never copied.
ages = (1, 4, 12, 24)
extra = {(pair, minute(t0 + int(a * 3.6e6), 0)) for _, _, pair, t0, _, t1, _ in trades for a in ages if t1 - t0 > a * 3.6e6}
# The same windows for BTC: is the copy better than simply holding BTC over the same hours?
extra |= {("BTCUSDT", minute(t0 + int(a * 3.6e6), 0)) for _, _, pair, t0, _, t1, _ in trades for a in ages if t1 - t0 > a * 3.6e6}
extra |= {("BTCUSDT", minute(t1, 1)) for _, _, pair, t0, _, t1, _ in trades}
asyncio.run(fetch_prices(extra))
close.update({(p, m): c for p, m, c in db.execute("SELECT pair, minute, close FROM binance_1m")})
print("\nCopy only positions still open after N hours (enter at N hours, exit 1 min after the trader):")
for age in ages:
    for label, who in (("all traders", None), ("top third", top), ("followed now (hindsight)", followed)):
        xs, ex = [], []
        for addr, coin, pair, t0, p0, t1, p1 in trades:
            if (who is not None and addr not in who) or t1 - t0 <= age * 3.6e6:
                continue
            m0, m1 = minute(t0 + int(age * 3.6e6), 0), minute(t1, 1)
            a, b = close.get((pair, m0)), close.get((pair, m1))
            ba, bb = close.get(("BTCUSDT", m0)), close.get(("BTCUSDT", m1))
            if a and b:
                xs.append(b / a - 1 - args.cost)
                if ba and bb:
                    ex.append((b / a - 1) - (bb / ba - 1))  # before costs, vs holding BTC over the same minutes
        if len(xs) >= 20:
            m, se = statistics.fmean(xs), statistics.pstdev(xs) / math.sqrt(len(xs))
            em, ese = statistics.fmean(ex), statistics.pstdev(ex) / math.sqrt(len(ex))
            print(f"  after {age:>2}h, {label:26} n={len(xs):5}  avg {m:+.2%} ±{2 * se:.2%}  won {sum(x > 0 for x in xs) / len(xs):.0%}"
                  f"  | vs BTC same hours (before costs) {em:+.2%} ±{2 * ese:.2%}")
        else:
            print(f"  after {age:>2}h, {label:26} n={len(xs)} (too few)")

print("\nAll traders, by how long they held:")
for lo, hi, name in ((0, 1, "< 1 hour"), (1, 24, "1-24 hours"), (24, 1e9, "over a day")):
    print(line(f"  {name}", [x for x in rows if lo <= x["hours"] < hi]))
