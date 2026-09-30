"""Is there any pattern that says which traders' longs are worth copying? Research only.

    <python with numpy + scikit-learn> research/copy_patterns.py [--cost 0.005]

Every long opened by a trader in the two pools (Hyperliquid sample, OKX lead traders; see copy_recheck.py) is a
candidate copy, bought when they open and sold when they close (hourly closes). For each, only what was known at that
moment is used as a feature: the trader's recent record (7- and 30-day results, win rate, how their earlier copies did,
how long they hold), how many other traders are long or short the coin, the coin's and BTC's recent trend, leverage.

Patterns are learned on trades that finished before `--split` and judged only on trades opened after it, which the
search never saw. Results are per trader with a 95% range from resampling traders. "Skill" = the copy's move minus
the same coin bought at random times for the same number of hours (so a rising market doesn't count).
"""
import argparse
import bisect
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.symbols import normalize_coin  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--cost", type=float, default=0.005)
ap.add_argument("--split", default="2026-09-01", help="learn before this day, test from it")
args = ap.parse_args()
H, D = 3_600_000, 86_400_000
SPLIT = int(time.mktime(time.strptime(args.split, "%Y-%m-%d"))) * 1000
random.seed(3)
np.random.seed(3)
db = sqlite3.connect(ROOT / "data" / "research.db")
okx_coins = {r[0] for r in sqlite3.connect(ROOT / "data" / "trading.db").execute("SELECT coin FROM markets")}
close = defaultdict(dict)
for pair, hour, c in db.execute("SELECT pair, hour, close FROM hourly"):
    close[pair][hour] = c

# --- every position (longs and shorts) with the trader's own prices -------------------------------------------
pos = []  # dict(pool, trader, coin, side, t0, t1, p0, p1, lever)
pnl_events = defaultdict(list)  # (pool, trader) -> [(time, usd)]
cur, open_ = None, {}
for addr, coin, px, sz, side, t, start_pos, pnl, fee in db.execute(
        "SELECT address, coin, px, sz, side, time, start_pos, closed_pnl, fee FROM hl_fills ORDER BY address, time"):
    if addr != cur:
        cur, open_ = addr, {}
    pnl_events[("HL", addr)].append((t, pnl - fee))
    after = start_pos + (sz if side == "B" else -sz)
    for d in (1, -1):
        was, now_ = d * start_pos, d * after
        if was <= 0 < now_:
            open_[(coin, d)] = (t, px)
        elif was > 0 >= now_ and (coin, d) in open_:
            t0, p0 = open_.pop((coin, d))
            sym, mult = normalize_coin(coin)
            if sym:
                pos.append({"pool": "HL", "trader": addr, "coin": sym, "side": d, "t0": t0, "t1": t, "p0": p0,
                            "p1": px, "lever": np.nan})
for code, inst, side, lever, p0, p1, t0, t1, pnl in db.execute(
        "SELECT code, inst_id, side, lever, open_px, close_px, open_time, close_time, pnl FROM okx_lead_positions "
        "WHERE inst_id LIKE '%-USDT-SWAP' AND open_px > 0 AND close_px > 0"):
    pos.append({"pool": "OKX", "trader": code, "coin": inst.split("-")[0], "side": 1 if side == "long" else -1,
                "t0": t0, "t1": t1, "p0": p0, "p1": p1, "lever": lever})
    pnl_events[("OKX", code)].append((t1, pnl))
for p in pos:
    p["ret"] = p["side"] * (p["p1"] / p["p0"] - 1)
print(f"{len(pos):,} positions by {len({(p['pool'], p['trader']) for p in pos})} traders")

# per-trader history, indexed by close time, for "what did they do before this moment"
hist = defaultdict(list)
for p in sorted(pos, key=lambda p: p["t1"]):
    hist[(p["pool"], p["trader"])].append(p)
