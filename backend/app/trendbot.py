"""Trend bot: automatic trading of the backtested trend strategy, with demo money.

Strategy (backtest.TrendEnsemble): BTC and ETH each get half of the account, held in proportion to how many of their
50/100/150-day averages the price is above; the rest stays in USDT. In a downtrend the bot sits in cash. When a
coin's futures funding rate (3-day average, Binance) is at or below zero, at least a third of its half is held. Backtested
on daily prices since 2018 with fees, it made more than holding either coin with about half the worst drop
(see the lab and README), which doesn't promise the future.

The bot checks once a day, a few minutes after the daily candle closes (00:00 UTC), using OKX daily prices. It
trades a coin only when its holding is off target by 1% of the account or more. Each trade fills at the live OKX
price, minus or plus slippage, and pays the user's taker fee. Demo only: nothing here places real orders.
"""

import dataclasses
import json
import sqlite3
import time
from dataclasses import dataclass

from . import backtest

STRATEGY = backtest.TrendEnsemble(("BTC-USDT", "ETH-USDT"), (50, 100, 150))
PAIRS = STRATEGY.pairs
NAME = "Trend BTC+ETH (50/100/150-day, funding)"


def strategy(funding: dict | None) -> backtest.TrendEnsemble:
    """The bot's strategy with the funding data it should use."""
    return dataclasses.replace(STRATEGY, funding=funding)
COINS = tuple(p.split("-")[0] for p in PAIRS)
DAY = 86400
CHECK_AFTER_S = 5 * 60  # after 00:00 UTC, so OKX has published the finished daily candle
SNAPSHOT_S = 3600
MIN_TRADE = backtest.MIN_TRADE
BACKTEST_FROM = 1_535_760_000  # 2018-09-01: the first day with 150 days of OKX history behind it


class StaleData(Exception):
    """The daily prices needed for today's decision aren't in yet."""


@dataclass
class Trade:
    coin: str
    side: str  # buy | sell
    qty: float
    price: float  # fill price, slippage included
    value_usd: float  # qty * price
    fee_usd: float


def decision_day(now: int) -> int:
    """The day (00:00 UTC) whose check is the latest one due: today once the check time has passed, else yesterday."""
    day = now - now % DAY
    return day if now >= day + CHECK_AFTER_S else day - DAY


def signal(panel: backtest.Panel, day: int, funding: dict | None = None) -> dict:
    """Target weights for `day`, from the close of the day before, with the reasons behind them."""
    t = panel.index_of(day - DAY)
    if panel.days[t] != day - DAY:
        raise StaleData(f"no daily close for {time.strftime('%Y-%m-%d', time.gmtime(day - DAY))} yet")
    s = strategy(funding)
    weights, coins = s.weights(panel, t), {}
    for pair in PAIRS:
        coin = pair.split("-")[0]
        f = s.avg_funding(panel, pair, t)
        coins[coin] = {"price": panel.close[pair][t], "weight": weights.get(pair, 0.0),
                       "averages": {n: panel.sma(pair, t, n) for n in STRATEGY.lookbacks},
                       "funding": f, "funding_floor": f is not None and f <= 0,
                       "trend_weight": (s.strength(panel, pair, t) or 0.0) / len(PAIRS)}
    return {"day": day, "close_of": day - DAY, "weights": {c: v["weight"] for c, v in coins.items()}, "coins": coins}


# --- the demo account ---------------------------------------------------------------------------------

def account(conn: sqlite3.Connection, user_id: int):
    return conn.execute("SELECT * FROM bot_accounts WHERE user_id = ?", (user_id,)).fetchone()


