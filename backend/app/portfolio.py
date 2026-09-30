"""The owner's positions: sell advice and alerts.

Every refresh, each open position is checked against what the top traders
are doing and against the price trend. Advice is HOLD, WATCH, SELL or
TAKE_PROFIT. Each reason fires one alert per position (unique by position + kind).
The same exit rules drive the paper track record (tracker.py).
"""

import json
import sqlite3
from dataclasses import dataclass

from . import activity
from .market import Market
from .models import Flow, Pick, Signal

SEVERITY = {"HOLD": 0, "WATCH": 1, "TAKE_PROFIT": 2, "SELL": 3}
EXIT_KINDS = {"flipped", "exited", "selling", "trend", "pump_fading", "pump_dump", "trader_closed"}  # close paper/demo trades
COPY_IGNORES = {"flipped", "exited", "selling", "selling_watch", "trend", "trend_watch"}  # copies follow one trader instead
EXIT_REASON = {"stop": "stop-loss hit", "target": "target reached", "time": "hold time over",
               "trend": "uptrend broke", "exited": "top traders left", "flipped": "top traders turned against it",
               "selling": "top traders sold", "pump_fading": "pump topped out", "pump_dump": "pump dumping",
               "trader_closed": "the copied trader sold"}
RISKY_STYLES = ("early", "pump")
STYLE_OF = {"Early": "early", "Pump": "pump", "Copy": "copy"}  # pick strength -> trade style
DEFAULT_STOP, DEFAULT_TARGET, DEFAULT_HOLD_DAYS = 0.10, 0.20, 7


@dataclass
class Finding:
    kind: str
    advice: str
    message: str


