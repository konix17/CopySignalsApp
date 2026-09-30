"""Rising now: smaller coins that just started moving up on unusual volume.

Every 15 seconds the scanner compares each smaller spot coin on your exchange: its last 15
minutes and last hour (1-minute candles, the current one included) against its normal activity. A coin is flagged when:
- it's up at least 2% in 15 minutes and 3% in the hour,
- 15-minute volume is at least 3× its normal pace (24h volume / 96),
- it's still trading near the top of the 15-minute range (not already reversing),
- it isn't already up more than 40% on the day (that's usually the end of a pump).

"Smaller" = outside the 20 most traded coins, with at least $3M daily volume
and a spread under 0.4%, so you can still get in and out.

Flags explain why (volume, speed, smart traders holding it, a new 7-day high)
and warn when it's risky (already up a lot today, thin market, long-term
downtrend, looks like a coordinated pump). Flagged coins whose pump is in its
starting phase become Pump rides with a trailing stop (see pumps.py); pumps
that are topping or dumping aren't offered. All of these are paid from the
high-risk budget: each trade uses a fixed share of it.
"""

import json
import math
import sqlite3
from dataclasses import asdict, dataclass

from . import pumps
from .market import Market
from .models import Check, Pick, Signal
from .picks import round_trip_cost

TOP_EXCLUDED = 20
MIN_VOLUME_24H = 3_000_000
MAX_SPREAD = 0.004
MIN_CHANGE_15M, MIN_CHANGE_1H = 0.02, 0.03
MIN_VOLUME_SURGE = 3.0
NEAR_HIGH = 0.985
MAX_DAY_CHANGE = 0.40
LATE_DAY_CHANGE = 0.25
THIN_VOLUME = 10_000_000
HOLD_DAYS = 2.0
MIN_STOP, MAX_STOP = 0.04, 0.12
RISING_FOR_S = 180  # still counts as "rising now" this long after the last scan that flagged it
KEEP_S = 24 * 3600


@dataclass
class Move:
    coin: str
    pair: str
    price: float
    change_15m: float
    change_1h: float
    volume_surge: float  # 15-minute volume / normal 15-minute volume
    high_15m: float


def universe(markets: dict[str, Market]) -> list[Market]:
    """Smaller coins that are still liquid enough to trade."""
    by_volume = sorted(markets.values(), key=lambda m: -m.volume_usd)
    big = {m.coin for m in by_volume[:TOP_EXCLUDED]}
    return [m for m in by_volume if m.coin not in big and m.volume_usd >= MIN_VOLUME_24H and m.spread <= MAX_SPREAD]


def detect(coins: list[Market], w15: dict[str, dict], w60: dict[str, dict]) -> list[Move]:
    out = []
    for m in coins:
        a, b = w15.get(m.pair), w60.get(m.pair)
        if not a or not b or a["open"] <= 0 or b["open"] <= 0:
            continue
        price = a["last"]
        change_15m = price / a["open"] - 1
        change_1h = price / b["open"] - 1
        normal_15m = m.volume_usd / 96
        surge = a["quote_volume"] / normal_15m if normal_15m else 0
        if (change_15m >= MIN_CHANGE_15M and change_1h >= MIN_CHANGE_1H and surge >= MIN_VOLUME_SURGE
                and price >= a["high"] * NEAR_HIGH and (m.change_24h or 0) <= MAX_DAY_CHANGE):
            out.append(Move(m.coin, m.pair, price, change_15m, change_1h, surge, a["high"]))
    return out


def score_move(mv: Move, m: Market, smart_holders: int, above_week_high: bool | None) -> float:
    s_volume = min(1.0, math.log(mv.volume_surge) / math.log(15))  # 3x ~ 0.4, 15x = 1
    s_move = min(1.0, mv.change_1h / 0.12)
    s_fresh = min(1.0, mv.change_15m / mv.change_1h) if mv.change_1h > 0 else 0
    s_smart = 1.0 if smart_holders else 0.0
    s_trend = 0.5 if m.ma20 is None else float(mv.price > m.ma20)
    score = 100 * (0.30 * s_volume + 0.20 * s_move + 0.15 * s_fresh + 0.15 * s_smart + 0.10 * s_trend
                   + 0.10 * bool(above_week_high))
    if (m.change_24h or 0) > LATE_DAY_CHANGE:
        score *= 0.7
    return round(score, 1)


