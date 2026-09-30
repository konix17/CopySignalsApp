"""Would the trend bot do better on perpetual futures: cheaper fees, shorting in downtrends, leverage? Research only.

    .venv/bin/python research/futures_backtest.py [--fee 0.001]

Same rule and data as the trend bot (OKX daily closes of BTC and ETH, 50/100/150-day averages, checked once a day,
from 2018-09) and the same engine idea as backtest.run, but weights can be negative (short) or above 1 (leverage).
Perpetuals differ from spot in three ways, all modelled:
- cost per unit traded: `--fee` (default 0.10% = 0.05% OKX taker + spread and slippage), vs 0.25% on spot
- funding, paid every day on the position: longs pay the 8-hour rate three times a day when it's positive, shorts
  receive it (Binance BTC/ETH history from late 2019; 0.01% per 8 hours before that, the usual baseline)
- liquidation: if a day's low (for longs) or high (for shorts) would take the account's loss past 100%, it's wiped out
Weights are reset to target every day (leveraged positions drift otherwise), which costs fees.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import backtest, history, trendbot  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--fee", type=float, default=0.001)
args = ap.parse_args()
DAY = 86400
PAIRS = list(trendbot.PAIRS)
conn = history.connect(ROOT / "data" / "history.db")
candles = history.load(conn, "1Dutc", PAIRS)
funding = history.load_funding(conn)
panel = backtest.Panel(candles)
hl = {p: {ts: (h, l) for ts, _o, h, l, _c, _v in rows} for p, rows in candles.items()}
rule = trendbot.strategy(funding)
BASE_FUNDING = 0.0001


def strength(pair, t):
    return rule.strength(panel, pair, t)


def spot_weights(t):
    return rule.weights(panel, t)  # the live bot: long only, with the negative-funding floor


def long_short(t, lev=1.0, short_only_below_all=False):
    """Each coin's half: long by how many averages the price is above; short when below them."""
    out = {}
    for pair in PAIRS:
        s = strength(pair, t)
        if s is None:
            continue
        w = (s if s > 0 else -1.0) if short_only_below_all else 2 * s - 1
        out[pair] = w * lev / len(PAIRS)
    return out


def run(weights, fee, pay_funding, start, end=None, label=""):
    i0, i1 = panel.index_of(start), panel.index_of(end) if end else len(panel.days) - 1
    equity, held, peak, worst, wiped, paid_fees, paid_funding = 1.0, {}, 1.0, 0.0, None, 0.0, 0.0
    curve = [(panel.days[i0], 1.0)]
    for t in range(i0, i1):
        target = {p: w for p, w in weights(t).items() if abs(w) > 1e-9}
        traded = sum(abs(target.get(p, 0) - held.get(p, 0)) for p in set(target) | set(held))
        equity *= 1 - fee * traded
        paid_fees += fee * traded
        held = target
        day_next = panel.days[t + 1]
        # liquidation: the worst moment of the next day, all positions at their extremes (cross margin)
        worst_move = 0.0
        for p, w in held.items():
            prev = panel.close[p][t]
            h, l = hl[p].get(day_next, (None, None))
            if prev and h and l:
                worst_move += w * ((l if w > 0 else h) / prev - 1)
        if worst_move <= -0.97:
            wiped = time.strftime("%Y-%m-%d", time.gmtime(day_next))
            equity = 0.0
            curve.append((day_next, 0.0))
            break
        growth = 0.0
        for p, w in held.items():
            a, b = panel.close[p][t], panel.close[p][t + 1]
            growth += w * (b / a - 1) if a and b else 0.0
            if pay_funding:
                rate = (funding.get(p) or {}).get(panel.days[t], BASE_FUNDING)
                growth -= w * 3 * rate  # longs pay positive funding, shorts receive it
                paid_funding += w * 3 * rate
        equity *= 1 + growth
        # weights drift with prices; rebalanced to target at the next check
        held = {p: w * (1 + ((panel.close[p][t + 1] / panel.close[p][t] - 1) if panel.close[p][t] else 0)) / (1 + growth)
                for p, w in held.items()} if growth > -1 else {}
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1)
        curve.append((day_next, equity))
    years = (curve[-1][0] - curve[0][0]) / DAY / 365.25
    cagr = equity ** (1 / years) - 1 if equity > 0 else -1.0
    by_year, base = {}, 1.0
    for i, (d, e) in enumerate(curve):
        y = time.gmtime(d).tm_year
        if i + 1 == len(curve) or time.gmtime(curve[i + 1][0]).tm_year != y:
            by_year[y] = e / base - 1 if base else -1.0
            base = e
    return {"label": label, "cagr": cagr, "dd": worst, "final": equity, "wiped": wiped, "by_year": by_year,
            "fees": paid_fees / years, "funding": paid_funding / years}


START = trendbot.BACKTEST_FROM
variants = [
    ("Spot, long only (the live bot)", spot_weights, 0.0025, False),
    ("Futures 1x, long only", spot_weights, args.fee, True),
    ("Futures 1x, long + short", lambda t: long_short(t), args.fee, True),
    ("Futures 1x, short only below all 3", lambda t: long_short(t, short_only_below_all=True), args.fee, True),
    ("Futures 2x, long only", lambda t: {p: 2 * w for p, w in spot_weights(t).items()}, args.fee, True),
    ("Futures 3x, long only", lambda t: {p: 3 * w for p, w in spot_weights(t).items()}, args.fee, True),
    ("Futures 2x, long + short", lambda t: long_short(t, 2), args.fee, True),
    ("Hold BTC", lambda t: {"BTC-USDT": 1.0}, 0.0025, False),
]
periods = [("2018-09 to now", START, None), ("2018-09 to 2022", START, 1_672_531_200), ("2023 to now", 1_672_531_200, None)]
print(f"BTC+ETH trend rule on futures (fee {args.fee:.2%} per unit traded, funding and liquidation included)\n")
for name, s, e in periods:
    print(name)
    for label, w, fee, fund in variants:
        r = run(w, fee, fund, s, e, label)
        extra = f"  WIPED OUT {r['wiped']}" if r["wiped"] else ""
        print(f"  {label:36} {r['cagr']:+7.1%} a year, worst drop {r['dd']:+6.1%}, $1,000 -> ${1000 * r['final']:>10,.0f}"
              f" | fees {r['fees']:.1%}/yr, funding {r['funding']:+.1%}/yr{extra}")
    print()
print("By year (2018-09 to now):")
rows = [(label, run(w, fee, fund, START, None, label)) for label, w, fee, fund in variants]
years = sorted(rows[0][1]["by_year"])
print(f"  {'':36}" + "".join(f"{y:>8}" for y in years))
for label, r in rows:
    print(f"  {label:36}" + "".join(f"{r['by_year'].get(y, float('nan')):+8.0%}" for y in years))
