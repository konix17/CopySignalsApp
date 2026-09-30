"""Does the crowd of recently profitable traders know which coins will rise? Research only.

    <python with numpy> research/trader_positioning.py

Not copying one trader: every day, each trader's net position in each coin is rebuilt (Hyperliquid from their fills,
OKX lead traders from their open and closed positions), and weighted by whether the trader made money over the
previous 30 days (known at the time, no hindsight). Per coin and day:
- smart_net: net long (USD) of recently profitable traders, relative to the coin's daily volume
- smart_flow: how much that changed today (profitable traders buying or selling)
- smart_long_share: of the profitable traders holding the coin, the share that's long
- crowd_net / crowd_flow: the same for every trader (the crowd; often a contrarian sign)
Each is tested on the next day's and next 3 days' move across coins (rank correlation), first on the earlier half
of the usable days, then on the later half. Only ~90 days of trader data exist, so treat any result as a lead to
forward-test, not proof.
"""
import math
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.symbols import normalize_coin  # noqa: E402

DAY_MS = 86_400_000
np.seterr(all="ignore")
rdb = sqlite3.connect(ROOT / "data" / "research.db")
mdb = sqlite3.connect(ROOT / "data" / "market.db")
t_first = rdb.execute("SELECT MIN(time) FROM hl_fills").fetchone()[0] // DAY_MS
t_last = rdb.execute("SELECT MAX(time) FROM hl_fills").fetchone()[0] // DAY_MS
days = list(range(t_first, t_last + 1))  # day numbers (ms // DAY_MS); a snapshot is at the end of each day
T = len(days)
close, vol = defaultdict(dict), defaultdict(dict)
for coin, day, c, v in mdb.execute("SELECT coin, day, close, quote_vol FROM spot_1d WHERE day >= ?",
                                   ((t_first - 40) * 86400,)):
    close[coin][day // 86400] = c
    vol[coin][day // 86400] = v
coins = sorted(c for c in close if len(close[c]) > 60)
cidx = {c: i for i, c in enumerate(coins)}
N = len(coins)
print(f"{T} days of trader data ({time.strftime('%Y-%m-%d', time.gmtime(t_first * 86400))} .. "
      f"{time.strftime('%Y-%m-%d', time.gmtime(t_last * 86400))}), {N} coins with prices")


def px(coin, d):
    return close[coin].get(d)


vol30 = np.full((T, N), np.nan)
for c, j in cidx.items():
    for i, d in enumerate(days):
        v = [vol[c].get(d - k) for k in range(30)]
        v = [x for x in v if x]
        if v:
            vol30[i, j] = sum(v) / len(v)

# --- each trader's position per coin at each day's end, and their realised profit by day -----------------------
pools = {}
# Hyperliquid
hl_pos = defaultdict(lambda: np.zeros((T, N)))  # trader -> signed USD per day and coin
hl_pnl = defaultdict(lambda: np.zeros(T))
cur, last = None, {}
rows = rdb.execute("SELECT address, coin, px, sz, side, time, start_pos, closed_pnl, fee FROM hl_fills ORDER BY address, time")


def flush(addr, last_):
    """Fill daily snapshots for one trader: position after the last fill before each day's end."""
    arr = hl_pos[addr]
    for (sym, mult), events in last_.items():
        j = cidx.get(sym)
        if j is None:
            continue
        k, pos = 0, events[0][1]  # before the first fill: its starting position
        for i, d in enumerate(days):
            while k < len(events) and events[k][0] <= d:
                pos = events[k][2]
                k += 1
            p = px(sym, d)
            if p and pos:
                arr[i, j] = pos * p * mult


for addr, coin, fpx, sz, side, t, start_pos, pnl, fee in rows:
    if addr != cur:
        if cur is not None:
            flush(cur, last)
        cur, last = addr, defaultdict(list)
    sym, mult = normalize_coin(coin)
    if sym:
        last[(sym, mult or 1)].append((t // DAY_MS, start_pos, start_pos + (sz if side == "B" else -sz)))
    i = t // DAY_MS - t_first
    if 0 <= i < T:
        hl_pnl[addr][i] += pnl - fee
if cur is not None:
    flush(cur, last)
pools["HL"] = (hl_pos, hl_pnl)

# OKX lead traders: margin x leverage, long or short, while the position was open
okx_pos = defaultdict(lambda: np.zeros((T, N)))
okx_pnl = defaultdict(lambda: np.zeros(T))
for code, inst, side, lever, margin, t0, t1, pnl in rdb.execute(
        "SELECT code, inst_id, side, lever, margin, open_time, close_time, pnl FROM okx_lead_positions "
        "WHERE inst_id LIKE '%-USDT-SWAP'"):
    j = cidx.get(inst.split("-")[0])
    usd = (margin or 0) * (lever or 1) * (1 if side == "long" else -1)
    a = max(0, t0 // DAY_MS - t_first)
    b = T if t1 is None else min(T, t1 // DAY_MS - t_first)
    if j is not None and a < b:
        okx_pos[code][a:b, j] += usd
    if t1 is not None and pnl is not None and 0 <= t1 // DAY_MS - t_first < T:
        okx_pnl[code][t1 // DAY_MS - t_first] += pnl
pools["OKX"] = (okx_pos, okx_pnl)
print(f"traders: Hyperliquid {len(hl_pos)}, OKX leads {len(okx_pos)}")

# --- signals ---------------------------------------------------------------------------------------------------
C = np.full((T + 3, N), np.nan)
for c, j in cidx.items():
    for i in range(T + 3):
        C[i, j] = px(c, t_first + i) or np.nan
fwd1 = C[1:T + 1] / C[:T] - 1
fwd3 = C[3:T + 3] / C[:T] - 1
S = {}
for pool, (pos, pnl) in pools.items():
    smart_net, crowd_net = np.zeros((T, N)), np.zeros((T, N))
    smart_long, smart_any = np.zeros((T, N)), np.zeros((T, N))
    known = np.zeros(T, bool)
    for trader, arr in pos.items():
        cum = np.cumsum(pnl[trader])
        p30 = cum - np.concatenate([np.zeros(30), cum[:-30]])  # profit over the last 30 days, known at day end
        smart = (p30 > 0)[:, None]
        smart_net += arr * smart
        crowd_net += arr
        smart_long += (arr > 0) * smart
        smart_any += (arr != 0) * smart
    known[30:] = True
    S[f"{pool} smart_net"] = smart_net / vol30
    S[f"{pool} smart_flow"] = np.vstack([np.full((1, N), np.nan), np.diff(smart_net, axis=0)]) / vol30
    S[f"{pool} smart_long_share"] = np.where(smart_any >= 3, smart_long / np.maximum(smart_any, 1), np.nan)
    S[f"{pool} crowd_net"] = crowd_net / vol30
    S[f"{pool} crowd_flow"] = np.vstack([np.full((1, N), np.nan), np.diff(crowd_net, axis=0)]) / vol30
    for k in list(S):
        if k.startswith(pool):
            S[k][~known] = np.nan
            if "share" not in k:
                S[k][(smart_any if "smart" in k else np.abs(crowd_net)) == 0] = np.nan  # no one in the coin


def ic(sig, target, i):
    ok = ~np.isnan(sig[i]) & ~np.isnan(target[i])
    if ok.sum() < 15:
        return np.nan
    return float(np.corrcoef(sig[i, ok].argsort().argsort(), target[i, ok].argsort().argsort())[0, 1])


usable = [i for i in range(T) if i >= 30]
half = usable[len(usable) // 2]
print(f"\nRank correlation across coins with the next move (+ = coins they favour go on to do better)")
print(f"earlier half: {time.strftime('%m-%d', time.gmtime((t_first + usable[0]) * 86400))} .. "
      f"{time.strftime('%m-%d', time.gmtime((t_first + half) * 86400))}, later half: after that\n")
for name, sig in S.items():
    for tname, target, h in (("next day", fwd1, 1), ("next 3 days", fwd3, 3)):
        a = [ic(sig, target, i) for i in usable if i < half - h]
        b = [ic(sig, target, i) for i in usable if i >= half]
        a, b = np.array([x for x in a if not np.isnan(x)]), np.array([x for x in b if not np.isnan(x)])
        if len(a) < 8 or len(b) < 8:
            continue
        # overlapping 3-day targets: t-stat on non-overlapping days
        ta = a.mean() / (a.std(ddof=1) / math.sqrt(len(a) / h))
        tb = b.mean() / (b.std(ddof=1) / math.sqrt(len(b) / h))
        coins_per_day = np.nanmean([(~np.isnan(sig[i])).sum() for i in usable])
        print(f"  {name:22} {tname:11} earlier {a.mean():+.3f} (t {ta:+4.1f})  later {b.mean():+.3f} (t {tb:+4.1f})"
              f"  ~{coins_per_day:.0f} coins/day")