def risky_size(budget: float, budget_free: float, share: float) -> float:
    """Each risky trade uses `share` of the high-risk budget, never more than what's left of it."""
    size = max(0.0, min(budget * share, budget_free))
    return round(size / 5) * 5 if size >= 50 else round(size)


def build(
    moves: list[Move],
    markets: dict[str, Market],
    signals: dict[str, Signal],
    week_highs: dict[str, float],
    pump_states: dict,
    budget: float,
    budget_free: float,
    share: float,
    fee_rate: float,
    slippage: float,
) -> list[Pick]:
    """Flagged coins become Early picks; coins in the starting phase of a pump become Pump rides.
    Coins whose pump is topping or dumping aren't offered at all (see the avoid list)."""
    out = []
    for mv in moves:
        m = markets[mv.coin]
        pump = pump_states.get(mv.coin)
        if pump and pump.phase in ("topping", "dumping"):
            continue
        sig = signals.get(f"perp:{mv.coin}")
        smart = sig.n_traders if sig and sig.direction == "long" else 0
        high7 = week_highs.get(mv.coin)
        breakout = high7 is not None and mv.price > high7
        riding = pump is not None and pump.phase == "starting"

        flags = []
        if riding:
            flags.append(Check("Pump starting", True, pump.summary))
        flags += [
            Check("Rising fast", True, f"{mv.change_15m * 100:+.1f}% in 15 minutes, {mv.change_1h * 100:+.1f}% in the last hour"),
            Check("Volume surge", True, f"{mv.volume_surge:.1f}× its normal trading volume right now"),
        ]
        if breakout:
            flags.append(Check("New 7-day high", True, f"above last week's high of {high7:.6g}"))
        if smart:
            flags.append(Check("Smart traders hold it", True, f"{smart} of the followed traders are long"))
        if m.ma20 is not None and mv.price > m.ma20:
            flags.append(Check("Above its 20-day average", True, "the bigger trend agrees"))
        if pump and pump.phase == "running":
            flags.append(Check("Pump already running", False, pump.summary))
        if pump and pump.pump_like:
            flags.append(Check("Looks like a coordinated pump", False, "these rise fastest and collapse fastest: "
                                                                       "sell the moment the trailing stop or a dump alert hits"))
        if (m.change_24h or 0) > LATE_DAY_CHANGE:
            flags.append(Check("Already up a lot today", False, f"{m.change_24h * 100:+.0f}% in 24h: you may be late"))
        if m.volume_usd < THIN_VOLUME:
            flags.append(Check("Thin market", False, f"only ${m.volume_usd / 1e6:.1f}M traded per day: prices can jump both ways"))
        if m.ma50 is not None and mv.price < m.ma50:
            flags.append(Check("Long-term downtrend", False, "still below its 50-day average"))

        cost = round_trip_cost(m, fee_rate, slippage)
        size = risky_size(budget, budget_free, share)
        if riding:
            stop, target, hold, trail = pump.trail_pct, 3 * pump.trail_pct, pumps.RIDE_HOURS / 24, pump.trail_pct
            notes = [f"Pump ride: trailing stop {trail * 100:.0f}% below the highest price since you buy, "
                     f"out after {pumps.RIDE_HOURS} hours at the latest"]
        else:
            stop = max(MIN_STOP, min(MAX_STOP, 1.5 * (m.daily_vol or 0.06)))
            target, hold, trail = 2 * stop, HOLD_DAYS, None
            notes = ["Speculative early mover: small size, tight stop, short hold"]
        score = score_move(mv, m, smart, breakout) + (10 if riding else 0)
        features = {
            "change_15m": round(mv.change_15m, 5), "change_1h": round(mv.change_1h, 5),
            "volume_surge": round(mv.volume_surge, 3), "change_24h": m.change_24h, "volume_usd": m.volume_usd,
            "spread": round(m.spread, 6), "breakout_7d": breakout, "smart_holders": smart,
            "above_ma20": m.ma20 is not None and mv.price > m.ma20, "above_ma50": m.ma50 is not None and mv.price > m.ma50,
            "daily_vol": m.daily_vol, "pump_phase": pump.phase if pump else None,
            "pump_like": pump.pump_like if pump else None, "pump_gain": round(pump.gain_now, 5) if pump else None,
            "pump_spike": round(pump.volume_spike, 2) if pump else None, "score": round(min(100.0, score), 1),
            "stop_pct": round(stop, 4), "cost_pct": round(cost, 5),
        }
        out.append(Pick(
            market_key=f"perp:{mv.coin}", symbol=mv.coin, pair=mv.pair, strength="Pump" if riding else "Early",
            score=round(min(100.0, score), 1), checks=flags,
            price=mv.price, stop_price=mv.price * (1 - stop), target_price=mv.price * (1 + target),
            stop_pct=-stop, target_pct=target, cost_pct=cost, hold_days=hold, hold_basis="estimated",
            size_usd=size, net_win_usd=round(size * (target - cost), 2), net_loss_usd=round(-size * (stop + cost), 2),
            n_traders=smart, buyers_24h=0, sellers_24h=0, notes=notes, sources=["okx"], trail_pct=trail,
            features=features,
        ))
    out.sort(key=lambda p: (p.strength != "Pump", -p.score))
    return out


