"""Stress tests for the long/short result of research/daily_patterns.py. Research only.

    <python with numpy> research/longshort_check.py

Uses the model predictions saved by daily_patterns.py (data/daily_patterns_cache.npz), made walk-forward (each
quarter's model only saw earlier data). Checks:
- random scores through the same engine (must lose about the fees, or the engine is wrong)
- trading 1 or 2 days late (a real pattern survives some delay; a price quirk at the daily close doesn't)
- higher costs; funding charged for the day the position is actually held
- every year separately; the long side and the short side separately; how much it trades
"""
import math
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
z = np.load(ROOT / "data" / "daily_patterns_cache.npz")
days, univ, C, FUND, vol30 = z["days"], z["univ"], z["close"], z["fund_day"], z["vol30"]
T, N = C.shape
np.seterr(all="ignore")
fwd1 = np.full((T, N), np.nan)
fwd1[:-1] = C[1:] / C[:-1] - 1
fwd1 = np.where(np.isnan(fwd1) & univ, 0.0, fwd1)
fund_next = np.full((T, N), np.nan)
fund_next[:-1] = FUND[1:]  # funding paid while the position is held (the day after the signal)
DEV = (days >= 1_530_403_200) & (days < 1_719_792_000)
HOLD = days >= 1_719_792_000
btc = list(z["coins"]).index("BTC")


def run(score, top=25, frac=0.2, keep=1.5, cost=0.001, lag=0, seed=None):
    """Daily long/short, half the account each side. Returns net, long-leg and short-leg daily returns, turnover."""
    rng = np.random.default_rng(seed) if seed is not None else None
    w = np.zeros(N)
    net, lg, sh, turn = (np.full(T, np.nan) for _ in range(4))
    for i in range(lag, T - 1):
        s = score[i - lag] if rng is None else rng.random(N)
        ok = univ[i] & ~np.isnan(s) & ~np.isnan(FUND[i]) & ~np.isnan(fwd1[i])
        idx = np.where(ok)[0]
        idx = idx[np.argsort(-vol30[i, idx])][:top]
        new = np.zeros(N)
        if len(idx) >= 10:
            k = max(2, int(len(idx) * frac))
            order = idx[np.argsort(-s[idx])]
            lk, sk = set(order[: int(k * keep)]), set(order[-int(k * keep):])
            hl = [j for j in np.where(w > 0)[0] if j in lk]
            hs = [j for j in np.where(w < 0)[0] if j in sk]
            L = (hl + [j for j in order if j not in hl][: max(0, k - len(hl))])[:k]
            S = (hs + [j for j in order[::-1] if j not in hs][: max(0, k - len(hs))])[:k]
            new[L], new[S] = 0.5 / k, -0.5 / k
        traded = np.abs(new - w).sum()
        r = np.nan_to_num(fwd1[i])
        f = np.nan_to_num(fund_next[i])
        lg[i] = np.sum(np.where(new > 0, new * (r - f), 0)) * 2  # as a return on the long half
        sh[i] = np.sum(np.where(new < 0, new * (r - f), 0)) * 2
        net[i] = np.sum(new * (r - f)) - cost * traded
        turn[i] = traded
        w = new * (1 + r)
    return net, lg, sh, turn


def stats(x, mask):
    x = x[mask & ~np.isnan(x)]
    if len(x) < 30:
        return "n/a"
    years = len(x) / 365
    cagr = np.prod(1 + x) ** (1 / years) - 1
    eq = np.cumprod(1 + x)
    dd = (eq / np.maximum.accumulate(eq) - 1).min()
    return f"{cagr:+7.1%}/yr, worst {dd:+6.1%}, Sharpe {x.mean() / x.std() * math.sqrt(365):4.2f}"


def line(label, res):
    net, _, _, turn = res
    print(f"  {label:44} DEV {stats(net, DEV)} | HOLDOUT {stats(net, HOLD)} | trades {np.nanmean(turn):.0%}/day")


P3, P1 = z["pred3"], z["pred1"]
print("Sanity: random picks through the same engine (should lose roughly the fees)")
line("random, top 25", run(P3, seed=1))
line("random, top 100", run(P3, top=100, seed=2))

print("\nThe model (next 3 days), top 25 coins, funding charged on the day held")
base = run(P3)
line("as tested (0.10% per unit traded)", base)
for cost in (0.0015, 0.002, 0.003):
    line(f"costs {cost:.2%}", run(P3, cost=cost))
for lag in (1, 2):
    line(f"trading {lag} day(s) late", run(P3, lag=lag))
line("hold wider (keep while in top 2x)", run(P3, keep=2.0))
line("top 10 coins only", run(P3, top=10))
line("top 50 coins", run(P3, top=50))
print("\nThe model (next day), top 25 and top 100")
line("next day, top 25", run(P1))
line("next day, top 25, 1 day late", run(P1, lag=1))
line("next day, top 100", run(P1, top=100))

print("\nWhere the money comes from (next 3 days, top 25): each side as a return on its half of the account")
net, lg, sh, turn = base
for label, x in (("long side (bought coins, minus funding paid)", lg), ("short side (shorted coins, plus funding)", sh)):
    print(f"  {label:48} DEV {stats(x, DEV)} | HOLDOUT {stats(x, HOLD)}")
b = np.nan_to_num(fwd1[:, btc])
ok = ~np.isnan(net) & (DEV | HOLD)
print(f"  correlation with BTC's daily move: {np.corrcoef(net[ok], b[ok])[0, 1]:+.2f} (0 = doesn't depend on the market)")

print("\nBy year (next 3 days, top 25, after costs):")
for y in range(2018, 2027):
    m = np.array([time.gmtime(d).tm_year == y for d in days]) & (DEV | HOLD)
    if m.sum() > 30:
        x = net[m & ~np.isnan(net)]
        print(f"  {y}: {np.prod(1 + x) - 1:+7.1%}   (BTC {np.prod(1 + b[m]) - 1:+7.1%})")
