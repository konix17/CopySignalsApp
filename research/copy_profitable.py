"""The simplest copy rule: take the traders who made money last month (their own real profit), and next month copy
every long they open, buying when they buy and selling when they sell. Research only.

    .venv/bin/python research/copy_profitable.py [--cost 0.005]

Uses the data of research/copy_recheck.py (run that first: it fills the hourly price cache). Prices are hourly closes,
so a copy fills up to an hour after the trader; research/copy_backtest.py showed that a 0-10 minute delay changes
nothing. "Skill" = the copy's move minus the same coin bought at random times for the same number of hours.
"""
import argparse
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.symbols import normalize_coin  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--cost", type=float, default=0.005)
ap.add_argument("--shorts", action="store_true", help="copy shorts too (futures), a short's result is the price fall")
args = ap.parse_args()
H = 3_600_000
random.seed(11)
db = sqlite3.connect(ROOT / "data" / "research.db")
okx_coins = {r[0] for r in sqlite3.connect(ROOT / "data" / "trading.db").execute("SELECT coin FROM markets")}
close = defaultdict(dict)
for pair, hour, c in db.execute("SELECT pair, hour, close FROM hourly"):
    close[pair][hour] = c
month = lambda ms: time.strftime("%Y-%m", time.gmtime(ms / 1000))  # noqa: E731

# Each trader's own realised profit per month (all their trades, longs and shorts, after their fees), and their longs.
profit = defaultdict(float)  # (pool, trader, month) -> USD
longs = []  # (pool, trader, coin, t0, t1, side) - shorts too with --shorts
cur, open_ = None, {}
for addr, coin, sz, side, t, start_pos, pnl, fee in db.execute(
        "SELECT address, coin, sz, side, time, start_pos, closed_pnl, fee FROM hl_fills ORDER BY address, time"):
    if addr != cur:
        cur, open_ = addr, {}
    profit[("Hyperliquid", addr, month(t))] += pnl - fee
    after = start_pos + (sz if side == "B" else -sz)
    if start_pos <= 0 < after:
        open_[coin] = t
    elif start_pos > 0 >= after and coin in open_:
        sym, _ = normalize_coin(coin)
        if sym in okx_coins:
            longs.append(("Hyperliquid", addr, sym, open_.pop(coin), t, 1))
    if start_pos >= 0 > after:
        open_[("short", coin)] = t
    elif start_pos < 0 <= after and ("short", coin) in open_:
        sym, _ = normalize_coin(coin)
        t0 = open_.pop(("short", coin))
        if sym in okx_coins and args.shorts:
            longs.append(("Hyperliquid", addr, sym, t0, t, -1))
for code, inst, side, t0, t1, pnl in db.execute(
        "SELECT code, inst_id, side, open_time, close_time, pnl FROM okx_lead_positions WHERE inst_id LIKE '%-USDT-SWAP' AND close_time IS NOT NULL"):
    profit[("OKX leads", code, month(t1))] += pnl
    sym = inst.split("-")[0]
    if sym in okx_coins and (side == "long" or args.shorts):
        longs.append(("OKX leads", code, sym, t0, t1, 1 if side == "long" else -1))

span = (min(l[3] for l in longs) // H, max(l[4] for l in longs) // H)
rows = []
for pool, trader, coin, t0, t1, d in longs:
    a, b = close[coin].get(t0 // H), close[coin].get(t1 // H)
    if not (a and b):
        continue
    dur = t1 // H - t0 // H
    plac = [close[coin].get(s + dur, 0) / close[coin][s] - 1 for s in
            (random.randint(span[0], span[1] - dur) for _ in range(30)) if close[coin].get(s) and close[coin].get(s + dur)]
    if len(plac) < 10:
        continue
    rows.append({"pool": pool, "trader": trader, "month": month(t0), "hours": dur, "side": d,
                 "net": d * (b / a - 1) - args.cost, "skill": d * (b / a - 1 - statistics.fmean(plac))})


def per_trader(xs, key):
    per = defaultdict(list)
    for x in xs:
        per[x["trader"]].append(x[key])
    means = [statistics.fmean(v) for v in per.values()]
    boots = sorted(statistics.fmean(random.choices(means, k=len(means))) for _ in range(1000))
    return statistics.fmean(means), boots[25], boots[975]


for side, name in ((1, "longs"), (-1, "shorts")):
    for pool in ("Hyperliquid", "OKX leads"):
        xs = [x for x in rows if x["pool"] == pool and x["side"] == side]
        if len({x["trader"] for x in xs}) >= 5:
            n, nlo, nhi = per_trader(xs, "net")
            sk, slo, shi = per_trader(xs, "skill")
            print(f"All {name:6} {pool:11} {len(xs):6,} copies {len({x['trader'] for x in xs}):4} traders | net {n:+.2%} "
                  f"[{nlo:+.2%}, {nhi:+.2%}] | skill {sk:+.2%} [{slo:+.2%}, {shi:+.2%}]")
print()
print(f"Copy every long of last month's profitable traders, the moment they buy and sell "
      f"(after {args.cost:.2%} costs; skill = vs the same coin at random times; per trader, [95% range])\n")
months = sorted({x["month"] for x in rows})
for pool in ("Hyperliquid", "OKX leads"):
    for prev, nxt in zip(months, months[1:]):
        for label, keep in (("profitable last month", lambda p: p > 0), ("losing last month", lambda p: p <= 0)):
            who = {k[1] for k, p in profit.items() if k[0] == pool and k[2] == prev and keep(p)}
            xs = [x for x in rows if x["pool"] == pool and x["month"] == nxt and x["trader"] in who]
            if len({x["trader"] for x in xs}) < 5:
                continue
            n, nlo, nhi = per_trader(xs, "net")
            s, slo, shi = per_trader(xs, "skill")
            short = [x["net"] for x in xs if x["hours"] < 1]
            print(f"  {pool:11} {label:22} {prev} -> {nxt}: {len(xs):6,} copies {len({x['trader'] for x in xs}):4} traders"
                  f" | net {n:+.2%} [{nlo:+.2%}, {nhi:+.2%}] | skill {s:+.2%} [{slo:+.2%}, {shi:+.2%}]"
                  f" | {len(short) / len(xs):.0%} held < 1 h")