def save(conn: sqlite3.Connection, picks: list[Pick], prices: dict[str, float], now: int) -> list[Pick]:
    """Record flags; returns the ones that are new (not flagged in the last few minutes)."""
    new = []
    with conn:
        for p in picks:
            row = conn.execute("SELECT last_flagged_at FROM movers WHERE coin = ?", (p.symbol,)).fetchone()
            payload = json.dumps(asdict(p))
            if row and now - row[0] <= RISING_FOR_S:
                conn.execute("UPDATE movers SET last_flagged_at = ?, last_price = ?, peak_price = MAX(peak_price, ?), "
                             "score = ?, pick_json = ? WHERE coin = ?", (now, p.price, p.price, p.score, payload, p.symbol))
            else:
                conn.execute("INSERT OR REPLACE INTO movers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (p.symbol, now, p.price, now, p.price, p.price, p.score, payload))
                new.append(p)
        # Keep following recent flags so "since flagged" stays current.
        for coin, price in prices.items():
            conn.execute("UPDATE movers SET last_price = ?, peak_price = MAX(peak_price, ?) WHERE coin = ?",
                         (price, price, coin))
        conn.execute("DELETE FROM movers WHERE last_flagged_at < ?", (now - KEEP_S,))
    return new


def load(conn: sqlite3.Connection, now: int) -> tuple[list[Pick], list[dict], dict[str, dict]]:
    """(rising now, flagged earlier in the last 24h, flag info for each rising coin)."""
    rising, earlier = [], []
    for r in conn.execute("SELECT * FROM movers ORDER BY first_flagged_at DESC"):
        info = {"symbol": r["coin"], "flagged_at": r["first_flagged_at"], "flag_price": r["flag_price"],
                "last_price": r["last_price"], "since_flag": r["last_price"] / r["flag_price"] - 1,
                "best_since_flag": r["peak_price"] / r["flag_price"] - 1, "score": r["score"]}
        if now - r["last_flagged_at"] <= RISING_FOR_S:
            d = json.loads(r["pick_json"])
            d["checks"] = [Check(**c) for c in d["checks"]]
            rising.append((Pick(**d), info))
        else:
            earlier.append(info)
    return [p for p, _ in sorted(rising, key=lambda x: -x[0].score)], earlier, {p.symbol: i for p, i in rising}
