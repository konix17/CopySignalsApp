"""Turn the background data into a short list of spot buys.

Every candidate must be buyable on your exchange's spot market with enough
volume and a tight spread, then pass checks:

1. Smart traders are buying (required): at least 3 followed wallet traders
   hold it long, they agree (few on the other side), they aren't net
   selling, and the price hasn't already run far past their entry. Followers
   of copy traders mostly lose by joining late, so lateness is penalized hard.
2. Price is in an uptrend: above its 50-day average and up over 30 days
   (time-series momentum, the best-documented edge in crypto).
3. Not overcrowded: Binance top traders aren't already all-in long, funding
   isn't high, and there was no rush of new longs in 24h. Crowded longs are
   a known setup for sharp drops, so this is a warning sign, not a confirmation.

3 of 3 = Strong, 2 of 3 = Good (check 1 is always required). When BTC trades
below its own 50-day average the market is in a downtrend: fewer picks, half size.

Levels and size: stop = 1.5 × daily volatility × hold_days^¼ (3–20%), target =
2 × stop. Round-trip costs (fees + spread + slippage) are subtracted, and picks
whose net reward/risk falls below the minimum are dropped. Size is set so a stop
costs RISK_STRONG / RISK_GOOD of the bankroll, capped per coin and in total.
"""

import math
from dataclasses import dataclass

from .market import Market
from .models import Check, Flow, Pick, Positioning, Signal

HORIZON_DAYS = {"day": 2, "week": 7, "month": 21, "allTime": 30}
DEFAULT_VOL = 0.05
MIN_STOP, MAX_STOP = 0.03, 0.20
REWARD_RISK = 2.0
LATE_AFTER = 0.08  # price this far past the traders' average entry starts to count as late
CROWDED_LONG_SHARE = 0.75
HIGH_FUNDING = 0.0005  # 0.05% per 8h
LONG_RUSH = 0.08  # +8 pts long share in 24h
SOURCE_LABEL = {"hyperliquid": "Hyperliquid", "gmx": "GMX", "binance": "Binance", "okx": "OKX"}


@dataclass
class PickParams:
    bankroll: float
    open_exposure_usd: float
    fee_rate: float
    slippage: float
    min_volume_usd: float
    max_spread: float
    risk_strong: float
    risk_good: float
    max_position_pct: float
    max_total_pct: float
    max_picks: int
    max_picks_downtrend: int
    min_net_reward_risk: float


def percentile_map(values: list[float]) -> list[float]:
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    for rank, i in enumerate(order):
        out[i] = rank / (n - 1) if n > 1 else 1.0
    return out


def estimate_hold(market_key: str, by_window: dict[str, dict[str, Signal]],
                  observed: dict[tuple[str, str], float]) -> tuple[float, str]:
    if (market_key, "long") in observed:
        return max(0.5, min(60.0, observed[(market_key, "long")])), "observed"
    weights = {}
    for w, sigs in by_window.items():
        s = sigs.get(market_key)
        if s and s.direction == "long" and s.conviction > 0:
            weights[w] = s.conviction
    if not weights:
        return 7.0, "estimated"
    return sum(HORIZON_DAYS[w] * c for w, c in weights.items()) / sum(weights.values()), "estimated"


def stop_distance(daily_vol: float, hold_days: float) -> float:
    return max(MIN_STOP, min(MAX_STOP, 1.5 * daily_vol * max(hold_days, 1.0) ** 0.25))


def round_trip_cost(market: Market, fee_rate: float, slippage: float) -> float:
    """Buy and sell: fee + half the spread + slippage, each way."""
    return 2 * (fee_rate + market.spread / 2 + slippage)


def market_regime(markets: dict[str, Market]) -> dict:
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
                 f"Market downtrend: BTC is {gap * 100:+.1f}% vs its 50-day average, so fewer and smaller picks"),
    }


