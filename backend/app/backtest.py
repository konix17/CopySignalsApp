"""Backtests on daily OKX history (history.py): would a strategy have made money after fees?

Long-only spot, no leverage, as the app trades. Each day a strategy decides target weights from data up to that
day's close (no peeking); the portfolio moves to them at the close, paying `cost` on every unit traded (taker fee
plus spread and slippage), then earns the next day's returns. Cash earns nothing.

The strategies are fixed in advance and every one is reported, winners and losers, so the results can't be
cherry-picked. A backtest is still not a promise: the coin list is today's survivors, and markets change.
"""

import math
import statistics
import time
from dataclasses import dataclass, field

DAY = 86400
YEAR_DAYS = 365
DEFAULT_COST = 0.0025  # per unit traded: 0.20% taker + ~0.05% spread and slippage
MIN_TRADE = 0.01  # skip weight changes smaller than 1% of the account (saves fees on tiny rebalances)


# --- data ------------------------------------------------------------------------------------------

MAX_GAP_DAYS = 3


def split_at_gaps(candles: dict[str, list[tuple]], max_gap_days: int = MAX_GAP_DAYS) -> dict[str, list[tuple]]:
    """A pair that stopped trading for days and came back (a relisting, a redenomination, a new token on the old
    ticker) becomes separate pairs, "X-USDT" then "X-USDT#2", so its price doesn't jump across the gap."""
    out = {}
    for pair, rows in candles.items():
        part, k = [], 1
        for r in rows:
            if part and r[0] - part[-1][0] > max_gap_days * DAY:
                out[pair if k == 1 else f"{pair}#{k}"] = part
                part, k = [], k + 1
            part.append(r)
        if part:
            out[pair if k == 1 else f"{pair}#{k}"] = part
    return out


