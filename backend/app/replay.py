"""Exit-rule lab: replay the recorded paper trades on OKX price history under other exit rules.

The track record (tracker.py) shows what the live rules did. This asks "what if": every recorded trade is
replayed on 5-minute OKX candles from the moment it opened, under each rule set below, net of the same
round-trip costs. A coin is held at most once at a time, so a rule that keeps a trade open also skips the
re-buys that happened while it would still have been held.

Rule sets:
- recorded:     what actually happened (stop, target, hold time, plus the top-trader and trend exits)
- plan:         stop, target and hold time only (trailing stop for pump rides)
- sells_after:  plan, plus the "top traders sold" exit counting only sells made after the trade opened.
                The live rule counts sells from the last 24 hours, including ones before the buy, so a
                coin can be picked and then sold on the same data minutes later.

Conservative fills: a candle touching both the stop and the target counts as a stop, a gap through the stop
fills at the candle's open, and the candle the trade opened in is skipped. Top-trader sells are rebuilt
from the position log, so they're only as complete as that log.
"""

import asyncio
import bisect
import json
import sqlite3
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from . import db
from .market import OkxSpot, Pacer

BAR_S = 300
DAY = 86400
RULES = ("recorded", "plan", "sells_after")
RULE_LABEL = {
    "recorded": "What happened (live rules)",
    "plan": "Stop, target and hold time only",
    "sells_after": "Plan + sell only on trader sells after the buy",
}
SIGNAL_EXITS = {"selling", "exited", "flipped", "trend", "pump_fading", "pump_dump"}


@dataclass
class Outcome:
    trade_id: int
    symbol: str
    style: str
    opened_at: int
    closed_at: int | None  # None: still open under this rule
    reason: str
    net: float  # closed: final result; open: marked at the last price
    btc: float | None = None


@dataclass
class Candles:
    ts: list[int] = field(default_factory=list)  # open time, seconds
    rows: list[tuple[float, float, float, float]] = field(default_factory=list)  # open, high, low, close


# --- price history ---------------------------------------------------------------------------------

async def _history(client: httpx.AsyncClient, spot: OkxSpot, pair: str, start: int, end: int, pacer: Pacer) -> Candles:
    c = Candles()
    for ts, o, h, lo, close, _, _ in await spot.history(client, pair, "5m", start - BAR_S, end + BAR_S, pacer):
        c.ts.append(ts)
        c.rows.append((o, h, lo, close))
    return c


async def fetch_candles(trades: list[sqlite3.Row], pairs: dict[str, str], now: int,
                        region: str = "eea") -> dict[str, Candles]:
    """5-minute candles per coin covering all its trades (from the first open to the last planned exit)."""
    spans: dict[str, list[int]] = {}
    for t in trades:
        lo, hi = spans.get(t["symbol"], [t["opened_at"], t["opened_at"]])
        spans[t["symbol"]] = [min(lo, t["opened_at"]), max(hi, min(now, t["hold_until"]))]
    spot, pacer = OkxSpot(region), Pacer()
    sem = asyncio.Semaphore(4)

    async def one(client, coin):
        async with sem:
            return coin, await _history(client, spot, pairs.get(coin, f"{coin}-USDT"), *spans[coin], pacer)

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "copy-signals/0.4"}) as client:
        results = await asyncio.gather(*(one(client, c) for c in spans), return_exceptions=True)
    return {r[0]: r[1] for r in results if not isinstance(r, BaseException)}


# --- top-trader sells, rebuilt from the position log ---------------------------------------------