def holdings(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    return {r["coin"]: {"qty": r["qty"], "cost": r["cost_usd"]}
            for r in conn.execute("SELECT * FROM bot_holdings WHERE user_id = ? AND qty > 0", (user_id,))}


def start(conn: sqlite3.Connection, user_id: int, balance: float, now: int, btc_price: float | None) -> None:
    """Open a fresh demo account; the first check runs right away."""
    with conn:
        _delete(conn, user_id)
        conn.execute("INSERT INTO bot_accounts (user_id, mode, enabled, start_balance, cash, started_at, btc_start) "
                     "VALUES (?, 'demo', 1, ?, ?, ?, ?)", (user_id, balance, balance, now, btc_price))


def set_enabled(conn: sqlite3.Connection, user_id: int, enabled: bool) -> None:
    with conn:
        conn.execute("UPDATE bot_accounts SET enabled = ? WHERE user_id = ?", (int(enabled), user_id))


def reset(conn: sqlite3.Connection, user_id: int) -> None:
    with conn:
        _delete(conn, user_id)


def _delete(conn: sqlite3.Connection, user_id: int) -> None:
    for table in ("bot_accounts", "bot_holdings", "bot_trades", "bot_snapshots"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))


def value(conn: sqlite3.Connection, user_id: int, prices: dict[str, float]) -> dict | None:
    """The account marked at `prices` ({coin: price}), with each holding's weight and result."""
    a = account(conn, user_id)
    if a is None:
        return None
    positions, invested = [], 0.0
    for coin, h in holdings(conn, user_id).items():
        px = prices.get(coin)
        worth = h["qty"] * px if px else h["cost"]
        invested += worth
        positions.append({"coin": coin, "qty": h["qty"], "price": px, "value": worth, "cost": h["cost"],
                          "pnl": worth - h["cost"]})
    total = a["cash"] + invested
    for x in positions:
        x["weight"] = x["value"] / total if total else 0.0
    btc = prices.get("BTC")
    return {
        "mode": a["mode"], "enabled": bool(a["enabled"]), "started_at": a["started_at"], "last_run_at": a["last_run_at"],
        "start_balance": a["start_balance"], "cash": a["cash"], "invested": invested, "total": total,
        "pnl": total - a["start_balance"], "pnl_pct": total / a["start_balance"] - 1 if a["start_balance"] else 0.0,
        "btc_return": btc / a["btc_start"] - 1 if btc and a["btc_start"] else None,
        "positions": sorted(positions, key=lambda x: -x["value"]),
        "targets": json.loads(a["targets"]) if a["targets"] else None,
    }


def plan(cash: float, held: dict[str, float], prices: dict[str, float], weights: dict[str, float],
         min_trade: float = MIN_TRADE) -> list[tuple[str, str, float]]:
    """Orders (coin, side, USD) that bring holdings (`held`: coin -> qty) to target `weights`, sells first.
    Changes under `min_trade` of the account are skipped, except closing a holding completely."""
    total = cash + sum(q * prices[c] for c, q in held.items())
    orders = []
    for coin in sorted(set(held) | set(weights)):
        have = held.get(coin, 0.0) * prices[coin]
        want = weights.get(coin, 0.0) * total
        delta = want - have
        if want == 0 and have > 0:
            orders.append((coin, "sell", have))
        elif abs(delta) >= min_trade * total:
            orders.append((coin, "buy" if delta > 0 else "sell", abs(delta)))
    return sorted(orders, key=lambda o: o[1] != "sell")


