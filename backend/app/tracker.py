"""Track record: every swing copy is one paper trade.

A trade opens the first time a coin becomes a swing copy (at the exchange
price then) and closes at the first of:
- safety stop hit: exits at the stop, or lower if the price gapped through it
- the copied trader closing or halving the position
- the 30-day hold time running out

The result is net of the round-trip costs estimated at entry (fees, spread,
slippage) and compared with simply holding BTC over the same period. Prices
are checked every few seconds, and the trader's position every minute.

Paper trades of the strategies that were dropped (style other than 'copy') stay in the table but are no longer
updated or shown.
"""

import json
import sqlite3
import statistics
from dataclasses import asdict

from . import portfolio
from .market import Market
from .models import Pick


def open_trades(conn: sqlite3.Connection, picks: list[Pick], markets: dict[str, Market], now: int) -> int:
    held = {r[0] for r in conn.execute("SELECT market_key FROM pick_trades WHERE status = 'open' AND style = 'copy'")}
    btc = markets.get("BTC")
    rows = [
        (p.market_key, p.symbol, p.strength, now, p.price, p.stop_price, p.target_price, now + int(p.hold_days * 86400),
         p.cost_pct, p.n_traders, btc.price if btc else None, json.dumps([asdict(c) for c in p.checks]), p.price,
         portfolio.STYLE_OF.get(p.strength, "pick"), json.dumps(p.features))
        for p in picks if p.market_key not in held
    ]
    with conn:
        conn.executemany(
            "INSERT INTO pick_trades (market_key, symbol, strength, opened_at, entry_price, stop_price, target_price, "
            "hold_until, cost_pct, traders_at_entry, btc_entry, checks, last_price, style, features) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def update_trades(conn: sqlite3.Connection, markets: dict[str, Market], now: int) -> int:
    closed = 0
    btc = markets.get("BTC")
    for row in conn.execute("SELECT * FROM pick_trades WHERE status = 'open' AND style = 'copy'").fetchall():
        m = markets.get(row["symbol"])
        if not m:
            continue
        t = portfolio.with_peak(row, m.price)
        findings = portfolio.evaluate(t, m.price, now, copy_holding=portfolio.copy_holding(conn, t))
        decision = portfolio.exit_decision(t, m.price, findings, now)
        with conn:
            if decision is None:
                conn.execute("UPDATE pick_trades SET last_price = ?, peak_price = ? WHERE id = ?",
                             (m.price, t.get("peak_price"), t["id"]))
                continue
            reason, exit_price = decision
            net = exit_price / t["entry_price"] - 1 - t["cost_pct"]
            btc_ret = btc.price / t["btc_entry"] - 1 if btc and t["btc_entry"] else None
            conn.execute(
                "UPDATE pick_trades SET status = 'closed', closed_at = ?, exit_price = ?, exit_reason = ?, "
                "net_return = ?, btc_return = ?, last_price = ? WHERE id = ?",
                (now, exit_price, reason, net, btc_ret, m.price, t["id"]),
            )
        closed += 1
    return closed


def _summary(rows) -> dict:
    nets = [r["net_return"] for r in rows]
    btcs = [r["btc_return"] for r in rows if r["btc_return"] is not None]
    return {
        "trades": len(nets),
        "win_rate": sum(1 for x in nets if x > 0) / len(nets) if nets else None,
        "avg_net": statistics.fmean(nets) if nets else None,
        "avg_btc": statistics.fmean(btcs) if btcs else None,
        "total_net": sum(nets) if nets else None,  # sum of equal-sized trades, as a share of one trade's size
    }


def performance(conn: sqlite3.Connection, markets: dict[str, Market]) -> dict:
    closed = conn.execute("SELECT * FROM pick_trades WHERE status = 'closed' AND style = 'copy' "
                          "ORDER BY closed_at DESC").fetchall()
    open_ = conn.execute("SELECT * FROM pick_trades WHERE status = 'open' AND style = 'copy' "
                         "ORDER BY opened_at DESC").fetchall()
    first = conn.execute("SELECT MIN(opened_at) FROM pick_trades WHERE style = 'copy'").fetchone()[0]

    def unrealized(t):
        m = markets.get(t["symbol"])
        price = m.price if m else t["last_price"]
        return price / t["entry_price"] - 1 - t["cost_pct"]

    return {
        "since": first,
        "overall": _summary(closed),
        "open": [{"symbol": t["symbol"], "strength": t["strength"], "opened_at": t["opened_at"],
                  "entry_price": t["entry_price"], "net_now": unrealized(t)} for t in open_],
        "recent": [{"symbol": r["symbol"], "strength": r["strength"], "opened_at": r["opened_at"],
                    "closed_at": r["closed_at"], "entry_price": r["entry_price"], "exit_price": r["exit_price"],
                    "exit_reason": r["exit_reason"], "net_return": r["net_return"], "btc_return": r["btc_return"]}
                   for r in closed[:15]],
    }