def field_of(pos, name: str, default=None):
    try:
        value = pos[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def effective_stop(pos) -> float | None:
    """The fixed stop, raised by the trailing stop once the price has climbed."""
    stop = pos["stop_price"]
    trail, peak = field_of(pos, "trail_pct"), field_of(pos, "peak_price")
    if trail and peak:
        stop = max(stop or 0, peak * (1 - trail))
    return stop


def with_peak(pos, price: float) -> dict:
    """Row as a dict with the highest price since buying updated (trailing stops follow it)."""
    d = dict(pos)
    if d.get("trail_pct"):
        d["peak_price"] = max(d.get("peak_price") or d["entry_price"], price)
    return d


def evaluate(
    pos,
    price: float,
    signal: Signal | None,
    flow: Flow | None,
    market: Market | None,
    now: int,
    copy_holding: bool | None = None,
) -> list[Finding]:
    """`pos` needs direction, stop_price, target_price, traders_at_entry, hold_until. Swing copies (style "copy")
    ignore the crowd and trend rules and sell when the copied trader does: `copy_holding` False
    (swing.trader_still_holding)."""
    long = pos["direction"] == "long"
    out: list[Finding] = []

    stop, target = effective_stop(pos), pos["target_price"]
    if stop and (price <= stop if long else price >= stop):
        trailing = stop > (pos["stop_price"] or 0)
        out.append(Finding("stop", "SELL", f"{'Trailing stop' if trailing else 'Stop-loss'} hit "
                                           f"({price:.6g}, stop was {stop:.6g})"))
    if target and (price >= target if long else price <= target):
        out.append(Finding("target", "TAKE_PROFIT", f"Target reached ({price:.6g}, target was {target:.6g})"))

    at_entry = pos["traders_at_entry"] or 0
    if signal is None:
        holders = 0
    elif signal.direction == pos["direction"]:
        holders = signal.n_traders
    else:
        holders = signal.n_opposing
        if signal.agreement >= 0.6:
            out.append(Finding("flipped", "SELL",
                               f"Top traders now bet against it ({signal.n_traders} vs {holders})"))
    if at_entry and holders <= at_entry * 0.5:
        out.append(Finding("exited", "SELL",
                           f"Most top traders have left: {holders} hold it now vs {at_entry} when it was picked"))

    if flow and flow.sellers >= 2 and flow.sellers > flow.buyers:
        strong = flow.sellers >= max(2, 0.3 * at_entry)
        out.append(Finding("selling" if strong else "selling_watch", "SELL" if strong else "WATCH",
                           f"{flow.sellers} top traders sold in the last 24h ({flow.buyers} bought)"))

    # Early movers and pump rides are bought on a short burst, often below their 50-day average: the trend rule
    # doesn't apply (it used to close pump rides at the next check, paying fees for nothing).
    if long and market and market.ma50 is not None and style_of(pos) not in RISKY_STYLES:
        if price < market.ma50:
            out.append(Finding("trend", "SELL", "Uptrend broken: price fell below its 50-day average"))
        elif market.ma20 is not None and price < market.ma20:
            out.append(Finding("trend_watch", "WATCH", "Price dipped below its 20-day average"))

    if pos["hold_until"] and now >= pos["hold_until"]:
        out.append(Finding("hold_time", "WATCH", "Planned hold time is up: take profit or re-check"))
    if style_of(pos) == "copy":
        out = [f for f in out if f.kind not in COPY_IGNORES and f.kind != "target"]
        if copy_holding is False:
            out.append(Finding("trader_closed", "SELL", "The trader you copied sold or halved the position"))
    return out


def flow_since_entry(conn: sqlite3.Connection, pos, now: int) -> Flow:
    """Top-trader buys and sells since this position opened (at most the last 24 hours). Sells from before the buy
    don't count: the coin was picked knowing about them, so they'd sell it again straight away."""
    return activity.flow_since(conn, pos["market_key"], pos["direction"], max(pos["opened_at"], now - activity.DAY))


def copy_holding(conn: sqlite3.Connection, pos) -> bool | None:
    """For swing copies: whether the copied trader still holds; None for other trades."""
    from .swing import trader_still_holding  # swing imports picks, which is imported widely: import late
    return trader_still_holding(conn, pos) if style_of(pos) == "copy" else None


def exit_decision(trade, price: float, findings: list[Finding], now: int) -> tuple[str, float] | None:
    """When a paper or demo trade closes, and at what price: stop, target, a sell signal, or hold time over."""
    kinds = {f.kind for f in findings}
    if "stop" in kinds:
        return "stop", min(price, effective_stop(trade))  # a gap through the stop fills lower
    if "target" in kinds:
        return "target", trade["target_price"]  # a resting take-profit order fills at the target
    hit = kinds & EXIT_KINDS
    if hit:
        return sorted(hit)[0], price
    if now >= trade["hold_until"]:
        return "time", price
    return None


def style_of(pos) -> str:
    try:
        return pos["style"] or "pick"
    except (IndexError, KeyError):
        return "pick"


def advice_of(findings: list[Finding]) -> str:
    return max((f.advice for f in findings), key=SEVERITY.get, default="HOLD")


def pnl(pos, price: float) -> float:
    r = price / pos["entry_price"] - 1
    return r if pos["direction"] == "long" else -r


def open_position(conn: sqlite3.Connection, *, user_id: int, market_key: str, symbol: str, entry_price: float,
                  size_usd: float, pick: Pick | None, now: int, qty: float | None = None, source: str = "manual",
                  auto: bool = False, plan: dict | None = None, btc_entry: float | None = None,
                  cost_pct: float | None = None) -> int:
    """`plan` (stop_price, target_price, hold_until, traders_at_entry) overrides the pick's levels,
    e.g. from the paper trade that was open when an exchange buy happened. `cost_pct` is the estimated
    round-trip cost (fees, spread, slippage) used for results until real fees are known."""
    if plan:
        stop, target, hold_until, traders = plan["stop_price"], plan["target_price"], plan["hold_until"], plan["traders_at_entry"]
    elif pick:
        stop = entry_price * (1 + pick.stop_pct)
        target = entry_price * (1 + pick.target_pct)
        hold_until, traders = now + int(pick.hold_days * 86400), pick.n_traders
    else:
        stop, target = entry_price * (1 - DEFAULT_STOP), entry_price * (1 + DEFAULT_TARGET)
        hold_until, traders = now + DEFAULT_HOLD_DAYS * 86400, 0
    style = STYLE_OF.get(pick.strength, "pick") if pick else (plan or {}).get("style") or "pick"
    trail = pick.trail_pct if pick else (plan or {}).get("trail_pct")
    strength = pick.strength if pick else (plan or {}).get("strength")
    features = pick.features if pick else (plan or {}).get("features")
    with conn:
        cur = conn.execute(
            "INSERT INTO my_positions (opened_at, market_key, symbol, direction, price_key, entry_price, size_usd, "
            "stop_price, target_price, hold_until, traders_at_entry, auto, last_price, advice, qty, source, "
            "cost_pct, btc_entry, style, trail_pct, peak_price, user_id, strength, features) "
            "VALUES (?, ?, ?, 'long', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'HOLD', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, market_key, symbol, market_key, entry_price, size_usd, stop, target, hold_until, traders,
             int(auto), entry_price, qty if qty is not None else size_usd / entry_price, source,
             cost_pct if cost_pct is not None else (pick.cost_pct if pick else None), btc_entry, style,
             trail, entry_price if trail else None, user_id, strength,
             json.dumps(features) if isinstance(features, dict) else features),
        )
    return cur.lastrowid