class Panel:
    """Closes and volumes for many pairs on one calendar (None before a pair lists or on a missing bar), with cached
    indicators. Bars are daily unless `step` (seconds) says otherwise; lookbacks and `every` then count bars, and
    `days` holds each bar's open time."""

    def __init__(self, candles: dict[str, list[tuple]], step: int = DAY):
        self.step = step
        self.per_year = YEAR_DAYS * DAY / step
        candles = split_at_gaps({p: rows for p, rows in candles.items() if rows})
        first = min(rows[0][0] for rows in candles.values())
        last = max(rows[-1][0] for rows in candles.values())
        self.days = list(range(first, last + 1, step))
        index = {d: i for i, d in enumerate(self.days)}
        self.pairs = sorted(candles)
        self.close: dict[str, list[float | None]] = {}
        self.volume: dict[str, list[float]] = {}
        for pair, rows in candles.items():
            c, v = [None] * len(self.days), [0.0] * len(self.days)
            for ts, _o, _h, _l, close, vol in rows:
                if ts in index:
                    c[index[ts]], v[index[ts]] = close, vol
            for i in range(1, len(c)):  # a missing day inside the history repeats the previous close
                if c[i] is None and c[i - 1] is not None and i < index[rows[-1][0]]:
                    c[i] = c[i - 1]
            self.close[pair], self.volume[pair] = c, v
        self.listed = {p: next(i for i, x in enumerate(self.close[p]) if x is not None) for p in self.pairs}
        self._cache: dict = {}

    def index_of(self, ts: int) -> int:
        return max(0, min(len(self.days) - 1, (ts - self.days[0]) // self.step))

    def age(self, pair: str, t: int) -> int:
        """Days of history a pair has at day t."""
        return t - self.listed[pair] + 1 if self.close[pair][t] is not None else 0

    def _prefix(self, key, values):
        if key not in self._cache:
            acc, out = 0.0, [0.0]
            for x in values:
                acc += x or 0.0
                out.append(acc)
            self._cache[key] = out
        return self._cache[key]

    def sma(self, pair: str, t: int, n: int) -> float | None:
        if self.age(pair, t) < n:
            return None
        s = self._prefix(("c", pair), self.close[pair])
        return (s[t + 1] - s[t + 1 - n]) / n

    def avg_volume(self, pair: str, t: int, n: int) -> float:
        s = self._prefix(("v", pair), self.volume[pair])
        lo = max(0, t + 1 - n)
        return (s[t + 1] - s[lo]) / max(1, t + 1 - lo)

    def ret(self, pair: str, t: int, n: int) -> float | None:
        if self.age(pair, t) <= n:
            return None
        return self.close[pair][t] / self.close[pair][t - n] - 1

    def vol(self, pair: str, t: int, n: int = 60) -> float | None:
        """Annualized volatility of daily log returns over n days."""
        if self.age(pair, t) <= n:
            return None
        key = ("lr", pair)
        if key not in self._cache:
            c = self.close[pair]
            lr = [0.0] + [math.log(c[i] / c[i - 1]) if c[i] and c[i - 1] else 0.0 for i in range(1, len(c))]
            self._cache[key] = lr
            self._prefix(("lr1", pair), lr)
            self._prefix(("lr2", pair), [x * x for x in lr])
        s1, s2 = self._cache[("lr1", pair)], self._cache[("lr2", pair)]
        m = (s1[t + 1] - s1[t + 1 - n]) / n
        var = max(0.0, (s2[t + 1] - s2[t + 1 - n]) / n - m * m)
        return math.sqrt(var * self.per_year)

    def universe(self, t: int, top: int, min_age: int) -> list[str]:
        """The `top` most traded pairs (30-day average volume) with at least `min_age` days of history."""
        ok = [p for p in self.pairs if self.age(p, t) >= min_age]
        return sorted(ok, key=lambda p: -self.avg_volume(p, t, 30))[:top]


# --- strategies ------------------------------------------------------------------------------------

@dataclass
class Hold:
    """Buy and hold one pair (the benchmark)."""
    pair: str = "BTC-USDT"

    @property
    def name(self):
        return f"Hold {self.pair.split('-')[0]}"

    def weights(self, p: Panel, t: int) -> dict[str, float]:
        return {self.pair: 1.0} if p.close[self.pair][t] is not None else {}


@dataclass
class TrendFilter:
    """Hold one pair while it's above its n-day average, cash otherwise."""
    pair: str = "BTC-USDT"
    n: int = 100

    @property
    def name(self):
        return f"{self.pair.split('-')[0]} above its {self.n}-day average"

    def weights(self, p: Panel, t: int) -> dict[str, float]:
        m = p.sma(self.pair, t, self.n)
        return {self.pair: 1.0} if m is not None and p.close[self.pair][t] > m else {}


@dataclass
class TrendEnsemble:
    """Each pair gets an equal share of the account, held in proportion to how many of its `lookbacks`-day averages
    the price is above (all of them: fully in; none: that share sits in cash). Averaging several lookbacks, instead
    of picking whichever did best, keeps it from being fitted to the past. This is the trend bot's strategy.

    With `funding` ({pair: {day: average 8-hour funding rate}}): when a pair's funding averaged over the last
    `funding_days` days is at or below zero (futures traders are paying to bet against it), at least a third of its
    share is held anyway. Crowded bets against a coin tend to get squeezed. Research: research/sentiment_funding.py
    (chosen on 2019-10..2022, it also helped from 2023 on). Days without funding data give no signal."""
    pairs: tuple[str, ...] = ("BTC-USDT", "ETH-USDT")
    lookbacks: tuple[int, ...] = (50, 100, 150)
    funding: dict | None = field(default=None, repr=False, compare=False)
    funding_days: int = 3

    @property
    def name(self):
        extra = " + funding" if self.funding is not None else ""
        return f"Trend {'+'.join(x.split('-')[0] for x in self.pairs)} ({'/'.join(map(str, self.lookbacks))}-day){extra}"

    def avg_funding(self, p: Panel, pair: str, t: int) -> float | None:
        """Average funding over the last `funding_days` days up to and including day t (all known at its close)."""
        if not self.funding or pair not in self.funding:
            return None
        rates = [self.funding[pair].get(p.days[t] - i * DAY) for i in range(self.funding_days)]
        rates = [r for r in rates if r is not None]
        return sum(rates) / len(rates) if rates else None

    def strength(self, p: Panel, pair: str, t: int) -> float | None:
        price = p.close[pair][t]
        averages = [p.sma(pair, t, n) for n in self.lookbacks]
        if price is None or None in averages:
            return None
        return sum(price > m for m in averages) / len(averages)

    def weights(self, p: Panel, t: int) -> dict[str, float]:
        out = {}
        for pair in self.pairs:
            s = self.strength(p, pair, t) or 0.0
            f = self.avg_funding(p, pair, t)
            if f is not None and f <= 0 and self.strength(p, pair, t) is not None:
                s = max(s, 1 / 3)
            if s:
                out[pair] = s / len(self.pairs)
        return out


@dataclass
class TrendPortfolio:
    """Trend following across the most traded coins (time-series momentum, the best documented edge in crypto).

    Each coin in the universe gets a slot sized by inverse volatility, so calm and wild coins carry similar risk.
    The slot is filled by the coin's trend strength: the share of its 20/50/100-day averages the price is above.
    Coins in a downtrend sit in cash. With `btc_filter`, everything is halved while BTC is below its 100-day
    average."""
    top: int = 20
    lookbacks: tuple[int, ...] = (20, 50, 100)
    max_weight: float = 0.30
    btc_filter: bool = False

    @property
    def name(self):
        return f"Trend portfolio (top {self.top}{', BTC filter' if self.btc_filter else ''})"

    def weights(self, p: Panel, t: int) -> dict[str, float]:
        coins = p.universe(t, self.top, max(self.lookbacks) + 1)
        slots = {}
        for c in coins:
            v = p.vol(c, t, 60)
            if v:
                slots[c] = 1 / max(v, 0.2)
        if not slots:
            return {}
        total = sum(slots.values())
        out = {}
        for c, s in slots.items():
            price = p.close[c][t]
            strength = sum(price > p.sma(c, t, n) for n in self.lookbacks) / len(self.lookbacks)
            if strength > 0:
                out[c] = min(self.max_weight, strength * s / total)
        if self.btc_filter:
            m = p.sma("BTC-USDT", t, 100)
            if m is not None and p.close["BTC-USDT"][t] < m:
                out = {c: w / 2 for c, w in out.items()}
        return out


@dataclass
class Momentum:
    """Cross-sectional momentum: each week hold the `k` coins that rose most over 30 days, equal weights,
    only while BTC is above its 100-day average."""
    top: int = 20
    k: int = 5
    lookback: int = 30
    every: int = 7  # rebalance weekly

    @property
    def name(self):
        return f"Top {self.k} gainers weekly (of top {self.top})"

    def weights(self, p: Panel, t: int) -> dict[str, float]:
        m = p.sma("BTC-USDT", t, 100)
        if m is None or p.close["BTC-USDT"][t] < m:
            return {}
        coins = p.universe(t, self.top, self.lookback + 1)
        best = sorted(coins, key=lambda c: -(p.ret(c, t, self.lookback) or -1))[: self.k]
        return {c: 1 / self.k for c in best if (p.ret(c, t, self.lookback) or 0) > 0}


# --- engine -----------------------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    days: list[int] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    invested: list[float] = field(default_factory=list)
    turnover: float = 0.0
    costs: float = 0.0  # fees and slippage paid, as a share of the starting account (compounded away)
    entries: int = 0
    per_year: float = YEAR_DAYS  # bars per year, for annualizing

    def metrics(self) -> dict:
        eq = self.equity
        years = (self.days[-1] - self.days[0]) / DAY / YEAR_DAYS
        rets = [b / a - 1 for a, b in zip(eq, eq[1:])]
        peak, max_dd = eq[0], 0.0
        for x in eq:
            peak = max(peak, x)
            max_dd = min(max_dd, x / peak - 1)
        sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else -1.0
        return {
            "name": self.name, "total": eq[-1] / eq[0] - 1, "cagr": cagr, "max_drawdown": max_dd,
            "sharpe": statistics.fmean(rets) / sd * math.sqrt(self.per_year) if sd else 0.0,
            "vol": sd * math.sqrt(self.per_year), "invested": statistics.fmean(self.invested) if self.invested else 0.0,
            "turnover_per_year": self.turnover / years if years else 0.0, "entries": self.entries,
            "years": years, "by_year": self.by_year(),
        }

    def by_year(self) -> dict[int, float]:
        """Calendar-year returns, each from the previous year's last close."""
        out, base = {}, self.equity[0]
        for i, d in enumerate(self.days):
            y = time.gmtime(d).tm_year
            if i + 1 == len(self.days) or time.gmtime(self.days[i + 1]).tm_year != y:
                out[y] = self.equity[i] / base - 1
                base = self.equity[i]
        return out


def run(p: Panel, strategy, start: int, end: int | None = None, cost: float = DEFAULT_COST,
        every: int | None = None, min_trade: float = MIN_TRADE) -> Result:
    """Simulate `strategy` from day `start` to `end` (timestamps). Rebalances every `every` days (default: the
    strategy's own `every`, else daily)."""
    every = every or getattr(strategy, "every", 1)
    i0 = p.index_of(start)
    i1 = p.index_of(end) if end else len(p.days) - 1
    res = Result(strategy.name, per_year=p.per_year)
    equity, held = 1.0, {}
    res.days.append(p.days[i0])
    res.equity.append(equity)
    for t in range(i0, i1):
        if (t - i0) % every == 0:
            target = strategy.weights(p, t)
            new = dict(held)
            for c in set(held) | set(target):
                want, have = target.get(c, 0.0), held.get(c, 0.0)
                if abs(want - have) >= min_trade or (want == 0 and have > 0):
                    new[c] = want
                    if have == 0 and want > 0:
                        res.entries += 1
            new = {c: w for c, w in new.items() if w > 0}
            traded = sum(abs(new.get(c, 0.0) - held.get(c, 0.0)) for c in set(held) | set(new))
            res.turnover += traded
            fee = cost * traded
            res.costs += fee * equity
            equity *= 1 - fee
            held = new
        growth = 0.0
        moved = {}
        for c, w in held.items():
            a, b = p.close[c][t], p.close[c][t + 1]
            r = b / a - 1 if a and b else 0.0
            growth += w * r
            moved[c] = w * (1 + r)
        equity *= 1 + growth
        held = {c: x / (1 + growth) for c, x in moved.items()} if growth > -1 else {}
        res.days.append(p.days[t + 1])
        res.equity.append(equity)
        res.invested.append(sum(held.values()))
    return res


# The research set, all reported together (see the module docstring). On Binance data (dead coins included) the
# multi-coin ones lose to simply holding BTC; the BTC and ETH trend rules hold up across settings and fee levels.
STRATEGIES = [
    Hold("BTC-USDT"), Hold("ETH-USDT"),
    TrendFilter("BTC-USDT", 50), TrendFilter("BTC-USDT", 100), TrendFilter("BTC-USDT", 150), TrendFilter("BTC-USDT", 200),
    TrendEnsemble(("BTC-USDT",)), TrendEnsemble(("BTC-USDT", "ETH-USDT")),
    TrendPortfolio(top=10), TrendPortfolio(top=20), TrendPortfolio(top=20, btc_filter=True),
    Momentum(top=20, k=5),
]


def format_table(results: list[dict]) -> str:
    years = sorted({y for m in results for y in m["by_year"]})
    lines = [f"{'strategy':40} {'total':>9} {'a year':>7} {'worst':>6} {'Sharpe':>6} {'in':>4} "
             + " ".join(f"{y:>5}" for y in years)]
    for m in results:
        lines.append(f"{m['name'][:40]:40} {m['total'] * 100:+8.0f}% {m['cagr'] * 100:+6.1f}% {m['max_drawdown'] * 100:5.0f}% "
                     f"{m['sharpe']:6.2f} {m['invested'] * 100:3.0f}% "
                     + " ".join(f"{m['by_year'][y] * 100:+4.0f}%" if y in m["by_year"] else "    -" for y in years))
    return "\n".join(lines)
