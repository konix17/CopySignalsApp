"""Long/short paper test: every day, buy the coins the model (lsmodel.py) likes best and short the ones it likes least.

Among the 25 most traded coins that have a USDT perpetual, the best-scored fifth is bought and the worst-scored fifth
is shorted, with half the account on each side, so a market that rises or falls as a whole doesn't move the result.
A held coin stays while it's still in the best (or worst) 1.5 fifths, which saves fees. It runs once a day, after the
00:00 UTC close, at live prices. Every trade pays `COST` of its value (futures taker fee plus slippage) and positions
pay (long) or receive (short) the day's funding.

Paper money only: one shared account, nothing is sent to an exchange. The point is to see whether live results match
the backtest (research/longshort_check.py) before any real money.
"""

import json
import sqlite3
from dataclasses import dataclass

DAY = 86400
TOP_COINS = 25
FRACTION = 0.2  # of the eligible coins on each side
KEEP = 1.5  # a held coin stays while it's within this many fifths of its end
MIN_COINS = 10  # fewer eligible coins than this (e.g. an exchange outage): hold nothing
COST = 0.001  # per unit traded: 0.05% OKX futures taker fee + 0.05% slippage
RUN_AFTER_S = 15 * 60  # run 15 minutes after 00:00 UTC, when the day's data is published
DEFAULT_BALANCE = 10_000.0
SNAPSHOT_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS ls_account (
    id INTEGER PRIMARY KEY CHECK (id = 1), start_balance REAL NOT NULL, cash REAL NOT NULL, started_at INTEGER NOT NULL,
    btc_start REAL, last_run_day INTEGER, last_run_at INTEGER
);
CREATE TABLE IF NOT EXISTS ls_positions (coin TEXT PRIMARY KEY, qty REAL NOT NULL, entry_price REAL NOT NULL,
    opened_day INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ls_trades (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, day INTEGER NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, value_usd REAL NOT NULL, fee_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ls_days (
    day INTEGER PRIMARY KEY, ts INTEGER NOT NULL, equity REAL NOT NULL, funding_usd REAL NOT NULL, fees_usd REAL NOT NULL,
    longs TEXT NOT NULL, shorts TEXT NOT NULL, ranking TEXT NOT NULL, btc_price REAL
);
CREATE TABLE IF NOT EXISTS ls_snapshots (ts INTEGER PRIMARY KEY, equity REAL NOT NULL, btc_price REAL);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def pick(ranked: list[str], held_long: set[str], held_short: set[str], fraction: float = FRACTION,
         keep: float = KEEP) -> tuple[list[str], list[str]]:
    """Longs and shorts from `ranked` (eligible coins, best first). Held coins stay while they're within `keep`
    fifths of their end; the rest of each side is filled from the top (or bottom) of the ranking."""
    if len(ranked) < MIN_COINS:
        return [], []
    k = max(2, int(len(ranked) * fraction))
    wide = int(k * keep)
    top, bottom = ranked[:wide], ranked[::-1][:wide]
    longs = [c for c in ranked if c in held_long and c in top][:k]
    longs += [c for c in ranked if c not in longs and c not in bottom[:k]][: k - len(longs)]
    shorts = [c for c in ranked[::-1] if c in held_short and c in bottom and c not in longs][:k]
    shorts += [c for c in ranked[::-1] if c not in shorts and c not in longs][: k - len(shorts)]
    return longs, shorts


def eligible(scores: dict[str, float], volume30: dict[str, float], has_perp: set[str], prices: dict[str, float],
             top: int = TOP_COINS) -> list[str]:
    """The `top` most traded scored coins that can be shorted (a perpetual exists) and have a live price,
    best score first."""
    ok = [c for c in scores if c in has_perp and c in prices and volume30.get(c)]
    ok = sorted(ok, key=lambda c: -volume30[c])[:top]
    return sorted(ok, key=lambda c: -scores[c])


# --- the account ----------------------------------------------------------------------------------------

def account(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM ls_account WHERE id = 1").fetchone()


def positions(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["coin"]: dict(r) for r in conn.execute("SELECT * FROM ls_positions")}


def start(conn: sqlite3.Connection, balance: float, now: int, btc_price: float | None) -> None:
    with conn:
        _delete(conn)
        conn.execute("INSERT INTO ls_account (id, start_balance, cash, started_at, btc_start) VALUES (1, ?, ?, ?, ?)",
                     (balance, balance, now, btc_price))


def reset(conn: sqlite3.Connection) -> None:
    with conn:
        _delete(conn)


def _delete(conn: sqlite3.Connection) -> None:
    for t in ("ls_account", "ls_positions", "ls_trades", "ls_days", "ls_snapshots"):
        conn.execute(f"DELETE FROM {t}")


def value(conn: sqlite3.Connection, prices: dict[str, float]) -> dict | None:
    """The account marked at `prices`. Shorts are negative quantities: cash holds what selling them brought in."""
    a = account(conn)
    if a is None:
        return None
    pos = []
    for coin, p in positions(conn).items():
        px = prices.get(coin) or p["entry_price"]
        pnl = p["qty"] * (px - p["entry_price"])
        pos.append({"coin": coin, "side": "long" if p["qty"] > 0 else "short", "qty": abs(p["qty"]),
                    "entry_price": p["entry_price"], "price": px, "value": abs(p["qty"]) * px, "pnl": pnl,
                    "pnl_pct": pnl / (abs(p["qty"]) * p["entry_price"]), "opened_day": p["opened_day"],
                    "live": coin in prices})
    equity = a["cash"] + sum((x["value"] if x["side"] == "long" else -x["value"]) for x in pos)
    btc = prices.get("BTC")
    return {
        "start_balance": a["start_balance"], "equity": equity, "pnl": equity - a["start_balance"],
        "pnl_pct": equity / a["start_balance"] - 1, "started_at": a["started_at"], "last_run_day": a["last_run_day"],
        "last_run_at": a["last_run_at"], "btc_return": btc / a["btc_start"] - 1 if btc and a["btc_start"] else None,
        "long_value": sum(x["value"] for x in pos if x["side"] == "long"),
        "short_value": sum(x["value"] for x in pos if x["side"] == "short"),
        "positions": sorted(pos, key=lambda x: (x["side"], -x["value"])),
    }


@dataclass
class Trade:
    coin: str
    side: str  # buy | sell
    qty: float
    price: float
    value_usd: float
    fee_usd: float


def rebalance(conn: sqlite3.Connection, day: int, ranked: list[str], scores: dict[str, float],
              prices: dict[str, float], funding_day: dict[str, float], now: int, cost: float = COST) -> dict:
    """Run the day's check for `day` (the 00:00 UTC day that just closed). First the funding of the positions held
    over that day is settled, then the book is moved to the new longs and shorts at `prices`."""
    a = account(conn)
    held = positions(conn)
    cash = a["cash"]
    funding = 0.0
    for coin, p in held.items():  # held through `day`: longs pay the day's funding, shorts receive it
        rate = funding_day.get(coin) or 0.0
        px = prices.get(coin) or p["entry_price"]
        funding += p["qty"] * px * rate
    cash -= funding
    marks = {c: prices.get(c) or p["entry_price"] for c, p in held.items()}
    equity = cash + sum(p["qty"] * marks[c] for c, p in held.items())
    longs, shorts = pick(ranked, {c for c, p in held.items() if p["qty"] > 0},
                         {c for c, p in held.items() if p["qty"] < 0})
    target = {}
    if longs and shorts and equity > 0:
        for c in longs:
            target[c] = 0.5 * equity / len(longs) / prices[c]
        for c in shorts:
            target[c] = -0.5 * equity / len(shorts) / prices[c]
    trades = []
    for coin in sorted(set(held) | set(target)):
        have = held.get(coin, {}).get("qty", 0.0)
        want = target.get(coin, 0.0)
        delta = want - have
        px = prices.get(coin) or marks.get(coin)
        if not px or abs(delta) * px < 0.5:  # nothing to do (or less than 50 cents)
            continue
        fee = abs(delta) * px * cost
        cash -= delta * px + fee
        trades.append(Trade(coin, "buy" if delta > 0 else "sell", abs(delta), px, abs(delta) * px, fee))
    with conn:
        for t in trades:
            conn.execute("INSERT INTO ls_trades (ts, day, coin, side, qty, price, value_usd, fee_usd) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (now, day, t.coin, t.side, t.qty, t.price, t.value_usd,
                                                             t.fee_usd))
        for coin in set(held) - set(target):
            conn.execute("DELETE FROM ls_positions WHERE coin = ?", (coin,))
        for coin, q in target.items():
            old = held.get(coin)
            if old and (old["qty"] > 0) == (q > 0):  # same side: keep the original entry price and day
                conn.execute("UPDATE ls_positions SET qty = ? WHERE coin = ?", (q, coin))
            else:
                conn.execute("INSERT OR REPLACE INTO ls_positions VALUES (?, ?, ?, ?)", (coin, q, prices[coin], day))
        conn.execute("UPDATE ls_account SET cash = ?, last_run_day = ?, last_run_at = ? WHERE id = 1", (cash, day, now))
        fees = sum(t.fee_usd for t in trades)
        after = cash + sum(q * prices[c] for c, q in target.items())
        conn.execute("INSERT OR REPLACE INTO ls_days VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (day, now, after, funding, fees, json.dumps(longs), json.dumps(shorts),
                      json.dumps([[c, round(scores[c], 5)] for c in ranked]), prices.get("BTC")))
    return {"day": day, "longs": longs, "shorts": shorts, "trades": trades, "funding_usd": funding,
            "fees_usd": sum(t.fee_usd for t in trades), "equity": equity}


def snapshot(conn: sqlite3.Connection, prices: dict[str, float], now: int, every: int = SNAPSHOT_S) -> bool:
    """Record the account's value about once an hour (for the chart)."""
    v = value(conn, prices)
    if v is None:
        return False
    last = conn.execute("SELECT MAX(ts) FROM ls_snapshots").fetchone()[0]
    if last is not None and now - last < every:
        return False
    with conn:
        conn.execute("INSERT OR REPLACE INTO ls_snapshots VALUES (?, ?, ?)", (now, v["equity"], prices.get("BTC")))
    return True


def decision_day(now: int) -> int:
    """The day (00:00 UTC) whose close the next run uses: yesterday once today's run time has passed."""
    today = now - now % DAY
    return today - DAY if now >= today + RUN_AFTER_S else today - 2 * DAY


def due(conn: sqlite3.Connection, now: int) -> bool:
    a = account(conn)
    return a is not None and (a["last_run_day"] is None or a["last_run_day"] < decision_day(now))