class SellLog:
    """When followed traders sold (closed or halved a long) and bought, per market."""

    def __init__(self, conn: sqlite3.Connection):
        self.sells: dict[str, list[int]] = defaultdict(list)
        self.buys: dict[str, list[tuple[int, int | None]]] = defaultdict(list)  # (first_seen, closed_at)
        for r in conn.execute("SELECT market_key, first_seen, exact, baseline, closed_at, reduced_at "
                              "FROM position_log WHERE direction = 'long'"):
            sold = r["closed_at"] if r["reduced_at"] is None else min(r["reduced_at"], r["closed_at"] or r["reduced_at"])
            if sold is not None:
                self.sells[r["market_key"]].append(sold)
            if r["exact"] or not r["baseline"]:
                self.buys[r["market_key"]].append((r["first_seen"], r["closed_at"]))
        for v in self.sells.values():
            v.sort()

    def flow(self, market_key: str, since: int, now: int) -> tuple[int, int]:
        """(buyers still holding, sellers) between `since` and `now`."""
        s = self.sells.get(market_key, [])
        sellers = bisect.bisect_right(s, now) - bisect.bisect_right(s, since)
        buyers = sum(1 for first, closed in self.buys.get(market_key, ())
                     if since < first <= now and (closed is None or closed > now))
        return buyers, sellers


# --- replay ----------------------------------------------------------------------------------------

def _btc_return(btc: Candles | None, t0: int, t1: int) -> float | None:
    if not btc or not btc.ts:
        return None
    i0 = max(0, bisect.bisect_right(btc.ts, t0) - 1)
    i1 = max(0, bisect.bisect_right(btc.ts, t1) - 1)
    a, b = btc.rows[i0][3], btc.rows[i1][3]
    return b / a - 1 if a else None


def simulate(trade, candles: Candles, now: int, sells: SellLog | None = None) -> tuple[int | None, str, float]:
    """Walk one trade forward. Returns (exit time or None if still open, reason, exit or last price)."""
    entry, stop, target = trade["entry_price"], trade["stop_price"], trade["target_price"]
    trail, peak = trade["trail_pct"], trade["entry_price"]
    at_entry = trade["traders_at_entry"] or 0
    start = bisect.bisect_left(candles.ts, trade["opened_at"] + 1)  # skip the candle the trade opened in
    last = entry
    for i in range(start, len(candles.ts)):
        ts = candles.ts[i]
        if ts >= trade["hold_until"]:
            return trade["hold_until"], "time", last
        if ts + BAR_S > now:
            break  # still forming
        o, h, lo, c = candles.rows[i]
        eff = max(stop, peak * (1 - trail)) if trail else stop
        if lo <= eff:
            return ts + BAR_S, "stop", min(o, eff)
        if h >= target:
            return ts + BAR_S, "target", target
        if trail:
            peak = max(peak, h)
        last = c
        if sells is not None:
            end = ts + BAR_S
            buyers, sellers = sells.flow(trade["market_key"], max(trade["opened_at"], end - DAY), end)
            if sellers >= 2 and sellers > buyers and sellers >= max(2, 0.3 * at_entry):
                return end, "selling", c
    if now >= trade["hold_until"]:
        return trade["hold_until"], "time", last
    return None, "open", last


def replay(conn: sqlite3.Connection, candles: dict[str, Candles], now: int) -> dict[str, list[Outcome]]:
    trades = conn.execute("SELECT * FROM pick_trades ORDER BY opened_at, id").fetchall()
    sells = SellLog(conn)
    btc = candles.get("BTC")
    out: dict[str, list[Outcome]] = {r: [] for r in RULES}

    for t in trades:
        if t["status"] == "closed":
            out["recorded"].append(Outcome(t["id"], t["symbol"], t["style"], t["opened_at"], t["closed_at"],
                                           t["exit_reason"], t["net_return"], t["btc_return"]))
        else:
            last = t["last_price"] or t["entry_price"]
            out["recorded"].append(Outcome(t["id"], t["symbol"], t["style"], t["opened_at"], None, "open",
                                           last / t["entry_price"] - 1 - t["cost_pct"]))

    for rule, log in (("plan", None), ("sells_after", sells)):
        busy_until: dict[str, float] = {}
        for t in trades:
            c = candles.get(t["symbol"])
            if c is None or not c.ts or t["opened_at"] < busy_until.get(t["symbol"], 0):
                continue  # no price history, or this coin would still be held from an earlier buy
            closed_at, reason, price = simulate(t, c, now, log)
            busy_until[t["symbol"]] = closed_at if closed_at is not None else float("inf")
            net = price / t["entry_price"] - 1 - t["cost_pct"]
            out[rule].append(Outcome(t["id"], t["symbol"], t["style"], t["opened_at"], closed_at, reason, net,
                                     _btc_return(btc, t["opened_at"], closed_at or now)))
    return out


