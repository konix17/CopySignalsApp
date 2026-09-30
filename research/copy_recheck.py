"""Stricter test of copying traders' longs on spot: is there skill, or just a rising market? Research only.

    .venv/bin/python research/copy_recheck.py [--cost 0.005] [--age 12]

Two independent pools of traders over the same ~90 days:
- Hyperliquid: fills of 259 random active traders plus the ones the app followed (research/hl_fetch_fills.py)
- OKX copy-trading lead traders: their closed futures positions (research/okx_fetch_leads.py)

Each long the trader kept open for `--age` hours is "copied": bought at that point, sold when they closed it, paying
`--cost` round trip, on hourly OKX-listed spot prices (Binance 1h closes, OKX when Binance hasn't got the coin).
Checks the first test (copy_backtest.py) didn't make:
- luck: the same coin bought at random times for the same number of hours (the placebo); skill = copy - placebo
- spread: results per trader (each trader counts once) and per month, not per trade, since trades on the same days
  move together; confidence intervals by resampling traders
- selection: do traders who did well in one month do better the next?
- portfolio: the live rule (best-scored trader per coin, 5 copies) vs spreading over many traders
"""
import argparse
import asyncio
import math
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.symbols import normalize_coin  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--cost", type=float, default=0.005)
ap.add_argument("--age", type=float, default=12)
ap.add_argument("--placebos", type=int, default=30)
args = ap.parse_args()
H = 3_600_000
db = sqlite3.connect(ROOT / "data" / "research.db", timeout=120)
db.execute("CREATE TABLE IF NOT EXISTS hourly (pair TEXT, hour INTEGER, close REAL, PRIMARY KEY (pair, hour))")
okx_coins = {r[0] for r in sqlite3.connect(ROOT / "data" / "trading.db").execute("SELECT coin FROM markets")}
random.seed(7)


@dataclass
class Trade:
    pool: str
    trader: str
    coin: str
    t0: int  # trader opened (ms)
    t1: int  # trader closed (ms)
    p0: float = 0.0  # trader's entry price (spot terms)


# --- the traders' longs ------------------------------------------------------------------------------------
def hl_trades() -> list[Trade]:
    out, open_ = [], {}
    cur = None
    for addr, coin, p, sz, side, t, start_pos in db.execute(
            "SELECT address, coin, px, sz, side, time, start_pos FROM hl_fills ORDER BY address, time"):
        if addr != cur:
            cur, open_ = addr, {}
        after = start_pos + (sz if side == "B" else -sz)
        if start_pos <= 0 < after:
            open_[coin] = (t, p)
        elif start_pos > 0 >= after and coin in open_:
            sym, mult = normalize_coin(coin)
            t0, p0 = open_.pop(coin)
            if sym in okx_coins:
                out.append(Trade("Hyperliquid", addr, sym, t0, t, p0 / (mult or 1)))
    return out


def okx_trades() -> list[Trade]:
    out = []
    for code, inst, t0, t1, p0 in db.execute("SELECT code, inst_id, open_time, close_time, open_px FROM okx_lead_positions "
                                             "WHERE side = 'long' AND inst_id LIKE '%-USDT-SWAP' AND close_time IS NOT NULL"):
        sym = inst.split("-")[0]
        if sym in okx_coins:
            out.append(Trade("OKX leads", code, sym, t0, t1, p0))
    return out


trades = hl_trades()
try:
    trades += okx_trades()
except sqlite3.OperationalError:
    pass
start = min(t.t0 for t in trades)
end = max(t.t1 for t in trades)
start = max(start, end - 92 * 86400_000)
trades = [t for t in trades if t.t0 >= start]
print(f"{len(trades):,} closed longs in OKX-listed coins, {len({(t.pool, t.trader) for t in trades})} traders, "
      f"{time.strftime('%Y-%m-%d', time.gmtime(start / 1000))} to {time.strftime('%Y-%m-%d', time.gmtime(end / 1000))}")