def rebalance(conn: sqlite3.Connection, user_id: int, weights: dict[str, float], prices: dict[str, float],
              fee_rate: float, slippage: float, now: int, reason: str = "daily check") -> list[Trade]:
    """Trade the demo account to `weights` at `prices`. Sells fill `slippage` below the price and buys above it;
    every trade pays `fee_rate` of its value."""
    a = account(conn, user_id)
    held = holdings(conn, user_id)
    cash = a["cash"]
    trades = []
    for coin, side, usd in plan(cash, {c: h["qty"] for c, h in held.items()}, prices, weights):
        h = held.setdefault(coin, {"qty": 0.0, "cost": 0.0})
        if side == "sell":
            qty = h["qty"] if usd >= h["qty"] * prices[coin] - 1e-9 else usd / prices[coin]
            fill = prices[coin] * (1 - slippage)
            gross = qty * fill
            fee = gross * fee_rate
            cash += gross - fee
            h["cost"] -= h["cost"] * (qty / h["qty"]) if h["qty"] else 0.0
            h["qty"] -= qty
        else:
            spend = min(usd, cash)
            if spend < 1:
                continue
            fill = prices[coin] * (1 + slippage)
            gross = spend / (1 + fee_rate)
            fee = spend - gross
            qty = gross / fill
            cash -= spend
            h["qty"] += qty
            h["cost"] += spend
        trades.append(Trade(coin, side, qty, fill, qty * fill, fee))
    with conn:
        for t in trades:
            conn.execute("INSERT INTO bot_trades (user_id, ts, coin, side, qty, price, value_usd, fee_usd, reason) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (user_id, now, t.coin, t.side, t.qty, t.price,
                                                                t.value_usd, t.fee_usd, reason))
        for coin, h in held.items():
            if h["qty"] > 1e-12:
                conn.execute("INSERT OR REPLACE INTO bot_holdings VALUES (?, ?, ?, ?)", (user_id, coin, h["qty"], h["cost"]))
            else:
                conn.execute("DELETE FROM bot_holdings WHERE user_id = ? AND coin = ?", (user_id, coin))
        conn.execute("UPDATE bot_accounts SET cash = ? WHERE user_id = ?", (cash, user_id))
    return trades


def due(conn: sqlite3.Connection, now: int) -> list[int]:
    """Users whose bot is on and hasn't run today's check."""
    day = decision_day(now)
    return [r[0] for r in conn.execute(
        "SELECT user_id FROM bot_accounts WHERE enabled = 1 AND (last_run_day IS NULL OR last_run_day < ?)", (day,))]


def run_due(conn: sqlite3.Connection, sig: dict, prices: dict[str, float], fee_for, slippage: float,
            now: int) -> dict[int, list[Trade]]:
    """Run today's check for every bot that's due. `sig` is signal() for decision_day(now)."""
    if sig["day"] != decision_day(now):
        raise StaleData("the signal isn't for today's check")
    if any(c not in prices for c in COINS):
        raise StaleData("no live price for every coin")
    out = {}
    for user_id in due(conn, now):
        out[user_id] = rebalance(conn, user_id, sig["weights"], prices, fee_for(user_id), slippage, now)
        with conn:
            conn.execute("UPDATE bot_accounts SET last_run_day = ?, last_run_at = ?, targets = ? WHERE user_id = ?",
                         (sig["day"], now, json.dumps(sig["weights"]), user_id))
    return out


def snapshot(conn: sqlite3.Connection, prices: dict[str, float], now: int, every: int = SNAPSHOT_S) -> None:
    """Record each account's value about once an hour (for the chart)."""
    for (user_id,) in conn.execute("SELECT user_id FROM bot_accounts").fetchall():
        last = conn.execute("SELECT MAX(ts) FROM bot_snapshots WHERE user_id = ?", (user_id,)).fetchone()[0]
        if last is not None and now - last < every:
            continue
        v = value(conn, user_id, prices)
        with conn:
            conn.execute("INSERT OR REPLACE INTO bot_snapshots VALUES (?, ?, ?, ?)",
                         (user_id, now, v["total"], prices.get("BTC")))


# --- the backtest shown with the bot -----------------------------------------------------------------

def backtest_summary(panel: backtest.Panel, cost: float = backtest.DEFAULT_COST, funding: dict | None = None) -> dict:
    """The bot's strategy and plain holding, on the same days, with weekly points for a chart."""
    bot = backtest.run(panel, strategy(funding), BACKTEST_FROM, cost=cost)
    holds = {c: backtest.run(panel, backtest.Hold(f"{c}-USDT"), BACKTEST_FROM, cost=cost) for c in COINS}
    step = 7
    points = [[d, round(bot.equity[i], 4), *(round(h.equity[i], 4) for h in holds.values())]
              for i, d in enumerate(bot.days) if i % step == 0 or i == len(bot.days) - 1]
    return {"from": bot.days[0], "to": bot.days[-1], "cost": cost, "strategy": bot.metrics(),
            "hold": {c: h.metrics() for c, h in holds.items()}, "columns": ["day", "bot", *COINS], "points": points}