hist_t1 = {k: [p["t1"] for p in v] for k, v in hist.items()}
for k in pnl_events:
    pnl_events[k].sort()
pnl_t = {k: [t for t, _ in v] for k, v in pnl_events.items()}
pnl_cum = {k: np.cumsum([x for _, x in v]) for k, v in pnl_events.items()}
# open intervals per coin and side, to count other traders in the same trade
starts, ends = defaultdict(list), defaultdict(list)
for p in pos:
    starts[(p["pool"], p["coin"], p["side"])].append(p["t0"])
    ends[(p["pool"], p["coin"], p["side"])].append(p["t1"])
for k in starts:
    starts[k].sort()
    ends[k].sort()
btc_hours = sorted(close["BTC"])
btc_px = np.array([close["BTC"][h] for h in btc_hours])
btc_cum = np.concatenate([[0], np.cumsum(btc_px)])


def usd_since(k, t, days):
    ts = pnl_t.get(k)
    if not ts:
        return 0.0
    i, j = bisect.bisect_left(ts, t - days * D), bisect.bisect_left(ts, t)
    c = pnl_cum[k]
    return float((c[j - 1] if j else 0) - (c[i - 1] if i else 0))


def open_count(k, t):
    return bisect.bisect_right(starts[k], t) - bisect.bisect_right(ends[k], t)


def coin_ret(coin, h, hours):
    a, b = close[coin].get(h - hours), close[coin].get(h)
    return b / a - 1 if a and b else np.nan


def btc_trend(h):
    i = bisect.bisect_right(btc_hours, h) - 1
    if i < 1200:
        return np.nan
    return btc_px[i] / ((btc_cum[i + 1] - btc_cum[i + 1 - 1200]) / 1200) - 1


FEATURES = ["pool_okx", "major", "ret7", "ret30", "win30", "n30", "usd7", "usd30", "prev_copy", "hold_med",
            "others_long", "others_short", "coin_24h", "coin_7d", "btc_vs_50d", "lever"]
