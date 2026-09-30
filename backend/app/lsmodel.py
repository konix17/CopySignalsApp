"""The long/short model: which of the most traded coins will beat the others over the next 3 days.

Research: research/daily_patterns.py and research/longshort_check.py (walk-forward on 2018-2024, then a holdout from
mid-2024 it never saw). Each day, 32 signals per coin (price trends, volatility, volume surges, buying pressure,
funding, futures activity, open interest) are ranked across the day's `TOP` most traded coins, plus 10 market-wide
ones (BTC trend, breadth, implied volatility). A gradient-boosting model learns how those predict each coin's move
over the next `HORIZON` days relative to the average coin. The paper test (longshort.py) buys the best-scored fifth
of the 25 most traded coins and shorts the worst fifth.

Everything here only uses data known at the day's close (00:00 UTC). The same code builds training data and today's
prediction, so the live model sees exactly what it was trained on.
"""

import pickle
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

DAY = 86400
TOP = 100  # coins ranked each day (by 30-day volume)
MIN_AGE = 90  # days of history a coin needs
HORIZON = 3  # days the model predicts
WINDOW = 320  # days of history enough to compute every signal for the latest day
MAX_ROWS = 300_000  # training rows (a random sample of all days x coins)
MIN_ROWS = 20_000  # fewer known outcomes than this: not enough to train on
ENSEMBLE = 5  # models averaged; one model's result swung from +54% to +107% a year with its random draw
COIN_FEATURES = [
    "ret_1d", "ret_3d", "ret_7d", "ret_14d", "ret_30d", "ret_90d", "ret_30d_skip7", "vol_7d", "vol_30d", "vol_ratio",
    "vs_avg20", "vs_avg50", "vs_avg200", "off_30d_high", "day_range", "close_in_range", "volume_surge_1d",
    "volume_surge_7d", "buy_pressure_1d", "buy_pressure_7d", "buy_pressure_change", "trades_surge", "trade_size_change",
    "funding_1d", "funding_7d", "funding_vs_30d", "futures_vs_spot_volume", "futures_buy_pressure", "oi_change_1d",
    "oi_change_7d", "oi_vs_volume", "size_rank",
]
MARKET_FEATURES = [
    "btc_ret_1d", "btc_ret_7d", "btc_vs_avg50", "btc_vs_avg200", "breadth_above_avg50", "market_ret_1d",
    "market_ret_7d", "btc_funding_7d", "btc_implied_vol", "btc_implied_vol_change",
]
FEATURES = COIN_FEATURES + MARKET_FEATURES


@dataclass
class Data:
    """Day x coin arrays (NaN where there's no data). `days` are 00:00 UTC timestamps of each daily candle."""
    days: np.ndarray
    coins: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray  # spot quote volume (USDT)
    taker_buy: np.ndarray  # quote volume bought by market orders
    trades: np.ndarray
    fut_volume: np.ndarray
    fut_taker_buy: np.ndarray
    funding: np.ndarray  # average funding rate per payment that day
    funding_day: np.ndarray  # what a position paid (long) or got (short) that day: rate x payments
    oi: np.ndarray  # open interest (coins), Bybit
    btc_dvol: np.ndarray = field(default_factory=lambda: np.array([]))  # Deribit BTC implied volatility, per day

    @property
    def T(self) -> int:
        return len(self.days)

    @property
    def N(self) -> int:
        return len(self.coins)