def net_result(pos, exit_price: float, fees_usd: float | None = None) -> float:
    """Result as a share of the money put in. With `fees_usd` (actual exchange fees for the buy and the
    sell) it's exact; otherwise the estimated round-trip cost is subtracted."""
    gross = exit_price / pos["entry_price"] - 1
    if fees_usd is not None and pos["size_usd"]:
        return gross - fees_usd / pos["size_usd"]
    return gross - (pos["cost_pct"] or 0)


def close_position(conn: sqlite3.Connection, position_id: int, exit_price: float, now: int, reason: str = "manual",
                   fees_usd: float | None = None, btc_price: float | None = None) -> None:
    pos = conn.execute("SELECT * FROM my_positions WHERE id = ?", (position_id,)).fetchone()
    net = net_result(pos, exit_price, fees_usd)
    btc_ret = btc_price / pos["btc_entry"] - 1 if btc_price and pos["btc_entry"] else None
    with conn:
        conn.execute(
            "UPDATE my_positions SET status = 'closed', closed_at = ?, exit_price = ?, exit_reason = ?, net_return = ?, "
            "btc_return = ?, fees_usd = ?, advice = NULL WHERE id = ?",
            (now, exit_price, reason, net, btc_ret, fees_usd, position_id),
        )
        conn.execute("UPDATE alerts SET seen = 1 WHERE position_id = ?", (position_id,))


