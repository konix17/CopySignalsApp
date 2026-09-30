"""Who is buying and who is selling.

Each refresh diffs the followed traders' current positions against the
position log: new positions are entries, vanished ones are exits, and a
position cut to half its peak size or less counts as a (partial) sell. Swing
copies (swing.py) start from positions that stay open 12 hours and sell when
the trader closes or halves them.
"""

import sqlite3

from .models import Position

REDUCED_AT = 0.5  # size <= 50% of peak counts as selling


def sync_position_log(
    conn: sqlite3.Connection, source: str, positions: list[Position], fetched: set[str], ts: int, stale_after_s: float
) -> None:
    """Record entries/exits for one source. Only traders in `fetched` can close positions,
    so a failed request never looks like a sell-off."""
    last_fetch = {r[0]: r[1] for r in conn.execute("SELECT address, last_fetched FROM trader_fetch WHERE source = ?", (source,))}
    open_rows = {
        (r["address"], r["market_key"], r["direction"]): r
        for r in conn.execute("SELECT * FROM position_log WHERE source = ? AND closed_at IS NULL", (source,))
    }
    current = {(p.address, p.market_key, p.direction): p for p in positions if p.address in fetched}

    with conn:
        for key, p in current.items():
            row = open_rows.get(key)
            if row:
                peak = max(row["peak_size_usd"], p.size_usd)
                reduced = row["reduced_at"] or (ts if p.size_usd <= REDUCED_AT * peak else None)
                conn.execute(
                    "UPDATE position_log SET last_seen = ?, size_usd = ?, peak_size_usd = ?, reduced_at = ? WHERE id = ?",
                    (ts, p.size_usd, peak, reduced, row["id"]),
                )
            else:
                # Trader newly watched (or not seen for a while): we don't know when they entered.
                watched = p.address in last_fetch and last_fetch[p.address] >= ts - stale_after_s
                exact = p.opened_at is not None
                conn.execute(
                    "INSERT INTO position_log (source, address, market_key, direction, first_seen, exact, baseline, "
                    "last_seen, size_usd, peak_size_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source, p.address, p.market_key, p.direction, p.opened_at if exact else ts, int(exact),
                     int(not watched), ts, p.size_usd, p.size_usd),
                )
        for key, row in open_rows.items():
            if key not in current and key[0] in fetched:
                conn.execute("UPDATE position_log SET closed_at = ? WHERE id = ?", (ts, row["id"]))
        conn.executemany(
            "INSERT OR REPLACE INTO trader_fetch VALUES (?, ?, ?)", [(source, a, ts) for a in fetched]
        )
