"""Automatic trading for demo accounts only.

For every user who switched it on, each new pick of an enabled type (Strong, Good, Early, Pump) is bought
with demo money at the current OKX price, following the pick's plan. Sizes keep the same share of the
account that the pick suggests for the reference bankroll, within the user's limits on open trades and on
how much of the demo account may be invested. Selling is done by the normal demo exit rules
(portfolio.settle_demo_trades), checked every few seconds. Real accounts are never traded.
"""

import sqlite3

from . import app_settings, portfolio, users
from .logs import audit
from .market import Market
from .models import Pick

TYPES = ("Strong", "Good", "Copy", "Early", "Pump")
# Early movers and pump rides are off by default: a 6-month backtest (research/rising_backtest.py, 3,683 trades)
# found no edge after costs. They're still followed as paper trades in the track record.
DEFAULTS = {"enabled": False, "types": ["Strong", "Good", "Copy"], "max_open": 10, "max_invested_pct": 0.60}
MIN_TRADE_USD = 5.0
ORDER = {"Strong": 0, "Copy": 1, "Pump": 2, "Good": 3, "Early": 4}


def config(conn: sqlite3.Connection, user_id: int) -> dict:
    saved = users.get_setting(conn, user_id, "autotrade", {}) or {}
    cfg = {**DEFAULTS, **saved}
    cfg["types"] = [t for t in cfg["types"] if t in TYPES]
    cfg["max_open"] = max(1, min(50, int(cfg["max_open"])))
    cfg["max_invested_pct"] = max(0.05, min(1.0, float(cfg["max_invested_pct"])))
    return cfg


def demo_start(conn: sqlite3.Connection, user_id: int) -> float:
    return float(users.get_setting(conn, user_id, "demo_start_balance", app_settings.get(conn, "default_demo_balance")))


def run(conn: sqlite3.Connection, picks: list[Pick], reference_bankroll: float, markets: dict[str, Market],
        now: int, reference_fee: float | None = None, fee_for=None) -> list[dict]:
    """Open demo trades for every user with auto-trading on. Returns what was bought.
    Picks carry costs for `reference_fee`; with `fee_for(user_id)` each trade is costed at that user's own fee."""
    bought = []
    if not picks or reference_bankroll <= 0:
        return bought
    btc = markets.get("BTC")
    for user in users.all_active(conn):
        cfg = config(conn, user.id)
        if not cfg["enabled"]:
            continue
        acct = portfolio.demo_account(conn, user.id, demo_start(conn, user.id))
        open_syms = {r[0] for r in conn.execute(
            "SELECT symbol FROM my_positions WHERE user_id = ? AND source = 'demo' AND status = 'open'", (user.id,))}
        n_open, invested, cash, value = len(open_syms), acct["invested"], acct["cash"], acct["value"]
        fee_shift = 2 * (fee_for(user.id) - reference_fee) if fee_for and reference_fee is not None else 0.0
        for p in sorted(picks, key=lambda x: (ORDER.get(x.strength, 9), -x.score)):
            if p.strength not in cfg["types"] or p.symbol in open_syms or p.size_usd <= 0:
                continue
            if n_open >= cfg["max_open"]:
                break
            size = (p.size_usd / reference_bankroll) * value
            size = round(min(size, cfg["max_invested_pct"] * value - invested, cash), 2)
            if size < MIN_TRADE_USD:
                continue
            m = markets.get(p.symbol)
            price = m.price if m else p.price
            pid = portfolio.open_position(conn, user_id=user.id, market_key=p.market_key, symbol=p.symbol,
                                          entry_price=price, size_usd=size, pick=p, now=now, source="demo", auto=True,
                                          btc_entry=btc.price if btc else None,
                                          cost_pct=max(0.0, p.cost_pct + fee_shift))
            audit(conn, "demo.auto_buy", user_id=user.id, username=user.username, now=now,
                  detail={"position": pid, "symbol": p.symbol, "type": p.strength, "size_usd": size, "price": price})
            bought.append({"user_id": user.id, "position_id": pid, "symbol": p.symbol, "size_usd": size})
            open_syms.add(p.symbol)
            n_open, invested, cash = n_open + 1, invested + size, cash - size
    return bought