span = (min(p["t0"] for p in pos) // H, max(p["t1"] for p in pos) // H)
rows = []
for p in pos:
    if p["side"] != 1 or p["coin"] not in okx_coins:
        continue  # copies are spot longs
    h0, h1 = p["t0"] // H, p["t1"] // H
    a, b = close[p["coin"]].get(h0), close[p["coin"]].get(h1)
    if not (a and b):
        continue
    k = (p["pool"], p["trader"])
    past = hist[k][:bisect.bisect_left(hist_t1[k], p["t0"])]
    p7 = [x for x in past if x["t1"] >= p["t0"] - 7 * D]
    p30 = [x for x in past if x["t1"] >= p["t0"] - 30 * D]
    prev = [x["copy_net"] for x in past if "copy_net" in x]
    dur = h1 - h0
    plac = [close[p["coin"]][s + dur] / close[p["coin"]][s] - 1 for s in
            (random.randint(span[0], span[1] - dur) for _ in range(20)) if close[p["coin"]].get(s) and close[p["coin"]].get(s + dur)]
    if len(plac) < 8:
        continue
    gross = b / a - 1
    p["copy_net"] = gross - args.cost  # visible to this trader's later trades as "how their copies did"
    rows.append({
        "pool": p["pool"], "trader": p["trader"], "t0": p["t0"], "t1": p["t1"], "net": gross - args.cost,
        "skill": gross - statistics.fmean(plac),
        "pool_okx": float(p["pool"] == "OKX"), "major": float(p["coin"] in ("BTC", "ETH")),
        "ret7": statistics.fmean(x["ret"] for x in p7) if p7 else np.nan,
        "ret30": statistics.fmean(x["ret"] for x in p30) if p30 else np.nan,
        "win30": sum(x["ret"] > 0 for x in p30) / len(p30) if p30 else np.nan, "n30": len(p30),
        "usd7": usd_since(k, p["t0"], 7), "usd30": usd_since(k, p["t0"], 30),
        "prev_copy": statistics.fmean(prev[-20:]) if prev else np.nan,
        "hold_med": statistics.median((x["t1"] - x["t0"]) / H for x in past[-50:]) if past else np.nan,
        "others_long": open_count((p["pool"], p["coin"], 1), p["t0"]) - 1,
        "others_short": open_count((p["pool"], p["coin"], -1), p["t0"]),
        "coin_24h": coin_ret(p["coin"], h0, 24), "coin_7d": coin_ret(p["coin"], h0, 168),
        "btc_vs_50d": btc_trend(h0), "lever": p["lever"],
    })
train = [r for r in rows if r["t1"] < SPLIT]
test = [r for r in rows if r["t0"] >= SPLIT]
print(f"{len(rows):,} copyable longs: learn on {len(train):,} (finished before {args.split}), test on {len(test):,} "
      f"(opened after)\n")


def per_trader(xs, key):
    per = defaultdict(list)
    for x in xs:
        per[(x["pool"], x["trader"])].append(x[key])
    means = [statistics.fmean(v) for v in per.values()]
    if len(means) < 5:
        return np.nan, np.nan, np.nan, len(means)
    boots = sorted(statistics.fmean(random.choices(means, k=len(means))) for _ in range(1000))
    return statistics.fmean(means), boots[25], boots[975], len(means)


def show(label, xs):
    n, nlo, nhi, k = per_trader(xs, "net")
    s, slo, shi, _ = per_trader(xs, "skill")
    print(f"  {label:44} {len(xs):6,} copies {k:4} traders | net {n:+.2%} [{nlo:+.2%}, {nhi:+.2%}] | "
          f"skill {s:+.2%} [{slo:+.2%}, {shi:+.2%}]")


print("Baseline: copy every long")
show("learning period (Jul-Aug)", train)
show("test period (Sep)", test)

# --- 1. simple rules: the best fifth of each feature, chosen on the learning period ------------------------------
print("\nSimple rules: pick the best fifth of one feature on Jul-Aug, then see if it still works in Sep")
for f in FEATURES:
    vals = np.array([r[f] for r in train], dtype=float)
    ok = ~np.isnan(vals)
    if ok.sum() < 200 or len(set(vals[ok])) < 3:
        continue
    edges = np.unique(np.quantile(vals[ok], [0, .2, .4, .6, .8, 1]))
    best, best_s = None, -9
    for lo_, hi_ in zip(edges, edges[1:]):
        xs = [r for r in train if lo_ <= r[f] <= hi_]
        s = per_trader(xs, "skill")[0]
        if len(xs) >= 100 and s > best_s:
            best, best_s = (lo_, hi_), s
    if best is None:
        continue
    tr = [r for r in train if best[0] <= r[f] <= best[1]]
    te = [r for r in test if not np.isnan(r[f]) and best[0] <= r[f] <= best[1]]
    s_tr = per_trader(tr, "skill")[0]
    s_te, lo, hi, k = per_trader(te, "skill")
    n_te = per_trader(te, "net")[0]
    print(f"  {f:12} {best[0]:>10.4g} to {best[1]:<10.4g} learned skill {s_tr:+.2%} -> Sep skill {s_te:+.2%} "
          f"[{lo:+.2%}, {hi:+.2%}], net {n_te:+.2%} ({len(te):,} copies, {k} traders)")

# --- 2. a machine-learning model on all features together --------------------------------------------------------
X_tr = np.array([[r[f] for f in FEATURES] for r in train], dtype=float)
X_te = np.array([[r[f] for f in FEATURES] for r in test], dtype=float)
print("\nMachine-learning model (gradient boosting, all features), learned on Jul-Aug, judged on Sep:")
for target in ("skill", "net"):
    y = np.clip(np.array([r[target] for r in train]), -0.5, 0.5)
    model = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                          l2_regularization=1.0, random_state=0).fit(X_tr, y)
    pred = model.predict(X_te)
    order = np.argsort(-pred)
    actual = np.array([r[target] for r in test])
    rank_corr = np.corrcoef(np.argsort(np.argsort(pred)), np.argsort(np.argsort(actual)))[0, 1]
    print(f"  predicting {target}: rank correlation with what happened {rank_corr:+.3f}")
    for share, label in ((0.1, "top 10% predicted"), (0.3, "top 30% predicted"), (1.0, "all")):
        pick = [test[i] for i in order[: max(1, int(share * len(test)))]]
        show(f"  {label}", pick)