# --- hourly prices -------------------------------------------------------------------------------------------
async def fetch_hourly(coins: set[str]) -> None:
    have = {r[0] for r in db.execute("SELECT pair FROM hourly GROUP BY pair HAVING MAX(hour) >= ?", (end // H - 2,))}
    todo = sorted(c for c in coins if c not in have)
    sem = asyncio.Semaphore(6)

    async def binance(client, coin) -> list[tuple]:
        rows, t = [], start - 2 * 86400_000
        while t < end:
            r = await client.get("https://api.binance.com/api/v3/klines",
                                 params={"symbol": f"{coin}USDT", "interval": "1h", "startTime": t, "limit": 1000})
            if r.status_code == 400:
                return []
            r.raise_for_status()
            k = r.json()
            if not k:
                break
            rows += [(coin, int(x[0]) // H, float(x[4])) for x in k]
            t = int(k[-1][0]) + H
        return rows

    async def okx(client, coin) -> list[tuple]:
        rows, after = [], None
        while True:
            p = {"instId": f"{coin}-USDT", "bar": "1H", "limit": "100"}
            if after:
                p["after"] = after
            r = await client.get("https://www.okx.com/api/v5/market/history-candles", params=p)
            k = r.json().get("data", [])
            if not k:
                break
            rows += [(coin, int(x[0]) // H, float(x[4])) for x in k]
            after = k[-1][0]
            if int(after) < start - 2 * 86400_000:
                break
            await asyncio.sleep(0.12)
        return rows

    async def one(client, coin):
        async with sem:
            rows = await binance(client, coin) or await okx(client, coin)
            db.executemany("INSERT OR REPLACE INTO hourly VALUES (?, ?, ?)", rows)

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(todo), 30):
            await asyncio.gather(*(one(client, c) for c in todo[i:i + 30]), return_exceptions=True)
            db.commit()
            print(f"  hourly prices {min(i + 30, len(todo))}/{len(todo)}", flush=True)


asyncio.run(fetch_hourly({t.coin for t in trades} | {"BTC"}))
close: dict[str, dict[int, float]] = defaultdict(dict)
for pair, hour, c in db.execute("SELECT pair, hour, close FROM hourly WHERE hour >= ?", (start // H - 48,)):
    close[pair][hour] = c


def px(coin, ms):
    """Close of the hour the moment falls in (so a fill comes 0-60 minutes after the event)."""
    return close[coin].get(ms // H)


# --- copy each qualifying long ----------------------------------------------------------------------------
AGE = int(args.age * H)
open_by_coin = defaultdict(list)  # every long (any length), to count how many other traders hold the coin
for t in trades:
    open_by_coin[(t.pool, t.coin)].append(t)
hours_span = (start // H, end // H)
rows = []
for t in trades:
    if t.t1 - t.t0 <= AGE:
        continue
    a, b = px(t.coin, t.t0 + AGE), px(t.coin, t.t1)
    ba, bb = px("BTC", t.t0 + AGE), px("BTC", t.t1)
    if not (a and b and ba and bb):
        continue
    dur = t.t1 // H - (t.t0 + AGE) // H
    plac = []
    for _ in range(args.placebos):  # the same coin, the same number of hours, a random start
        s = random.randint(hours_span[0], hours_span[1] - dur)
        pa, pb = close[t.coin].get(s), close[t.coin].get(s + dur)
        if pa and pb:
            plac.append(pb / pa - 1)
    if len(plac) < args.placebos // 2:
        continue
    gross = b / a - 1
    others = sum(1 for o in open_by_coin[(t.pool, t.coin)] if o.trader != t.trader and o.t0 <= t.t0 + AGE < o.t1)
    rows.append({"in_profit": bool(t.p0) and a > t.p0, "others": others, "pool": t.pool, "trader": t.trader, "coin": t.coin, "entry": t.t0 + AGE, "exit": t.t1, "hours": dur,
                 "net": gross - args.cost, "vs_btc": gross - (bb / ba - 1), "skill": gross - statistics.fmean(plac),
                 "month": time.strftime("%Y-%m", time.gmtime((t.t0 + AGE) / 1000))})


def boot_by_trader(xs, key, n=1000):
    """Mean of per-trader means, and a 95% range from resampling traders."""
    per = defaultdict(list)
    for x in xs:
        per[x["trader"]].append(x[key])
    means = [statistics.fmean(v) for v in per.values()]
    if len(means) < 5:
        return statistics.fmean(means) if means else float("nan"), float("nan"), float("nan")
    boots = sorted(statistics.fmean(random.choices(means, k=len(means))) for _ in range(n))
    return statistics.fmean(means), boots[int(0.025 * n)], boots[int(0.975 * n)]


def report(label, xs):
    if len(xs) < 30:
        print(f"  {label:34} n={len(xs)} (too few)")
        return
    ntr = len({x["trader"] for x in xs})
    m_net = statistics.fmean(x["net"] for x in xs)
    m_sk = statistics.fmean(x["skill"] for x in xs)
    t_sk, lo, hi = boot_by_trader(xs, "skill")
    t_net, nlo, nhi = boot_by_trader(xs, "net")
    print(f"  {label:34} {len(xs):6,} trades {ntr:4} traders | per trade: net {m_net:+.2%}, skill {m_sk:+.2%} | "
          f"per trader: net {t_net:+.2%} [{nlo:+.2%}, {nhi:+.2%}], skill {t_sk:+.2%} [{lo:+.2%}, {hi:+.2%}]")


print(f"\nCopies of longs still open after {args.age:g} h (net = after {args.cost:.2%} costs; skill = before costs, minus the"
      f" same coin at random times; [95% range])")
for pool in ("Hyperliquid", "OKX leads"):
    xs = [x for x in rows if x["pool"] == pool]
    print(f"\n{pool}")
    report("all", xs)
    for m in sorted({x["month"] for x in xs}):
        report(f"  entered {m}", [x for x in xs if x["month"] == m])
    for lo_h, hi_h, name in ((0, 24, "held < 1 day more"), (24, 24 * 7, "1-7 days more"), (24 * 7, 1e9, "over a week more")):
        report(f"  {name}", [x for x in xs if lo_h <= x["hours"] < hi_h])
    # how much of the profit comes from the best few traders
    per = defaultdict(float)
    for x in xs:
        per[x["trader"]] += x["net"]
    tot = sum(per.values())
    top5 = sum(sorted(per.values(), reverse=True)[:5])
    pos = sum(v > 0 for v in per.values())
    if per:
        print(f"    {pos} of {len(per)} traders net positive; the best 5 traders make {top5:+.1f} of the {tot:+.1f} "
              f"summed net (in trade units)")

# --- is there skill in some group of trades? ---------------------------------------------------------------
groups_ = dict(db.execute("SELECT address, grp FROM hl_traders"))
acct = {a: v or 0 for a, v in db.execute("SELECT address, account_value FROM hl_traders")}
print("\nSkill by kind of trade (before costs, vs the same coin at random times; per trader, [95% range]):")
filters = [
    ("trader in profit at the copy", lambda x: x["in_profit"]),
    ("trader in a loss at the copy", lambda x: not x["in_profit"]),
    ("2+ other traders also long", lambda x: x["others"] >= 2),
    ("no other trader long", lambda x: x["others"] == 0),
    ("HL: traders the app followed", lambda x: groups_.get(x["trader"]) == "followed"),
    ("HL: random sample", lambda x: groups_.get(x["trader"]) == "sample"),
    ("HL: account over $1M", lambda x: acct.get(x["trader"], 0) >= 1e6),
    ("HL: account under $100k", lambda x: 0 < acct.get(x["trader"], 0) < 1e5),
    ("BTC or ETH", lambda x: x["coin"] in ("BTC", "ETH")),
    ("other coins", lambda x: x["coin"] not in ("BTC", "ETH")),
]
for pool in ("Hyperliquid", "OKX leads"):
    for label, f in filters:
        xs = [x for x in rows if x["pool"] == pool and f(x)]
        if len(xs) >= 30 and len({x["trader"] for x in xs}) >= 5:
            v, lo, hi = boot_by_trader(xs, "skill")
            n, nlo, nhi = boot_by_trader(xs, "net")
            print(f"  {pool:11} {label:32} {len(xs):6,} trades {len({x['trader'] for x in xs}):4} traders: "
                  f"skill {v:+.2%} [{lo:+.2%}, {hi:+.2%}]  net {n:+.2%}")

# --- selection: last month's winners, next month -------------------------------------------------------------
print("\nDoes picking last month's best copy traders help next month? (net per trade, per-trader average)")
months = sorted({x["month"] for x in rows})
for pool in ("Hyperliquid", "OKX leads"):
    for m_prev, m_next in zip(months, months[1:]):
        prev = defaultdict(list)
        for x in rows:
            if x["pool"] == pool and x["month"] == m_prev:
                prev[x["trader"]].append(x["net"])
        good = {k for k, v in prev.items() if len(v) >= 3 and statistics.fmean(v) > 0}
        bad = {k for k, v in prev.items() if len(v) >= 3 and statistics.fmean(v) <= 0}
        nxt = [x for x in rows if x["pool"] == pool and x["month"] == m_next]
        for label, who in (("winners", good), ("losers", bad)):
            xs = [x for x in nxt if x["trader"] in who]
            if len(xs) >= 10:
                v, lo, hi = boot_by_trader(xs, "net")
                print(f"  {pool:11} {m_prev} {label:7} -> {m_next}: {len(xs):5} trades {len({x['trader'] for x in xs}):3}"
                      f" traders, net {v:+.2%} [{lo:+.2%}, {hi:+.2%}]")


# --- portfolio: the live rule vs spreading -----------------------------------------------------------------
def simulate(pool, pick, slots, per_trader_cap):
    """Hour by hour: equal-size slots (1/slots of the account each), a copy opens when `pick` chooses a coin not held,
    closes when that trader closes. Returns yearly-rate return, worst drop, and the most copies from one trader."""
    xs = sorted((x for x in rows if x["pool"] == pool), key=lambda x: x["entry"])
    by_entry = defaultdict(list)
    for x in xs:
        by_entry[x["entry"] // H].append(x)
    opened, equity, peak, worst, held, value = [], 1.0, 1.0, 0.0, [], []
    max_one = 0
    for h in range(start // H, end // H):
        for pos in [p for p in held if p["exit"] // H <= h]:
            held.remove(pos)
            equity += pos["size"] * pos["net"]
        cands = [x for x in by_entry.get(h, []) if x["coin"] not in {p["coin"] for p in held}]
        for x in pick(cands, held):
            if len(held) >= slots:
                break
            if sum(p["trader"] == x["trader"] for p in held) >= per_trader_cap:
                continue
            if x["coin"] in {p["coin"] for p in held}:
                continue
            held.append({**x, "size": equity / slots})
            opened.append(x)
        max_one = max([max_one] + [sum(p["trader"] == q["trader"] for p in held) for q in held])
        # mark to market roughly: open copies at their final result spread over their life (enough for drawdown shape)
        mtm = equity + sum(p["size"] * p["net"] * min(1, (h - p["entry"] // H) / max(1, p["hours"])) for p in held)
        peak = max(peak, mtm)
        worst = min(worst, mtm / peak - 1)
        value.append(mtm)
    days = (end - start) / 86400_000
    btc = px("BTC", end - H) / px("BTC", start + H) - 1
    return value[-1] - 1, worst, len(opened), max_one, btc, days


score = defaultdict(float)  # a trader's score: their copies' net results before each moment (no hindsight)
print("\nPortfolio over the whole period (copies share the account in equal slots):")
for pool in ("Hyperliquid", "OKX leads"):
    if not any(x["pool"] == pool for x in rows):
        continue
    past = defaultdict(list)

    def past_mean(x):
        v = [r["net"] for r in past[x["trader"]] if r["exit"] < x["entry"]]
        return statistics.fmean(v) if len(v) >= 3 else -1

    for x in rows:
        if x["pool"] == pool:
            past[x["trader"]].append(x)
    rules = [
        ("live rule: best trader, 5 slots, no cap", lambda c, h: sorted(c, key=lambda x: -past_mean(x)), 5, 99),
        ("5 slots, max 1 per trader", lambda c, h: sorted(c, key=lambda x: -past_mean(x)), 5, 1),
        ("20 slots, max 2 per trader", lambda c, h: sorted(c, key=lambda x: -past_mean(x)), 20, 2),
        ("20 slots, random order, max 2", lambda c, h: random.sample(c, len(c)), 20, 2),
    ]
    for label, pick, slots, cap in rules:
        ret, dd, n, one, btc, days = simulate(pool, pick, slots, cap)
        print(f"  {pool:11} {label:40} {ret:+7.1%} in {days:.0f} days, worst drop {dd:+.1%}, {n} copies,"
              f" up to {one} open from one trader | BTC {btc:+.1%}")