def smart_money_check(s: Signal, flow: Flow, strength: float) -> tuple[Check, float]:
    net = flow.buyers - flow.sellers
    breadth = 1 - math.exp(-s.n_traders / 4)
    flow_f = 0.5 + 0.5 * math.tanh(net / 3)
    late = max(0.4, 1 - (s.move_since_entry - LATE_AFTER) * 2) if s.move_since_entry > LATE_AFTER else 1.0
    score = s.agreement**1.5 * (0.4 * breadth + 0.3 * strength + 0.3 * flow_f) * late
    passed = s.n_traders >= 3 and s.agreement >= 0.65 and net > -2 and score >= 0.40
    parts = [f"{s.n_traders} proven traders hold it"]
    if s.n_opposing:
        parts.append(f"{s.n_opposing} bet against it")
    if flow.buyers:
        parts.append(f"{flow.buyers} added in the last 24h")
    if flow.sellers:
        parts.append(f"{flow.sellers} sold in the last 24h")
    if s.move_since_entry > LATE_AFTER:
        parts.append(f"price already {s.move_since_entry * 100:+.0f}% past their entry")
    return Check("Smart traders are buying", passed, ", ".join(parts)), score


def trend_check(m: Market) -> Check:
    if not m.has_trend_data or m.ret30 is None:
        return Check("Price is in an uptrend", False, "Not enough price history yet")
    above20, above50, up30 = m.trend_checks
    passed = above50 and up30
    detail = (f"{'above' if above50 else 'below'} its 50-day average, "
              f"{m.ret30 * 100:+.0f}% over 30 days")
    if above50 and not above20:
        detail += ", dipped below its 20-day average"
    return Check("Price is in an uptrend", passed, detail)


def crowding_check(rows: list[Positioning]) -> Check:
    binance = next((p for p in rows if p.exchange == "binance"), None)
    if binance is None:
        return Check("Not overcrowded", True, "No futures data for this coin")
    problems = []
    if binance.long_share >= CROWDED_LONG_SHARE:
        problems.append(f"{binance.long_share * 100:.0f}% of Binance top traders are already long")
    if (binance.funding or 0) >= HIGH_FUNDING:
        problems.append(f"buyers pay high funding ({binance.funding * 100:.3f}% per 8h)")
    if (binance.change_24h or 0) >= LONG_RUSH:
        problems.append(f"rush of new longs (+{binance.change_24h * 100:.0f} pts in 24h)")
    if problems:
        return Check("Not overcrowded", False, "; ".join(problems))
    funding = "normal funding" if binance.funding is not None else "funding unknown"
    return Check("Not overcrowded", True, f"Binance top traders {binance.long_share * 100:.0f}% long, {funding}")