# --- 3. is it the traders, or just buying dips? ---------------------------------------------------------------
from sklearn.inspection import permutation_importance  # noqa: E402

y = np.clip(np.array([r["skill"] for r in train]), -0.5, 0.5)
model = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                      l2_regularization=1.0, random_state=0).fit(X_tr, y)
imp = permutation_importance(model, X_te, np.clip([r["skill"] for r in test], -0.5, 0.5), n_repeats=5,
                             random_state=0)
print("\nWhat the model leans on (drop in test accuracy when a feature is shuffled):")
for i in np.argsort(-imp.importances_mean)[:8]:
    print(f"  {FEATURES[i]:12} {imp.importances_mean[i]:+.5f}")

pred = model.predict(X_te)
top = [test[i] for i in np.argsort(-pred)[: len(test) // 10]]
print("\nTop 10% picks in September, by pool and by week:")
for pool in ("HL", "OKX"):
    show(f"{pool}", [r for r in top if r["pool"] == pool])
for wk in sorted({time.strftime("%W", time.gmtime(r["t0"] / 1000)) for r in top}):
    show(f"week {wk}", [r for r in top if time.strftime("%W", time.gmtime(r["t0"] / 1000)) == wk])

# The same coins at the same moments, without the trader: every OKX-listed coin that had also fallen over 7 days
# when a top pick was made, bought then and held the same number of hours.
print("\nWithout traders: buy any coin that fell over the last 7 days, at the same moments, same hold:")
coins = [c for c in okx_coins if len(close[c]) > 1000]
free = []
for r in top:
    h0, dur = r["t0"] // H, (r["t1"] - r["t0"]) // H
    for c in random.sample(coins, min(8, len(coins))):
        m7 = coin_ret(c, h0, 168)
        a, b = close[c].get(h0), close[c].get(h0 + dur)
        if a and b and not np.isnan(m7) and m7 < 0:
            free.append({"pool": r["pool"], "trader": r["trader"], "net": b / a - 1 - args.cost, "skill": np.nan})
print(f"  trader-free dips at the top picks' moments: {len(free):,} buys, "
      f"net {statistics.fmean(x['net'] for x in free):+.2%} per buy (top picks themselves: "
      f"{statistics.fmean(r['net'] for r in top):+.2%})")

# A second fold: learn on July, test on August.
cut = SPLIT - 31 * D
tr2 = [r for r in rows if r["t1"] < cut]
te2 = [r for r in rows if cut <= r["t0"] < SPLIT]
if len(tr2) > 1000:
    # features July has no values for yet (e.g. the 50-day BTC average needs 50 days of hourly prices) are left out
    use = [f for f in FEATURES if len({r[f] for r in tr2 if not np.isnan(r[f])}) >= 3]
    m2 = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                       l2_regularization=1.0, random_state=0).fit(
        np.array([[r[f] for f in use] for r in tr2], dtype=float),
        np.clip([r["skill"] for r in tr2], -0.5, 0.5))
    p2 = m2.predict(np.array([[r[f] for f in use] for r in te2], dtype=float))
    print(f"\nSecond check: learn on July ({len(tr2):,}), judge on August ({len(te2):,}):")
    show("top 10% predicted", [te2[i] for i in np.argsort(-p2)[: len(te2) // 10]])
    show("all", te2)
