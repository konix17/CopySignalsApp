"""Pump-and-dump detection from 5-minute candles.

A pump starts with an "ignition" candle: volume at least 5× the coin's normal
5-minute volume and the price up at least 1%. From there, the app tracks the
price the pump started from (base), the highest price since (peak), and how
the volume behaves, then puts the pump in a phase:

- starting: less than 15% above base and still near the high. This is the only
  phase worth riding.
- running: already 15% or more above base, still near the high. Late: most of
  the easy gain is gone.
- topping: 5% or more off the peak, or volume has faded to under a third of its
  peak while the price is up. Pumps usually end here.
- dumping: up 12% or more, then down 12% or more from the peak. Sell if you
  hold it, and don't buy.

"Looks like a coordinated pump" = a thin market (under $20M a day), a volume
spike of 10× or more, and a fast move (8% or more within an hour of ignition).
Those move hardest and collapse hardest.

Riding a pump: buy only in the starting phase, protect it with a trailing stop
(3× the coin's typical 5-minute range, 4–10%) that follows the price up, take
profit at 3× the trail, and give up after 6 hours. Any sign of topping or
dumping is a sell.
"""

import sqlite3
import statistics
import time
from dataclasses import dataclass

IGNITION_VOLUME = 5.0
IGNITION_MOVE = 0.01
STARTING_MAX_GAIN = 0.15
TOPPING_OFF_PEAK = 0.05
DUMP_RISE, DUMP_OFF_PEAK = 0.12, 0.12
FADE = 0.35
PUMP_LIKE_VOLUME, PUMP_LIKE_SPIKE, PUMP_LIKE_MOVE = 20_000_000, 10.0, 0.08
MIN_TRAIL, MAX_TRAIL = 0.04, 0.10
RIDE_HOURS = 6


@dataclass
class PumpState:
    coin: str
    phase: str  # starting | running | topping | dumping
    pump_like: bool
    rise: float  # peak vs base
    from_peak: float  # price vs peak (<= 0)
    gain_now: float  # price vs base
    minutes: float  # since ignition
    volume_spike: float  # biggest 5-minute volume / normal
    base_price: float
    peak_price: float
    price: float
    trail_pct: float

    @property
    def summary(self) -> str:
        text = {
            "starting": f"pump starting: {self.gain_now * 100:+.0f}% from where it began {self.minutes:.0f} min ago",
            "running": f"pump running: already {self.gain_now * 100:+.0f}% in {self.minutes:.0f} min, late to join",
            "topping": f"pump topping out: {self.from_peak * 100:.0f}% off its peak, buyers are fading",
            "dumping": f"dumping: pumped {self.rise * 100:+.0f}%, now {self.from_peak * 100:.0f}% off the peak",
        }[self.phase]
        return text + (" (looks like a coordinated pump)" if self.pump_like else "")


def analyze(coin: str, candles: list[list], volume_usd_24h: float, now_ms: int | None = None) -> PumpState | None:
    """`candles`: 5m candles as returned by OkxSpot.candles_5m, oldest first (the last one may still be forming).
    None = no pump."""
    if len(candles) < 6 or volume_usd_24h <= 0:
        return None
    normal = volume_usd_24h / 288
    rows = [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in candles]
    ignition = next((i for i, (_, o, _, _, c, qv) in enumerate(rows)
                     if qv >= IGNITION_VOLUME * normal and o > 0 and c / o - 1 >= IGNITION_MOVE), None)
    if ignition is None:
        return None
    t0, base = rows[ignition][0], rows[ignition][1]
    after = rows[ignition:]
    peak = max(h for _, _, h, _, _, _ in after)
    price = rows[-1][4]
    volumes = [qv for *_, qv in after]
    peak_volume = max(volumes)
    recent_volume = statistics.fmean(volumes[-2:])
    rise, from_peak, gain_now = peak / base - 1, price / peak - 1, price / base - 1
    minutes = ((now_ms or rows[-1][0] + 300_000) - t0) / 60_000

    if rise >= DUMP_RISE and from_peak <= -DUMP_OFF_PEAK:
        phase = "dumping"
    elif from_peak <= -TOPPING_OFF_PEAK or (recent_volume < FADE * peak_volume and gain_now >= 0.10):
        phase = "topping"
    elif gain_now < STARTING_MAX_GAIN:
        phase = "starting"
    else:
        phase = "running"

    spike = peak_volume / normal
    fast = any(h / base - 1 >= PUMP_LIKE_MOVE for t, _, h, _, _, _ in after if t - t0 <= 3_600_000)
    ranges = [h / l - 1 for _, _, h, l, _, _ in rows[:ignition] if l > 0] or [h / l - 1 for _, _, h, l, _, _ in rows if l > 0]
    trail = max(MIN_TRAIL, min(MAX_TRAIL, 3 * statistics.median(ranges)))
    return PumpState(coin, phase, volume_usd_24h < PUMP_LIKE_VOLUME and spike >= PUMP_LIKE_SPIKE and fast,
                     rise, from_peak, gain_now, minutes, spike, base, peak, price, trail)


def save(conn: sqlite3.Connection, states: dict[str, PumpState], checked: set[str], now: int) -> None:
    """Store the latest state for every coin checked; coins checked with no pump are cleared."""
    with conn:
        for coin in checked:
            s = states.get(coin)
            if s is None:
                conn.execute("DELETE FROM pumps WHERE coin = ?", (coin,))
                continue
            conn.execute(
                "INSERT OR REPLACE INTO pumps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (coin, s.phase, int(s.pump_like), s.rise, s.from_peak, s.gain_now, s.minutes, s.volume_spike,
                 s.base_price, s.peak_price, s.price, s.trail_pct, s.summary, now),
            )
        conn.execute("DELETE FROM pumps WHERE updated_at < ?", (now - 6 * 3600,))


def phases(conn: sqlite3.Connection, max_age_s: int = 900, now: int | None = None) -> dict[str, dict]:
    """Recent pump states by coin (as dicts), for sell alerts and the UI."""
    now = now or int(time.time())
    rows = conn.execute("SELECT * FROM pumps WHERE updated_at >= ?", (now - max_age_s,))
    return {r["coin"]: dict(r) for r in rows}