def load(conn: sqlite3.Connection, since: int | None = None, coins: list[str] | None = None) -> Data:
    """Arrays from the ls_* tables in the history database (lsdata.py), from day `since` on."""
    since = since or 0
    where = " AND coin IN (%s)" % ",".join("?" * len(coins)) if coins else ""
    args = [since, *(coins or [])]
    names = coins or [r[0] for r in conn.execute(
        f"SELECT DISTINCT coin FROM ls_spot WHERE day >= ?{where} ORDER BY coin", args)]
    lo, hi = conn.execute(f"SELECT MIN(day), MAX(day) FROM ls_spot WHERE day >= ?{where}", args).fetchone()
    if lo is None:
        raise ValueError("no market data yet")
    days = np.arange(lo, hi + DAY, DAY)
    di = {int(d): i for i, d in enumerate(days)}
    ci = {c: j for j, c in enumerate(names)}
    T, N = len(days), len(names)

    def grid():
        return np.full((T, N), np.nan)

    o, h, lw, c, v, tb, tr, fv, ftb, fr, fd, oi = (grid() for _ in range(12))
    for coin, day, *vals in conn.execute(
            f"SELECT coin, day, open, high, low, close, quote_vol, taker_buy_quote, trades FROM ls_spot "
            f"WHERE day >= ?{where}", args):
        if coin in ci and day in di:
            i, j = di[day], ci[coin]
            o[i, j], h[i, j], lw[i, j], c[i, j], v[i, j], tb[i, j], tr[i, j] = vals
    for coin, day, vol, buy in conn.execute(
            f"SELECT coin, day, quote_vol, taker_buy_quote FROM ls_fut WHERE day >= ?{where}", args):
        if coin in ci and day in di:
            fv[di[day], ci[coin]], ftb[di[day], ci[coin]] = vol, buy
    for coin, day, rate, n in conn.execute(f"SELECT coin, day, rate, n FROM ls_funding WHERE day >= ?{where}", args):
        if coin in ci and day in di:
            fr[di[day], ci[coin]], fd[di[day], ci[coin]] = rate, rate * n
    for coin, day, x in conn.execute(f"SELECT coin, day, oi FROM ls_oi WHERE day >= ?{where}", args):
        if coin in ci and day in di:
            oi[di[day], ci[coin]] = x
    dvol = np.full(T, np.nan)
    for day, x in conn.execute("SELECT day, close FROM ls_dvol WHERE coin = 'BTC' AND day >= ?", (since,)):
        if day in di:
            dvol[di[day]] = x
    return Data(days, names, o, h, lw, c, v, tb, tr, fv, ftb, fr, fd, oi, dvol)


# --- rolling helpers (NaN-aware) ------------------------------------------------------------------------

def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    if k > 0:
        out[k:] = a[:-k]
    elif k < 0:
        out[:k] = a[-k:]
    else:
        out[:] = a
    return out


