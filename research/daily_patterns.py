"""Search for daily trading patterns in every coin's market data, honestly. Research only.

    <python with numpy + scikit-learn> research/daily_patterns.py [--top 100] [--cost 0.0025]

Data: research/collect_market.py (Binance spot incl. delisted coins, Binance futures and funding, Bybit open interest,
Deribit implied volatility). Each day the `--top` most traded coins with 90+ days of history form the universe.

About 35 signals per coin and day, all known at that day's close (00:00 UTC): price trends over 1-90 days, volatility,
distance from averages, volume surges, buying pressure (taker buys), trade counts, funding rates, futures activity,
open-interest changes, plus market-wide ones (BTC trend, breadth, implied volatility).

To avoid fooling ourselves:
- DEV period (2018-07 .. 2024-06): everything is chosen here.
- HOLDOUT period (2024-07 .. now): only the strategies chosen on DEV are shown there, once. That's the real test.
- Costs: `--cost` per unit traded (0.20% taker + slippage), so daily reshuffling pays its real price.
- Statistics per month (days in a month move together), so a lucky week can't pass as a pattern.
"""
import argparse
import math
import sqlite3
import time
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--top", type=int, default=100)
ap.add_argument("--cost", type=float, default=0.0025)
ap.add_argument("--n", type=int, default=10, help="coins held by the selection strategies")
args = ap.parse_args()
DAY = 86400
DEV = (1_530_403_200, 1_719_792_000)  # 2018-07-01 .. 2024-07-01
HOLD = (1_719_792_000, 10**12)
db = sqlite3.connect(ROOT / "data" / "market.db")
np.seterr(all="ignore")

# --- load into day x coin arrays --------------------------------------------------------------------------
coins = [r[0] for r in db.execute("SELECT coin FROM spot_1d GROUP BY coin HAVING COUNT(*) >= 120")]
cidx = {c: i for i, c in enumerate(coins)}
d0, d1 = db.execute("SELECT MIN(day), MAX(day) FROM spot_1d").fetchone()
days = np.arange(d0, d1 + DAY, DAY)
T, N = len(days), len(coins)
didx = {int(d): i for i, d in enumerate(days)}


def grid():
    return np.full((T, N), np.nan)


O, Hh, L, C, V, TB, TR = (grid() for _ in range(7))
for coin, day, o, h, lo, c, qv, tb, tr in db.execute("SELECT * FROM spot_1d"):
    if coin in cidx and day in didx:
        i, j = didx[day], cidx[coin]
        O[i, j], Hh[i, j], L[i, j], C[i, j], V[i, j], TB[i, j], TR[i, j] = o, h, lo, c, qv, tb, tr
FV, FTB, FUND, OI, FUND_DAY = grid(), grid(), grid(), grid(), grid()
for coin, day, c, qv, tb in db.execute("SELECT * FROM fut_1d"):
    if coin in cidx and day in didx:
        FV[didx[day], cidx[coin]], FTB[didx[day], cidx[coin]] = qv, tb
for coin, day, rate, n in db.execute("SELECT * FROM funding"):
    if coin in cidx and day in didx:
        FUND[didx[day], cidx[coin]] = rate
        FUND_DAY[didx[day], cidx[coin]] = rate * n  # what a position pays (long) or gets (short) that day
for coin, day, oi in db.execute("SELECT * FROM oi_1d"):
    if coin in cidx and day in didx:
        OI[didx[day], cidx[coin]] = oi
dvol = {cur: np.full(T, np.nan) for cur in ("BTC", "ETH")}
for cur, day, c in db.execute("SELECT * FROM dvol"):
    if day in didx:
        dvol[cur][didx[day]] = c
print(f"{N} coins, {T} days ({time.strftime('%Y-%m-%d', time.gmtime(d0))} .. {time.strftime('%Y-%m-%d', time.gmtime(d1))})")


# --- helpers ----------------------------------------------------------------------------------------------
def shift(a, k):
    out = np.full_like(a, np.nan)
    if k > 0:
        out[k:] = a[:-k]
    elif k < 0:
        out[:k] = a[-k:]
    else:
        out[:] = a
    return out


