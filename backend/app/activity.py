"""Who is buying and who is selling.

Each refresh diffs the followed traders' current positions against the
position log: new positions are entries, vanished ones are exits, and a
position cut to half its peak size or less counts as a (partial) sell. This
drives the "traders are selling" alerts and the observed holding times.
"""

import sqlite3
import statistics
from collections import defaultdict

from .models import Flow, Position

DAY = 86400
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


def flows(conn: sqlite3.Connection, now: int, window_s: int = DAY) -> dict[tuple[str, str], Flow]:
    """Entries and exits by currently followed traders in the last `window_s`."""
    since = now - window_s
    rows = conn.execute(
        """
        SELECT market_key, direction,
          SUM(closed_at IS NULL AND first_seen >= :since AND (exact = 1 OR baseline = 0)) AS buyers,
          SUM(closed_at >= :since OR (closed_at IS NULL AND reduced_at >= :since)) AS sellers
        FROM position_log l
        WHERE EXISTS (SELECT 1 FROM trader_stats t WHERE t.source = l.source AND t.address = l.address)
          AND (closed_at IS NULL OR closed_at >= :since)
        GROUP BY market_key, direction
        """,
        {"since": since},
    )
    return {(r["market_key"], r["direction"]): Flow(r["buyers"] or 0, r["sellers"] or 0) for r in rows}


def flow_since(conn: sqlite3.Connection, market_key: str, direction: str, since: int) -> Flow:
    """Entries and exits by currently followed traders on one side of one market since `since`
    (the same rules as flows())."""
    r = conn.execute(
        """
        SELECT SUM(closed_at IS NULL AND first_seen >= :since AND (exact = 1 OR baseline = 0)) AS buyers,
               SUM(closed_at >= :since OR (closed_at IS NULL AND reduced_at >= :since)) AS sellers
        FROM position_log l
        WHERE market_key = :key AND direction = :dir
          AND EXISTS (SELECT 1 FROM trader_stats t WHERE t.source = l.source AND t.address = l.address)
          AND (closed_at IS NULL OR closed_at >= :since)
        """,
        {"key": market_key, "dir": direction, "since": since},
    ).fetchone()
    return Flow(r["buyers"] or 0, r["sellers"] or 0)


def hold_times(conn: sqlite3.Connection, now: int, lookback_s: int = 30 * DAY) -> dict[tuple[str, str], float]:
    """Median observed holding time in days, per market side, where there are at least 3 complete trades."""
    samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in conn.execute(
        "SELECT market_key, direction, closed_at - first_seen AS held FROM position_log "
        "WHERE closed_at >= ? AND (exact = 1 OR baseline = 0)",
        (now - lookback_s,),
    ):
        samples[(r["market_key"], r["direction"])].append(r["held"] / DAY)
    return {k: statistics.median(v) for k, v in samples.items() if len(v) >= 3}