def summarize(outcomes: list[Outcome]) -> dict:
    closed = [o for o in outcomes if o.closed_at is not None]
    open_ = [o for o in outcomes if o.closed_at is None]
    nets = [o.net for o in closed]
    reasons: dict[str, list[float]] = defaultdict(list)
    for o in closed:
        reasons[o.reason].append(o.net)
    return {
        "trades": len(closed),
        "win_rate": sum(x > 0 for x in nets) / len(nets) if nets else None,
        "avg_net": statistics.fmean(nets) if nets else None,
        "total_net": sum(nets),  # equal-sized trades: sum of results, as a share of one trade's size
        "avg_hours": statistics.fmean((o.closed_at - o.opened_at) / 3600 for o in closed) if closed else None,
        "avg_btc": statistics.fmean(b) if (b := [o.btc for o in closed if o.btc is not None]) else None,
        "open": len(open_),
        "open_net": sum(o.net for o in open_),
        "by_reason": {k: {"trades": len(v), "avg_net": statistics.fmean(v)} for k, v in sorted(reasons.items())},
    }


def report(results: dict[str, list[Outcome]]) -> dict:
    styles = ("pick", "early", "pump")
    return {rule: {"label": RULE_LABEL[rule], "all": summarize(rows),
                   "by_style": {s: summarize([o for o in rows if o.style == s]) for s in styles}}
            for rule, rows in results.items()}


async def run(conn: sqlite3.Connection, region: str = "eea", now: int | None = None) -> dict:
    """Fetch the price history the recorded trades need and replay them under every rule set."""
    now = now or int(time.time())
    trades = conn.execute("SELECT symbol, opened_at, hold_until FROM pick_trades").fetchall()
    if not trades:
        return {"now": now, "rules": {}, "missing": []}
    pairs = {c: m.pair for c, m in db.load_markets(conn).items()}
    spans = [*trades, {"symbol": "BTC", "opened_at": min(t["opened_at"] for t in trades), "hold_until": now}]
    candles = await fetch_candles(spans, pairs, now, region)
    missing = sorted({t["symbol"] for t in trades} - set(candles))
    return {"now": now, "rules": report(replay(conn, candles, now)), "missing": missing}


def format_report(result: dict) -> str:
    def pct(x):
        return "     -" if x is None else f"{x * 100:+6.2f}%"

    lines = []
    for rule, r in result["rules"].items():
        lines.append(f"\n{r['label']}  [{rule}]")
        lines.append(f"  {'':8} {'trades':>6} {'win':>5} {'avg net':>8} {'total':>9} {'hours':>6} {'BTC':>8} {'open':>5}")
        for name, s in [("all", r["all"]), *r["by_style"].items()]:
            if not s["trades"] and not s["open"]:
                continue
            win = "    -" if s["win_rate"] is None else f"{s['win_rate'] * 100:4.0f}%"
            hours = "     -" if s["avg_hours"] is None else f"{s['avg_hours']:6.1f}"
            lines.append(f"  {name:8} {s['trades']:>6} {win} {pct(s['avg_net'])} {s['total_net'] * 100:+8.1f}% "
                         f"{hours} {pct(s['avg_btc'])} {s['open']:>5}")
        reasons = ", ".join(f"{k} {v['trades']} ({v['avg_net'] * 100:+.2f}%)" for k, v in r["all"]["by_reason"].items())
        lines.append(f"  exits: {reasons}")
    if result.get("missing"):
        lines.append(f"\nNo price history for: {', '.join(result['missing'])}")
    return "\n".join(lines)


def save(conn: sqlite3.Connection, result: dict) -> None:
    db.set_pref(conn, "replay_result", json.dumps(result))


def load(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_pref(conn, "replay_result", "")
    return json.loads(raw) if raw else None
