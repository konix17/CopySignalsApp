"""Swing copies: copy a followed trader's long once it has stayed open for 12 hours; sell when they sell.

Research (research/copy_backtest.py: 6,500 long trades by 260 random Hyperliquid traders, second half of the last
90 days, copied on spot with 0.5% round-trip costs):
- How fast you copy doesn't matter: instant +0.28% per trade, 10 minutes late +0.28%. Holding time does: positions
  the trader closed within an hour lost 0.59% per copy, because quick trades don't cover the fees.
- Copying only positions still open N hours after the trader opened them, and selling a minute after they close:
  4 h +1.07% per trade, 12 h +1.33% (±0.50), 24 h +1.90% after costs; before costs 0.84-1.37% better than holding
  BTC over the same hours. Positions traders keep are the ones they're right about.
It's a mostly rising market, so copies are followed as paper trades and demo-traded, not trusted blindly.

Rules: a long held by a followed trader for at least MIN_AGE_H, not cut to half its peak size, in a coin that's
liquid on OKX spot (MIN_VOLUME_USD and MAX_SPREAD). One copy per coin, following the best-scored trader
holding it. Sell when that trader closes or halves the position; a SAFETY_STOP guards against disasters (the rule
was tested without a stop, so it's set wide). When BTC is below its 50-day average, copies are half size.
"""

import json
import sqlite3

from .market import Market, round_trip_cost
from .models import Check, Pick

MIN_AGE_H = 12
SAFETY_STOP = 0.25
MAX_HOLD_DAYS = 30
SIZE_PCT = 0.05  # of the bankroll per copy
MAX_COPIES = 5
NO_TARGET = 1.0  # +100%: copies have no target; they sell when the trader does


def market_regime(markets: dict[str, Market]) -> dict:
    """Uptrend while BTC is above its 50-day average; in a downtrend copies are half size."""
    btc = markets.get("BTC")
    if not btc or btc.ma50 is None:
        return {"risk_on": True, "text": "Market trend unknown (no BTC history yet)"}
    up = btc.price > btc.ma50
    gap = btc.price / btc.ma50 - 1
    return {
        "risk_on": up,
        "btc_price": btc.price,
        "btc_ma50": btc.ma50,
        "text": (f"Market uptrend: BTC is {gap * 100:+.1f}% vs its 50-day average" if up else
                 f"Market downtrend: BTC is {gap * 100:+.1f}% vs its 50-day average, so copies are half size"),
    }


def candidates(conn: sqlite3.Connection, markets: dict[str, Market], scores: dict[tuple[str, str], float], now: int,
               bankroll: float, fee_rate: float, slippage: float, min_volume_usd: float, max_spread: float,
               risk_on: bool = True) -> list[Pick]:
    """Current swing copies, best trader first."""
    rows = conn.execute(
        """
        SELECT l.id, l.source, l.address, l.market_key, l.first_seen, l.baseline, p.entry_price, p.size_usd, p.symbol
        FROM position_log l
        JOIN positions p ON p.source = l.source AND p.address = l.address AND p.market_key = l.market_key
                         AND p.direction = 'long'
        WHERE l.direction = 'long' AND l.closed_at IS NULL AND l.reduced_at IS NULL AND l.first_seen <= ?
        """, (now - MIN_AGE_H * 3600,)).fetchall()
    best: dict[str, tuple] = {}
    for r in rows:
        score = scores.get((r["source"], r["address"]), 0.0)
        m = markets.get(r["symbol"])
        if score <= 0 or m is None or m.volume_usd < min_volume_usd or m.spread > max_spread:
            continue
        if r["symbol"] not in best or score > best[r["symbol"]][0]:
            best[r["symbol"]] = (score, r, m)
    out = []
    for score, r, m in sorted(best.values(), key=lambda x: -x[0])[:MAX_COPIES]:
        age_h = (now - r["first_seen"]) / 3600
        cost = round_trip_cost(m, fee_rate, slippage)
        size = round(bankroll * SIZE_PCT * (1 if risk_on else 0.5))
        vs_entry = m.price / r["entry_price"] - 1 if r["entry_price"] else 0.0
        held = f"at least {age_h / 24:.0f} days" if r["baseline"] else (
            f"{age_h:.0f} hours" if age_h < 48 else f"{age_h / 24:.1f} days")
        who = f"{r['source'].capitalize()} trader {r['address'][:6]}…{r['address'][-4:]}"
        out.append(Pick(
            market_key=r["market_key"], symbol=r["symbol"], pair=m.pair, strength="Copy", score=round(score, 4),
            checks=[Check("A proven trader is holding it", True, f"{who} has held it {held}"),
                    Check("They're in profit" if vs_entry >= 0 else "They're holding through a loss", vs_entry >= 0,
                          f"price is {vs_entry * 100:+.1f}% from their entry")],
            price=m.price, stop_price=m.price * (1 - SAFETY_STOP), target_price=m.price * (1 + NO_TARGET),
            stop_pct=-SAFETY_STOP, target_pct=NO_TARGET, cost_pct=cost, hold_days=MAX_HOLD_DAYS, hold_basis="estimated",
            size_usd=size, net_win_usd=0.0, net_loss_usd=round(-size * (SAFETY_STOP + cost), 2),
            n_traders=1, buyers_24h=0, sellers_24h=0,
            notes=["Sells when that trader sells or halves the position (checked every minute), not at a target"],
            sources=[r["source"]],
            features={"copy_log_id": r["id"], "copy_source": r["source"], "copy_address": r["address"], "age_h": round(age_h, 1),
                      "baseline": bool(r["baseline"]), "trader_entry": r["entry_price"], "trader_score": round(score, 4)},
        ))
    return out


def trader_still_holding(conn: sqlite3.Connection, pos) -> bool | None:
    """For a copy trade: is the copied trader still holding (not closed, not halved)? None if unknown."""
    try:
        features = json.loads(pos["features"] or "{}") if isinstance(pos["features"], str) else (pos["features"] or {})
    except (KeyError, IndexError, ValueError):
        return None
    log_id = features.get("copy_log_id")
    if not log_id:
        return None
    row = conn.execute("SELECT closed_at, reduced_at FROM position_log WHERE id = ?", (log_id,)).fetchone()
    if row is None:
        return None
    return row["closed_at"] is None and row["reduced_at"] is None