def raise_alert(conn: sqlite3.Connection, pos, kind: str, advice: str, message: str, now: int) -> dict | None:
    level = "danger" if advice == "SELL" else "success" if advice == "TAKE_PROFIT" else "warning"
    title = {"SELL": "SELL", "TAKE_PROFIT": "TAKE PROFIT", "WATCH": "CHECK"}[advice]
    text = message if kind == "demo_done" else f"{title} {pos['symbol']}: {message}"
    cur = conn.execute("INSERT OR IGNORE INTO alerts (ts, position_id, kind, level, message, user_id) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (now, pos["id"], kind, level, text, pos["user_id"]))
    if cur.rowcount:
        return {"position_id": pos["id"], "kind": kind, "level": level, "message": text, "auto": bool(pos["auto"]),
                "user_id": pos["user_id"], "source": pos["source"], "symbol": pos["symbol"]}
    return None


def update_positions(
    conn: sqlite3.Connection,
    signals: dict[str, Signal],
    markets: dict[str, Market],
    now: int,
) -> list[dict]:
    """Re-evaluate every open position; returns newly raised alerts."""
    new_alerts = []
    for row in conn.execute("SELECT * FROM my_positions WHERE status = 'open' AND source != 'demo'").fetchall():
        sig = signals.get(row["market_key"])
        m = markets.get(row["symbol"])
        price = (m.price if m else None) or (sig.mark_price if sig else None) or row["last_price"]
        pos = with_peak(row, price)
        findings = evaluate(pos, price, sig, flow_since_entry(conn, pos, now),
                            m, now, copy_holding=copy_holding(conn, pos))
        advice = advice_of(findings)
        with conn:
            conn.execute("UPDATE my_positions SET last_price = ?, advice = ?, advice_reasons = ?, peak_price = ? WHERE id = ?",
                         (price, advice, json.dumps([f.message for f in findings]), pos.get("peak_price"), pos["id"]))
            for f in findings:
                a = raise_alert(conn, pos, f.kind, f.advice, f.message, now)
                if a:
                    new_alerts.append(a)
    return new_alerts


def settle_demo_trades(
    conn: sqlite3.Connection,
    signals: dict[str, Signal],
    markets: dict[str, Market],
    now: int,
) -> list[dict]:
    """Demo trades follow the pick's plan by themselves: they close at the stop, the target, a sell
    signal or when the hold time is over. Returns a 'finished' alert per closed trade.
    Successful = positive result after fees."""
    finished = []
    btc = markets.get("BTC")
    for row in conn.execute("SELECT * FROM my_positions WHERE status = 'open' AND source = 'demo'").fetchall():
        m = markets.get(row["symbol"])
        if not m:
            continue
        pos = with_peak(row, m.price)
        findings = evaluate(pos, m.price, signals.get(pos["market_key"]), flow_since_entry(conn, pos, now),
                            m, now, copy_holding=copy_holding(conn, pos))
        decision = exit_decision(pos, m.price, findings, now)
        with conn:
            if decision is None:
                conn.execute("UPDATE my_positions SET last_price = ?, advice = ?, advice_reasons = ?, peak_price = ? "
                             "WHERE id = ?", (m.price, advice_of(findings), json.dumps([f.message for f in findings]),
                                              pos.get("peak_price"), pos["id"]))
                continue
            reason, exit_price = decision
            net = exit_price / pos["entry_price"] - 1 - (pos["cost_pct"] or 0)
            btc_ret = btc.price / pos["btc_entry"] - 1 if btc and pos["btc_entry"] else None
            conn.execute(
                "UPDATE my_positions SET status = 'closed', closed_at = ?, exit_price = ?, exit_reason = ?, "
                "net_return = ?, btc_return = ?, last_price = ?, advice = NULL WHERE id = ?",
                (now, exit_price, reason, net, btc_ret, m.price, pos["id"]),
            )
            verdict = "successful" if net > 0 else "unsuccessful"
            message = (f"Demo trade {pos['symbol']} finished, {verdict}: {net * 100:+.1f}% "
                       f"({'+' if net >= 0 else '-'}${abs(net * pos['size_usd']):.2f} after fees), "
                       f"{EXIT_REASON.get(reason, reason)}")
            a = raise_alert(conn, pos, "demo_done", "TAKE_PROFIT" if net > 0 else "SELL", message, now)
            if a:
                finished.append(a)
    return finished


DEFAULT_DEMO_BALANCE = 10_000.0


def demo_account(conn: sqlite3.Connection, user_id: int, start_balance: float) -> dict:
    """A user's pretend-money account: every demo buy spends cash, every finished trade returns it plus its
    result. Open trades are valued at the latest price, after fees. Best/worst case = every open trade hits its
    target/stop."""
    rows = conn.execute("SELECT * FROM my_positions WHERE source = 'demo' AND user_id = ?", (user_id,)).fetchall()
    open_ = [r for r in rows if r["status"] == "open"]
    closed = [r for r in rows if r["status"] == "closed"]

    def result_at(r, price):  # dollar result after fees if the trade ended at `price`
        return r["size_usd"] * (price / r["entry_price"] - 1 - (r["cost_pct"] or 0))

    invested = sum(r["size_usd"] for r in open_)
    open_result = sum(result_at(r, r["last_price"] or r["entry_price"]) for r in open_)
    realized = sum((r["net_return"] or 0) * r["size_usd"] for r in closed)
    cash = start_balance - invested + realized
    value = cash + invested + open_result
    return {
        "start_balance": start_balance,
        "cash": cash,
        "invested": invested,
        "open_result": open_result,
        "realized": realized,
        "value": value,
        "total_result": value - start_balance,
        "total_result_pct": value / start_balance - 1 if start_balance else 0,
        "best_case": sum(result_at(r, r["target_price"]) for r in open_),
        "worst_case": sum(result_at(r, r["stop_price"]) for r in open_),
        "finished": len(closed),
        "successful": sum(1 for r in closed if (r["net_return"] or 0) > 0),
        "running": len(open_),
    }


def real_account(conn: sqlite3.Connection, user_id: int, account: dict | None) -> dict:
    """A user's real money, same shape as demo_account. Open positions are valued at the latest (live) price,
    after estimated selling costs. With OKX connected, value = trading-account cash + live value of the coins."""
    rows = conn.execute("SELECT * FROM my_positions WHERE source != 'demo' AND user_id = ?", (user_id,)).fetchall()
    open_ = [r for r in rows if r["status"] == "open"]
    closed = [r for r in rows if r["status"] == "closed" and r["net_return"] is not None]

    def result_at(r, price):
        return r["size_usd"] * (price / r["entry_price"] - 1 - (r["cost_pct"] or 0))

    def worth(r):
        return (r["qty"] or r["size_usd"] / r["entry_price"]) * (r["last_price"] or r["entry_price"])

    invested = sum(r["size_usd"] for r in open_)
    market_value = sum(worth(r) for r in open_)
    connected = bool(account and account.get("ok"))
    cash = account.get("cash_usd") if connected else None
    # Only coins actually in the exchange account count toward its value.
    on_exchange = sum(worth(r) for r in open_ if r["source"] == "synced" or r["exchange_check"] == "ok")
    return {
        "connected": connected,
        "cash": cash,
        "value": (cash or 0) + on_exchange if connected else market_value,
        "outside_exchange": market_value - on_exchange if connected else 0.0,
        "invested": invested,
        "market_value": market_value,
        "open_result": sum(result_at(r, r["last_price"] or r["entry_price"]) for r in open_),
        "best_case": sum(result_at(r, r["target_price"]) for r in open_),
        "worst_case": sum(result_at(r, r["stop_price"]) for r in open_),
        "realized": sum(r["net_return"] * r["size_usd"] for r in closed),
        "finished": len(closed),
        "successful": sum(1 for r in closed if r["net_return"] > 0),
        "running": len(open_),
    }


TYPE_OF_STYLE = {"early": "Early", "pump": "Pump", "copy": "Copy"}


def results_by_type(conn: sqlite3.Connection, user_id: int, demo: bool) -> dict[str, dict]:
    """Closed trades grouped by what kind of pick they came from: Strong, Good, Early, Pump (or Other)."""
    rows = conn.execute(
        f"SELECT * FROM my_positions WHERE user_id = ? AND status = 'closed' AND net_return IS NOT NULL "
        f"AND source {'=' if demo else '!='} 'demo'", (user_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        kind = r["strength"] or TYPE_OF_STYLE.get(r["style"], "Other")
        g = out.setdefault(kind, {"trades": 0, "wins": 0, "total_usd": 0.0, "sum_ret": 0.0, "sum_btc": 0.0, "n_btc": 0})
        g["trades"] += 1
        g["wins"] += r["net_return"] > 0
        g["total_usd"] += r["net_return"] * r["size_usd"]
        g["sum_ret"] += r["net_return"]
        if r["btc_return"] is not None:
            g["sum_btc"] += r["btc_return"]
            g["n_btc"] += 1
    return {k: {"trades": g["trades"], "win_rate": g["wins"] / g["trades"], "avg_return": g["sum_ret"] / g["trades"],
                "total_usd": g["total_usd"], "avg_btc": g["sum_btc"] / g["n_btc"] if g["n_btc"] else None}
            for k, g in out.items()}


def snapshot_demo(conn: sqlite3.Connection, user_id: int, value: float, btc_price: float | None, now: int,
                  every_s: int = 3600) -> bool:
    """Record the demo account's value at most once per `every_s` (for the progress chart)."""
    last = conn.execute("SELECT MAX(ts) FROM demo_snapshots WHERE user_id = ?", (user_id,)).fetchone()[0]
    if last is not None and now - last < every_s:
        return False
    with conn:
        conn.execute("INSERT OR REPLACE INTO demo_snapshots VALUES (?, ?, ?, ?)", (user_id, now, value, btc_price))
    return True