def roll_mean(a, n):
    """Mean over the last n days (NaN-aware, needs 2/3 of them)."""
    x = np.nan_to_num(a)
    cnt = np.cumsum(~np.isnan(a), axis=0, dtype=float)
    s = np.cumsum(x, axis=0)
    s = np.vstack([np.zeros((1,) + a.shape[1:]), s])
    cnt = np.vstack([np.zeros((1,) + a.shape[1:]), cnt])
    tot = s[n:] - s[:-n]
    k = cnt[n:] - cnt[:-n]
    out = np.full_like(a, np.nan, dtype=float)
    out[n - 1:] = np.where(k >= max(1, 2 * n // 3), tot / np.maximum(k, 1), np.nan)
    return out


def roll_std(a, n):
    m = roll_mean(a, n)
    m2 = roll_mean(a * a, n)
    return np.sqrt(np.maximum(m2 - m * m, 0))


def roll_max(a, n):
    out = np.full_like(a, np.nan)
    for i in range(n - 1, len(a)):
        out[i] = np.nanmax(a[i - n + 1:i + 1], axis=0)
    return out


lr = np.log(C / shift(C, 1))
age = np.cumsum(~np.isnan(C), axis=0)
vol30 = roll_mean(V, 30)
# universe: the top coins by 30-day volume with 90+ days of history, known at each close
univ = np.zeros((T, N), bool)
for i in range(T):
    ok = (age[i] >= 90) & ~np.isnan(C[i]) & ~np.isnan(vol30[i])
    idx = np.where(ok)[0]
    if len(idx):
        univ[i, idx[np.argsort(-vol30[i, idx])[:args.top]]] = True

fwd1 = shift(C, -1) / C - 1
fwd1 = np.where(np.isnan(fwd1) & univ, 0.0, fwd1)  # delisted overnight: out at the last close (optimistic)
fwd3 = shift(C, -3) / C - 1
fwd7 = shift(C, -7) / C - 1

btc = cidx["BTC"]
F = {}  # coin signals
F["ret_1d"] = C / shift(C, 1) - 1
F["ret_3d"] = C / shift(C, 3) - 1
F["ret_7d"] = C / shift(C, 7) - 1
F["ret_14d"] = C / shift(C, 14) - 1
F["ret_30d"] = C / shift(C, 30) - 1
F["ret_90d"] = C / shift(C, 90) - 1
F["ret_30d_skip7"] = shift(C, 7) / shift(C, 30) - 1
F["vol_7d"] = roll_std(lr, 7)
F["vol_30d"] = roll_std(lr, 30)
F["vol_ratio"] = F["vol_7d"] / F["vol_30d"]
F["vs_avg20"] = C / roll_mean(C, 20) - 1
F["vs_avg50"] = C / roll_mean(C, 50) - 1
F["vs_avg200"] = C / roll_mean(C, 200) - 1
F["off_30d_high"] = C / roll_max(Hh, 30) - 1
F["day_range"] = (Hh - L) / C
F["close_in_range"] = (C - L) / (Hh - L)
F["volume_surge_1d"] = V / vol30
F["volume_surge_7d"] = roll_mean(V, 7) / vol30
F["buy_pressure_1d"] = TB / V
F["buy_pressure_7d"] = roll_mean(TB, 7) / roll_mean(V, 7)
F["buy_pressure_change"] = F["buy_pressure_1d"] - roll_mean(TB / V, 30)
F["trades_surge"] = TR / roll_mean(TR, 30)
F["trade_size_change"] = (V / TR) / roll_mean(V / TR, 30)
F["funding_1d"] = FUND
F["funding_7d"] = roll_mean(FUND, 7)
F["funding_vs_30d"] = (FUND - roll_mean(FUND, 30)) / roll_std(FUND, 30)
F["futures_vs_spot_volume"] = FV / V
F["futures_buy_pressure"] = FTB / FV
F["oi_change_1d"] = OI / shift(OI, 1) - 1
F["oi_change_7d"] = OI / shift(OI, 7) - 1
F["oi_vs_volume"] = OI * C / roll_mean(FV, 7)
F["size_rank"] = -vol30  # bigger coins first
MKT = {}  # market-wide signals (the same for every coin on a day)
uni_ret = np.array([np.nanmean(F["ret_1d"][i][univ[i]]) if univ[i].any() else np.nan for i in range(T)])
MKT["btc_ret_1d"] = F["ret_1d"][:, btc]
MKT["btc_ret_7d"] = F["ret_7d"][:, btc]
MKT["btc_vs_avg50"] = F["vs_avg50"][:, btc]
MKT["btc_vs_avg200"] = F["vs_avg200"][:, btc]
MKT["breadth_above_avg50"] = np.array([np.nanmean((F["vs_avg50"][i] > 0)[univ[i]]) if univ[i].any() else np.nan
                                       for i in range(T)])
MKT["market_ret_1d"] = uni_ret
MKT["market_ret_7d"] = np.array([np.nanmean(F["ret_7d"][i][univ[i]]) if univ[i].any() else np.nan for i in range(T)])
MKT["btc_funding_7d"] = F["funding_7d"][:, btc]
MKT["btc_implied_vol"] = dvol["BTC"]
MKT["btc_implied_vol_change"] = dvol["BTC"] / shift(dvol["BTC"][:, None], 7)[:, 0] - 1


def period_mask(p):
    return (days >= p[0]) & (days < p[1])


def month_key(i):
    return time.strftime("%Y-%m", time.gmtime(days[i]))


def month_stats(daily, mask):
    """Mean daily value and its t-stat computed from monthly averages."""
    by = {}
    for i in np.where(mask & ~np.isnan(daily))[0]:
        by.setdefault(month_key(i), []).append(daily[i])
    m = np.array([np.mean(v) for v in by.values()])
    if len(m) < 6:
        return np.nan, np.nan
    return float(np.nanmean(daily[mask])), float(m.mean() / (m.std(ddof=1) / math.sqrt(len(m))))


def rank_ic(sig, target, i):
    ok = univ[i] & ~np.isnan(sig[i]) & ~np.isnan(target[i])
    if ok.sum() < 20:
        return np.nan
    a, b = sig[i, ok], target[i, ok]
    ra, rb = a.argsort().argsort(), b.argsort().argsort()
    return float(np.corrcoef(ra, rb)[0, 1])


# --- 1. each signal on its own: does it rank tomorrow's winners? --------------------------------------------
dev, hold = period_mask(DEV), period_mask(HOLD)
print("\n1. Each signal alone: rank correlation with the next day's move across coins (IC, +/-)")
print("   (|t| > 2 on DEV looks like a pattern; the HOLDOUT column is the real test)")
ic_dev = {}
rows = []
for name, sig in F.items():
    daily = np.array([rank_ic(sig, fwd1, i) for i in range(T)])
    md, td = month_stats(daily, dev)
    mh, th = month_stats(daily, hold)
    ic_dev[name] = (md, td)
    rows.append((abs(td) if not np.isnan(td) else 0, name, md, td, mh, th))
for _, name, md, td, mh, th in sorted(rows, reverse=True):
    same = "" if np.isnan(td) or abs(td) < 2 else ("  holds" if np.sign(mh) == np.sign(md) and abs(th) >= 2 else
                                                   "  weaker" if np.sign(mh) == np.sign(md) else "  REVERSED")
    print(f"   {name:24} DEV {md:+.4f} (t {td:+5.1f})   HOLDOUT {mh:+.4f} (t {th:+5.1f}){same}")


# --- 2. long-only strategies: hold the top coins by a signal ------------------------------------------------
def simulate(score, mask, n=args.n, keep=2, cost=args.cost, gate=None):
    """Each day hold the n best-scored universe coins equally; a held coin stays while it's in the top n*keep
    (fewer trades). `gate` (bool per day): be in cash on days it's False. Returns daily net returns and turnover."""
    w = np.zeros(N)
    out, turn = np.full(T, np.nan), np.full(T, np.nan)
    for i in range(T - 1):
        if not mask[i]:
            continue
        s = np.where(univ[i] & ~np.isnan(score[i]), score[i], -np.inf)
        order = np.argsort(-s)
        valid = order[np.isfinite(s[order])]
        new = np.zeros(N)
        if (gate is None or gate[i]) and len(valid) >= n:
            top_keep = set(valid[: n * keep])
            held = [j for j in np.where(w > 0)[0] if j in top_keep]
            pick = held + [j for j in valid if j not in held][: max(0, n - len(held))]
            new[pick[:n]] = 1.0 / n
        traded = np.abs(new - w).sum()
        r = np.nansum(new * np.nan_to_num(fwd1[i]))
        out[i] = r - cost * traded
        turn[i] = traded
        w = new * (1 + np.nan_to_num(fwd1[i]))
        w = w / w.sum() if w.sum() > 0 else w
    return out, turn


def summary(daily, mask):
    x = daily[mask & ~np.isnan(daily)]
    if len(x) < 30:
        return "n/a"
    years = len(x) / 365
    growth = np.prod(1 + x)
    cagr = growth ** (1 / years) - 1
    eq = np.cumprod(1 + x)
    dd = (eq / np.maximum.accumulate(eq) - 1).min()
    sharpe = x.mean() / x.std() * math.sqrt(365)
    return f"{cagr:+7.1%} a year, worst drop {dd:+6.1%}, Sharpe {sharpe:4.2f}"


equal_w = np.array([np.nanmean(fwd1[i][univ[i]]) if univ[i].any() else np.nan for i in range(T)])
btc_hold = fwd1[:, btc]
trend_gate = np.nan_to_num(MKT["btc_vs_avg50"]) > 0
print(f"\n2. Long only: hold the top {args.n} coins by a signal (after {args.cost:.2%} per unit traded)")
print(f"   {'Hold BTC':34} DEV {summary(btc_hold, dev)} | HOLDOUT {summary(btc_hold, hold)}")
print(f"   {'Hold all top coins equally':34} DEV {summary(equal_w, dev)} | HOLDOUT {summary(equal_w, hold)}")
# choose on DEV only: the 5 signals with the strongest DEV IC, in the direction DEV says
chosen = sorted(ic_dev, key=lambda k: -abs(ic_dev[k][1] if not np.isnan(ic_dev[k][1]) else 0))[:5]
for name in chosen:
    sign = 1 if ic_dev[name][0] > 0 else -1
    for label, gate in (("", None), (" + BTC uptrend", trend_gate)):
        daily, turn = simulate(sign * F[name], dev | hold, gate=gate)
        print(f"   {('+' if sign > 0 else '-') + name + label:34} DEV {summary(daily, dev)} | HOLDOUT {summary(daily, hold)}"
              f" | trades {np.nanmean(turn[dev]):.0%} of the account a day")

# --- 3. machine learning on every signal, retrained as time goes by ------------------------------------------
print("\n3. Machine learning on all signals (gradient boosting), retrained every 3 months on all earlier data,")
print("   predicting which coins beat the others over the next day and the next 3 days")
names = list(F) + list(MKT)


def xs_rank(a):
    out = np.full_like(a, np.nan)
    for i in range(T):
        ok = univ[i] & ~np.isnan(a[i])
        if ok.sum() > 1:
            out[i, ok] = a[i, ok].argsort().argsort() / (ok.sum() - 1)
    return out


RANKED = {k: xs_rank(v) for k, v in F.items()}
X_all = np.stack([RANKED[k] for k in F] + [np.repeat(MKT[k][:, None], N, axis=1) for k in MKT], axis=2)
excess1 = fwd1 - np.array([np.nanmean(fwd1[i][univ[i]]) if univ[i].any() else np.nan for i in range(T)])[:, None]
excess3 = fwd3 - np.array([np.nanmean(fwd3[i][univ[i]]) if univ[i].any() else np.nan for i in range(T)])[:, None]
first = np.where(days >= DEV[0])[0][0]
PRED = {}
for tname, target, horizon in (("next day", excess1, 1), ("next 3 days", excess3, 3)):
    pred = grid()
    for start in range(first, T, 91):
        train_end = start - horizon  # targets must be finished before the model is used
        tr = np.zeros((T, N), bool)
        tr[:train_end] = univ[:train_end]
        tr &= ~np.isnan(target)
        if tr.sum() < 20_000:
            continue
        model = HistGradientBoostingRegressor(max_iter=150, learning_rate=0.05, max_leaf_nodes=31,
                                              min_samples_leaf=500, l2_regularization=1.0, random_state=0)
        sel = np.where(tr)
        rng = np.random.default_rng(0)
        k = rng.choice(len(sel[0]), min(300_000, len(sel[0])), replace=False)
        Xtr = X_all[sel[0][k], sel[1][k]]
        cols = [c for c in range(Xtr.shape[1]) if len(np.unique(Xtr[~np.isnan(Xtr[:, c]), c])) >= 3]
        model.fit(Xtr[:, cols], np.clip(target[sel[0][k], sel[1][k]], -0.5, 0.5))
        stop = min(T, start + 91)
        te = univ[start:stop]
        ii, jj = np.where(te)
        if len(ii):
            pred[start + ii, jj] = model.predict(X_all[start + ii, jj][:, cols])
    PRED[horizon] = pred
    daily_ic = np.array([rank_ic(pred, fwd1 if horizon == 1 else fwd3, i) for i in range(T)])
    md, td = month_stats(daily_ic, dev)
    mh, th = month_stats(daily_ic, hold)
    print(f"   {tname}: IC DEV {md:+.4f} (t {td:+.1f}), HOLDOUT {mh:+.4f} (t {th:+.1f})")
    for label, gate, keep in (("top coins, daily", None, 1), ("top coins, fewer trades", None, 3),
                              ("fewer trades + BTC uptrend", trend_gate, 3)):
        daily, turn = simulate(pred, dev | hold, keep=keep, gate=gate)
        gross, _ = simulate(pred, dev | hold, keep=keep, gate=gate, cost=0.0)
        print(f"     {label:30} DEV {summary(daily, dev)} | HOLDOUT {summary(daily, hold)} | before costs HOLDOUT "
              f"{summary(gross, hold).split(',')[0]} | trades {np.nanmean(turn[dev]):.0%}/day")

# --- 4. market timing: when to hold at all -------------------------------------------------------------------
print("\n4. Market timing: does any market-wide signal say when to hold BTC? (the trend bot's question)")
for name, sig in MKT.items():
    ok = ~np.isnan(sig) & ~np.isnan(btc_hold)
    for p, pname in ((dev, "DEV"), (hold, "HOLDOUT")):
        pass
    daily_dev = np.where(dev & ok, sig * btc_hold, np.nan)
    c_dev = np.corrcoef(sig[dev & ok], btc_hold[dev & ok])[0, 1] if (dev & ok).sum() > 50 else np.nan
    c_hold = np.corrcoef(sig[hold & ok], btc_hold[hold & ok])[0, 1] if (hold & ok).sum() > 50 else np.nan
    print(f"   {name:24} correlation with BTC's next day: DEV {c_dev:+.3f}, HOLDOUT {c_hold:+.3f}")


# --- 5. long/short on futures: buy the coins the signals favour, short the ones they don't ---------------------
FUT_COST = 0.001  # per unit traded: 0.05% OKX futures taker + spread and slippage


def long_short(score, mask, top, frac=0.2, keep=1.5, cost=FUT_COST):
    """Among the `top` most traded coins that have a perpetual (funding data) that day: long the best `frac`,
    short the worst `frac`, half the account each side. Funding is paid on longs and received on shorts."""
    w = np.zeros(N)
    out = np.full(T, np.nan)
    for i in range(T - 1):
        if not mask[i]:
            continue
        ok = univ[i] & ~np.isnan(score[i]) & ~np.isnan(FUND_DAY[i]) & ~np.isnan(fwd1[i])
        idx = np.where(ok)[0]
        idx = idx[np.argsort(-vol30[i, idx])][:top]
        new = np.zeros(N)
        if len(idx) >= 10:
            k = max(2, int(len(idx) * frac))
            order = idx[np.argsort(-score[i, idx])]
            longs_keep = set(order[: int(k * keep)])
            shorts_keep = set(order[-int(k * keep):])
            held_l = [j for j in np.where(w > 0)[0] if j in longs_keep]
            held_s = [j for j in np.where(w < 0)[0] if j in shorts_keep]
            L = held_l + [j for j in order if j not in held_l][: max(0, k - len(held_l))]
            S = held_s + [j for j in order[::-1] if j not in held_s][: max(0, k - len(held_s))]
            new[L[:k]] = 0.5 / k
            new[S[:k]] = -0.5 / k
        traded = np.abs(new - w).sum()
        r = np.nansum(new * np.nan_to_num(fwd1[i])) - np.nansum(new * np.nan_to_num(FUND_DAY[i]))
        out[i] = r - cost * traded
        w = new * (1 + np.nan_to_num(fwd1[i]))
    return out


composite = np.zeros((T, N))
count = np.zeros((T, N))
for name in chosen:  # the 5 strongest signals on DEV, each pointed the way DEV says
    r = RANKED[name] * (1 if ic_dev[name][0] > 0 else -1)
    composite += np.nan_to_num(r)
    count += ~np.isnan(r)
composite = np.where(count > 0, composite / np.maximum(count, 1), np.nan)
print("\n5. Long/short on futures (buy the favoured fifth, short the worst fifth; futures fees and funding included)")
print(f"   the 5 signals, chosen on DEV: {', '.join(chosen)}")
for top in (25, 100):
    for label, score in (("5-signal mix", composite), ("calm coins only (-vol_30d)", -F["vol_30d"]),
                         ("ML, next day", PRED.get(1)), ("ML, next 3 days", PRED.get(3))):
        if score is None:
            continue
        daily = long_short(score, dev | hold, top)
        gross = long_short(score, dev | hold, top, cost=0.0)
        print(f"   top {top:3} coins, {label:28} DEV {summary(daily, dev)} | HOLDOUT {summary(daily, hold)}"
              f" | HOLDOUT before fees {summary(gross, hold).split(',')[0]}")

# Saved for research/longshort_check.py (robustness checks without retraining).
np.savez_compressed(ROOT / "data" / "daily_patterns_cache.npz", days=days, coins=np.array(coins), univ=univ,
                    close=C, fund_day=FUND_DAY, vol30=vol30, pred1=PRED[1], pred3=PRED[3], vol_30d=F["vol_30d"])
print("\nsaved data/daily_patterns_cache.npz")