def build_picks(
    signals: list[Signal],
    by_window: dict[str, dict[str, Signal]],
    flows: dict[tuple[str, str], Flow],
    positioning: dict[str, list[Positioning]],
    markets: dict[str, Market],
    observed_holds: dict[tuple[str, str], float],
    p: PickParams,
) -> tuple[list[Pick], list[dict], dict]:
    """Returns (picks, coins top traders are exiting, market regime)."""
    regime = market_regime(markets)
    longs = [s for s in signals if s.direction == "long"]
    strength = dict(zip((s.market_key for s in longs), percentile_map([s.conviction for s in longs])))
    candidates: list[Pick] = []
    exiting: list[dict] = []

    for s in longs:
        m = markets.get(s.symbol)
        if m is None or m.volume_usd < p.min_volume_usd or m.spread > p.max_spread:
            continue  # not buyable on the exchange's spot market, or too thin to trade cleanly
        flow = flows.get((s.market_key, "long"), Flow())
        if flow.sellers - flow.buyers >= 2:
            exiting.append({"symbol": s.symbol, "sellers": flow.sellers, "buyers": flow.buyers, "holders": s.n_traders})
            continue

        smart, smart_score = smart_money_check(s, flow, strength[s.market_key])
        if not smart.passed:
            continue
        trend = trend_check(m)
        crowd = crowding_check(positioning.get(s.symbol, []))
        passed = 1 + trend.passed + crowd.passed
        if passed < 2:
            continue

        hold, basis = estimate_hold(s.market_key, by_window, observed_holds)
        stop = stop_distance(m.daily_vol or DEFAULT_VOL, hold)
        target = stop * REWARD_RISK
        cost = round_trip_cost(m, p.fee_rate, p.slippage)
        if (target - cost) / (stop + cost) < p.min_net_reward_risk:
            continue
        risk = p.risk_strong if passed == 3 else p.risk_good
        size = min(p.bankroll * risk / (stop + cost), p.bankroll * p.max_position_pct)
        notes = []
        if basis == "observed":
            typical = f"{hold * 24:.0f} hours" if hold < 1.5 else f"{hold:.0f} days"
            notes.append(f"Top traders typically hold {s.symbol} about {typical}")
        binance = next((x for x in positioning.get(s.symbol, []) if x.exchange == "binance"), None)
        above20, above50, up30 = m.trend_checks
        features = {
            "smart_score": round(smart_score, 4), "agreement": s.agreement, "n_traders": s.n_traders,
            "n_opposing": s.n_opposing, "buyers_24h": flow.buyers, "sellers_24h": flow.sellers,
            "move_since_entry": s.move_since_entry, "conviction_pct": round(strength[s.market_key], 4),
            "above_ma20": above20, "above_ma50": above50, "ret30": m.ret30, "trend_pass": trend.passed,
            "crowd_pass": crowd.passed, "top_long_share": binance.long_share if binance else None,
            "top_long_change": binance.change_24h if binance else None, "funding": binance.funding if binance else None,
            "daily_vol": m.daily_vol, "spread": round(m.spread, 6), "volume_usd": m.volume_usd,
            "change_24h": m.change_24h, "risk_on": regime["risk_on"], "hold_days": round(hold, 2),
            "stop_pct": round(stop, 4), "cost_pct": round(cost, 5), "checks_passed": passed,
        }
        candidates.append(Pick(
            market_key=s.market_key, symbol=s.symbol, pair=m.pair,
            strength="Strong" if passed == 3 else "Good",
            score=round(smart_score + 0.5 * trend.passed + 0.3 * crowd.passed, 4),
            checks=[smart, trend, crowd],
            price=m.price, stop_price=m.price * (1 - stop), target_price=m.price * (1 + target),
            stop_pct=-stop, target_pct=target, cost_pct=cost, hold_days=round(hold, 1), hold_basis=basis,
            size_usd=size, net_win_usd=0, net_loss_usd=0,
            n_traders=s.n_traders, buyers_24h=flow.buyers, sellers_24h=flow.sellers,
            notes=notes, sources=s.sources, features=features,
        ))

    candidates.sort(key=lambda x: (x.strength != "Strong", -x.score))
    picks = candidates[: p.max_picks if regime["risk_on"] else p.max_picks_downtrend]

    # Size: half in a downtrend, then fit everything under the total exposure cap.
    if not regime["risk_on"]:
        for x in picks:
            x.size_usd /= 2
    room = max(0.0, p.bankroll * p.max_total_pct - p.open_exposure_usd)
    total = sum(x.size_usd for x in picks)
    if total > room:
        for x in picks:
            x.size_usd *= room / total if total else 0
        limit = f"{p.max_total_pct * 100:.0f}% of your bankroll"
        note = (f"Sized down so all of today's picks together stay within {limit}" if p.open_exposure_usd <= 0 else
                f"Sized down because your open positions already use part of the {limit} limit")
        for x in picks:
            x.notes.append(note)
    for x in picks:
        x.size_usd = round(x.size_usd / 5) * 5 if x.size_usd >= 50 else round(x.size_usd)
        x.net_win_usd = round(x.size_usd * (x.target_pct - x.cost_pct), 2)
        x.net_loss_usd = round(-x.size_usd * (-x.stop_pct + x.cost_pct), 2)

    exiting.sort(key=lambda e: -(e["sellers"] - e["buyers"]))
    return picks, exiting, regime