def _roll_mean(a: np.ndarray, n: int) -> np.ndarray:
    """Mean over the last n days, needing values on 2/3 of them."""
    x = np.nan_to_num(a)
    pad = np.zeros((1,) + a.shape[1:])
    s = np.vstack([pad, np.cumsum(x, axis=0)])
    k = np.vstack([pad, np.cumsum(~np.isnan(a), axis=0, dtype=float)])
    out = np.full(a.shape, np.nan)
    if len(a) >= n:
        tot, cnt = s[n:] - s[:-n], k[n:] - k[:-n]
        out[n - 1:] = np.where(cnt >= max(1, 2 * n // 3), tot / np.maximum(cnt, 1), np.nan)
    return out


def _roll_std(a: np.ndarray, n: int) -> np.ndarray:
    m = _roll_mean(a, n)
    return np.sqrt(np.maximum(_roll_mean(a * a, n) - m * m, 0))


def _roll_max(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(a.shape, np.nan)
    for i in range(n - 1, len(a)):
        w = a[i - n + 1:i + 1]
        ok = ~np.isnan(w).all(axis=0)
        out[i, ok] = np.nanmax(w[:, ok], axis=0)
    return out


def _nanmean_rows(a: np.ndarray, mask: np.ndarray) -> np.ndarray:
    x = np.where(mask, a, np.nan)
    n = (~np.isnan(x)).sum(axis=1)
    return np.where(n > 0, np.nansum(x, axis=1) / np.maximum(n, 1), np.nan)


# --- universe, signals, target ------------------------------------------------------------------------

def universe(d: Data, top: int = TOP) -> tuple[np.ndarray, np.ndarray]:
    """(univ, vol30): each day the `top` coins by 30-day average volume with MIN_AGE days of history."""
    age = np.cumsum(~np.isnan(d.close), axis=0)
    vol30 = _roll_mean(d.volume, 30)
    univ = np.zeros((d.T, d.N), bool)
    for i in range(d.T):
        ok = (age[i] >= MIN_AGE) & ~np.isnan(d.close[i]) & ~np.isnan(vol30[i])
        idx = np.where(ok)[0]
        if len(idx):
            univ[i, idx[np.argsort(-vol30[i, idx])[:top]]] = True
    return univ, vol30


def signals(d: Data, univ: np.ndarray, vol30: np.ndarray) -> tuple[dict, dict]:
    """(coin signals {name: T x N}, market signals {name: T}). Divisions by zero become missing values."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return _signals(d, univ, vol30)


def _signals(d: Data, univ: np.ndarray, vol30: np.ndarray) -> tuple[dict, dict]:
    C, V = d.close, d.volume
    lr = np.log(C / _shift(C, 1))
    F = {
        "ret_1d": C / _shift(C, 1) - 1, "ret_3d": C / _shift(C, 3) - 1, "ret_7d": C / _shift(C, 7) - 1,
        "ret_14d": C / _shift(C, 14) - 1, "ret_30d": C / _shift(C, 30) - 1, "ret_90d": C / _shift(C, 90) - 1,
        "ret_30d_skip7": _shift(C, 7) / _shift(C, 30) - 1,
        "vol_7d": _roll_std(lr, 7), "vol_30d": _roll_std(lr, 30),
        "vs_avg20": C / _roll_mean(C, 20) - 1, "vs_avg50": C / _roll_mean(C, 50) - 1,
        "vs_avg200": C / _roll_mean(C, 200) - 1, "off_30d_high": C / _roll_max(d.high, 30) - 1,
        "day_range": (d.high - d.low) / C, "close_in_range": (C - d.low) / (d.high - d.low),
        "volume_surge_1d": V / vol30, "volume_surge_7d": _roll_mean(V, 7) / vol30,
        "buy_pressure_1d": d.taker_buy / V,
        "buy_pressure_7d": _roll_mean(d.taker_buy, 7) / _roll_mean(V, 7),
        "trades_surge": d.trades / _roll_mean(d.trades, 30),
        "trade_size_change": (V / d.trades) / _roll_mean(V / d.trades, 30),
        "funding_1d": d.funding, "funding_7d": _roll_mean(d.funding, 7),
        "funding_vs_30d": (d.funding - _roll_mean(d.funding, 30)) / _roll_std(d.funding, 30),
        "futures_vs_spot_volume": d.fut_volume / V, "futures_buy_pressure": d.fut_taker_buy / d.fut_volume,
        "oi_change_1d": d.oi / _shift(d.oi, 1) - 1, "oi_change_7d": d.oi / _shift(d.oi, 7) - 1,
        "oi_vs_volume": d.oi * C / _roll_mean(d.fut_volume, 7), "size_rank": -vol30,
    }
    F["vol_ratio"] = F["vol_7d"] / F["vol_30d"]
    F["buy_pressure_change"] = F["buy_pressure_1d"] - _roll_mean(d.taker_buy / V, 30)
    for k in F:
        F[k] = np.where(np.isfinite(F[k]), F[k], np.nan)
    b = d.coins.index("BTC")
    dvol = d.btc_dvol if len(d.btc_dvol) == d.T else np.full(d.T, np.nan)
    M = {
        "btc_ret_1d": F["ret_1d"][:, b], "btc_ret_7d": F["ret_7d"][:, b],
        "btc_vs_avg50": F["vs_avg50"][:, b], "btc_vs_avg200": F["vs_avg200"][:, b],
        "breadth_above_avg50": _nanmean_rows((F["vs_avg50"] > 0).astype(float), univ & ~np.isnan(F["vs_avg50"])),
        "market_ret_1d": _nanmean_rows(F["ret_1d"], univ), "market_ret_7d": _nanmean_rows(F["ret_7d"], univ),
        "btc_funding_7d": F["funding_7d"][:, b], "btc_implied_vol": dvol,
        "btc_implied_vol_change": dvol / _shift(dvol[:, None], 7)[:, 0] - 1,
    }
    return F, M


def _rank_rows(a: np.ndarray, univ: np.ndarray) -> np.ndarray:
    """Each day, a coin's place among that day's universe (0 = lowest, 1 = highest)."""
    out = np.full(a.shape, np.nan)
    for i in range(a.shape[0]):
        ok = univ[i] & ~np.isnan(a[i])
        n = ok.sum()
        if n > 1:
            out[i, ok] = a[i, ok].argsort().argsort() / (n - 1)
    return out


def matrix(d: Data, univ: np.ndarray, vol30: np.ndarray, rows: np.ndarray | None = None) -> tuple:
    """(X, (ii, jj)): one row per universe coin-day (or only the days in `rows`), columns = FEATURES."""
    F, M = signals(d, univ, vol30)
    mask = univ.copy()
    if rows is not None:
        keep = np.zeros(d.T, bool)
        keep[rows] = True
        mask &= keep[:, None]
    ii, jj = np.where(mask)
    X = np.empty((len(ii), len(FEATURES)), dtype=np.float32)
    for k, name in enumerate(COIN_FEATURES):
        X[:, k] = _rank_rows(F[name], univ)[ii, jj]
    for k, name in enumerate(MARKET_FEATURES, start=len(COIN_FEATURES)):
        X[:, k] = M[name][ii]
    return X, (ii, jj)


def excess_return(d: Data, univ: np.ndarray, horizon: int = HORIZON) -> np.ndarray:
    """Each coin's move over the next `horizon` days minus the universe average (the model's target)."""
    fwd = _shift(d.close, -horizon) / d.close - 1
    return fwd - _nanmean_rows(fwd, univ)[:, None]


def subset(d: Data, keep: np.ndarray) -> Data:
    """The same data for the coins where `keep` (bool per coin) is true."""
    cols = np.where(keep)[0]
    arrays = {f: getattr(d, f)[:, cols] for f in ("open", "high", "low", "close", "volume", "taker_buy", "trades",
                                                  "fut_volume", "fut_taker_buy", "funding", "funding_day", "oi")}
    return Data(d.days, [d.coins[j] for j in cols], **arrays, btc_dvol=d.btc_dvol)


def ranked_coins_only(d: Data) -> Data:
    """Drop coins that were never in the universe (they can't affect any signal or target), to save memory."""
    univ, _ = universe(d)
    keep = univ.any(axis=0)
    keep[d.coins.index("BTC")] = True
    return subset(d, keep)


# --- the model ---------------------------------------------------------------------------------------------

@dataclass
class Model:
    """Several gradient-boosting models, each trained on its own random draw of the rows; their predictions are
    averaged (research/longshort_check.py: a single model's backtest ranged from +54% to +107% a year by seed)."""
    estimators: list  # [(estimator, columns used)] (features with no data in training are left out)
    trained_through: int  # the last day (00:00 UTC) whose outcome was known in training
    rows: int

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.mean([est.predict(X[:, cols]) for est, cols in self.estimators], axis=0)


@dataclass
class TrainingSet:
    """Every universe coin-day with its signals (X) and outcome (y, NaN until it's known)."""
    data: Data
    univ: np.ndarray
    vol30: np.ndarray
    X: np.ndarray
    ii: np.ndarray  # day index of each row
    jj: np.ndarray  # coin index of each row
    y: np.ndarray


def training_set(d: Data) -> TrainingSet:
    d = ranked_coins_only(d)
    univ, vol30 = universe(d)
    X, (ii, jj) = matrix(d, univ, vol30)
    return TrainingSet(d, univ, vol30, X, ii, jj, excess_return(d, univ)[ii, jj])


def fit(ts: TrainingSet, last: int, n_models: int = ENSEMBLE) -> Model:
    """Fit on the rows whose outcome was known by day index `last` + HORIZON (so none looks into the future).
    Each of the `n_models` gets its own random draw (and order) of those rows, which also changes the part held
    back to decide when to stop training."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    ok = (ts.ii <= last) & ~np.isnan(ts.y)
    X_all, y_all = ts.X[ok], ts.y[ok]
    if len(y_all) < MIN_ROWS:
        raise ValueError(f"not enough history to train ({len(y_all)} rows)")
    estimators = []
    for seed in range(n_models):
        pick = np.random.default_rng(seed).choice(len(y_all), min(MAX_ROWS, len(y_all)), replace=False)
        X, y = X_all[pick], np.clip(y_all[pick], -0.5, 0.5)
        cols = [c for c in range(X.shape[1]) if len(np.unique(X[~np.isnan(X[:, c]), c])) >= 3]
        est = HistGradientBoostingRegressor(max_iter=150, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=500,
                                            l2_regularization=1.0, random_state=0).fit(X[:, cols], y)
        estimators.append((est, cols))
    return Model(estimators, int(ts.data.days[last]), min(MAX_ROWS, len(y_all)))


def train(d: Data, until: int | None = None, n_models: int = ENSEMBLE) -> Model:
    """Fit on every universe coin-day whose `HORIZON`-day outcome is known before day `until` (default: now)."""
    ts = training_set(d)
    last = ts.data.T - 1 - HORIZON if until is None else int(np.searchsorted(ts.data.days, until)) - 1 - HORIZON
    return fit(ts, last, n_models)


def walk_forward(d: Data, start: int, every_days: int = 91, n_models: int = ENSEMBLE, progress=None) -> dict:
    """Backtest of the model and the paper book together, the honest way: from day `start`, a model is trained every
    `every_days` on the data before it and used for the days after; the book trades exactly like longshort.py
    (same picks, fees and funding) at daily closes. Returns daily results and a summary."""
    from . import longshort

    ts = training_set(d)
    d = ts.data
    pred = np.full((d.T, d.N), np.nan)
    i0 = int(np.searchsorted(d.days, start))
    for q in range(i0, d.T, every_days):
        model = fit(ts, q - 1 - HORIZON, n_models)
        rows = (ts.ii >= q) & (ts.ii < q + every_days)
        if rows.any():
            pred[ts.ii[rows], ts.jj[rows]] = model.predict(ts.X[rows])
        if progress:
            progress(q, d.T)
    btc = d.coins.index("BTC")
    w = {}  # coin index -> weight (share of the account, negative = short)
    days, rets, turnover, btc_rets = [], [], [], []
    for i in range(i0, d.T - 1):
        ok = ts.univ[i] & ~np.isnan(pred[i]) & ~np.isnan(d.funding[i]) & ~np.isnan(d.close[i + 1])
        idx = np.where(ok)[0]
        idx = idx[np.argsort(-ts.vol30[i, idx])][:longshort.TOP_COINS]
        ranked = [d.coins[j] for j in idx[np.argsort(-pred[i, idx])]]
        held_l = {d.coins[j] for j, x in w.items() if x > 0}
        held_s = {d.coins[j] for j, x in w.items() if x < 0}
        longs, shorts = longshort.pick(ranked, held_l, held_s)
        new = {d.coins.index(c): 0.5 / len(longs) for c in longs} | {d.coins.index(c): -0.5 / len(shorts) for c in shorts}
        traded = sum(abs(new.get(j, 0) - w.get(j, 0)) for j in set(w) | set(new))
        r = sum(x * (d.close[i + 1, j] / d.close[i, j] - 1) for j, x in new.items())
        fund = sum(x * np.nan_to_num(d.funding_day[i + 1, j]) for j, x in new.items())
        rets.append(r - fund - longshort.COST * traded)
        turnover.append(traded)
        days.append(int(d.days[i + 1]))
        btc_rets.append(d.close[i + 1, btc] / d.close[i, btc] - 1)
        grow = {j: x * (d.close[i + 1, j] / d.close[i, j]) for j, x in new.items()}
        total = 1 + r
        w = {j: x / total for j, x in grow.items()} if total > 0 else {}
    return summarize(days, rets, btc_rets, turnover)


def summarize(days: list[int], rets: list[float], btc_rets: list[float], turnover: list[float]) -> dict:
    import time

    x = np.array(rets)
    eq = np.cumprod(1 + x)
    btc = np.cumprod(1 + np.array(btc_rets))
    years = len(x) / 365
    by_year: dict[str, float] = {}
    for y in sorted({time.gmtime(t).tm_year for t in days}):
        m = np.array([time.gmtime(t).tm_year == y for t in days])
        by_year[str(y)] = float(np.prod(1 + x[m]) - 1)
    return {
        "from": days[0], "to": days[-1], "days": len(x),
        "cagr": float(eq[-1] ** (1 / years) - 1) if years > 0 else None,
        "max_drawdown": float((eq / np.maximum.accumulate(eq) - 1).min()),
        "sharpe": float(x.mean() / x.std() * np.sqrt(365)) if x.std() else None,
        "btc_cagr": float(btc[-1] ** (1 / years) - 1) if years > 0 else None,
        "btc_max_drawdown": float((btc / np.maximum.accumulate(btc) - 1).min()),
        "turnover_per_day": float(np.mean(turnover)), "win_days": float((x > 0).mean()), "by_year": by_year,
        "points": [[days[i], round(float(eq[i]), 5), round(float(btc[i]), 5)] for i in range(0, len(x), 7)]
                  + [[days[-1], round(float(eq[-1]), 5), round(float(btc[-1]), 5)]],
    }


def scores_for_day(model: Model, d: Data, i: int | None = None) -> dict[str, float]:
    """Predicted excess return per universe coin for day index `i` (default: the latest day)."""
    i = d.T - 1 if i is None else i
    univ, vol30 = universe(d)
    X, (ii, jj) = matrix(d, univ, vol30, rows=np.array([i]))
    if not len(ii):
        return {}
    pred = model.predict(X)
    return {d.coins[j]: float(p) for j, p in zip(jj, pred)}


def save(model: Model, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(model))
    tmp.replace(path)


def load_model(path: Path) -> Model | None:
    """The model saved by `save` (the app's own file in data/, never a downloaded one). None if missing or from an
    older version of this code (it's then retrained)."""
    if not path.exists():
        return None
    try:
        model = pickle.loads(path.read_bytes())
    except Exception:
        return None
    return model if isinstance(model, Model) and hasattr(model, "estimators") else None
