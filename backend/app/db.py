import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

from .models import Position, Positioning, TraderStat

SCHEMA = """
CREATE TABLE IF NOT EXISTS trader_stats (
    source TEXT NOT NULL, address TEXT NOT NULL, window TEXT NOT NULL,
    name TEXT, pnl REAL, roi REAL, volume REAL, account_value REAL, win_rate REAL,
    score REAL NOT NULL, followed INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
    PRIMARY KEY (source, address, window)
);
CREATE TABLE IF NOT EXISTS positions (
    source TEXT NOT NULL, address TEXT NOT NULL, market_key TEXT NOT NULL, asset_class TEXT NOT NULL,
    symbol TEXT, title TEXT, direction TEXT NOT NULL, size_usd REAL, entry_price REAL, mark_price REAL,
    price_key TEXT, leverage REAL, unrealized_pnl REAL, opened_at INTEGER, url TEXT, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS positions_market ON positions (market_key);

-- One row per trader position lifetime, so we can see traders entering and exiting.
-- baseline = the position already existed when we started watching this trader, and its
-- open time is unknown (exact = 0): it is not counted as a fresh buy, nor used for hold times.
CREATE TABLE IF NOT EXISTS position_log (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, address TEXT NOT NULL, market_key TEXT NOT NULL,
    direction TEXT NOT NULL, first_seen INTEGER NOT NULL, exact INTEGER NOT NULL, baseline INTEGER NOT NULL,
    last_seen INTEGER NOT NULL, size_usd REAL, peak_size_usd REAL, reduced_at INTEGER, closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS position_log_open ON position_log (source, address, closed_at);
CREATE INDEX IF NOT EXISTS position_log_market ON position_log (market_key, direction);
CREATE TABLE IF NOT EXISTS trader_fetch (
    source TEXT NOT NULL, address TEXT NOT NULL, last_fetched INTEGER NOT NULL, PRIMARY KEY (source, address)
);

CREATE TABLE IF NOT EXISTS positioning (
    exchange TEXT NOT NULL, symbol TEXT NOT NULL, long_share REAL NOT NULL, long_share_24h REAL, funding REAL,
    updated_at INTEGER NOT NULL, PRIMARY KEY (exchange, symbol)
);
-- OKX spot market data: price, liquidity and trend per coin.
CREATE TABLE IF NOT EXISTS markets (
    coin TEXT PRIMARY KEY, pair TEXT NOT NULL, price REAL NOT NULL, bid REAL, ask REAL, volume_usd REAL,
    ma20 REAL, ma50 REAL, ret30 REAL, daily_vol REAL, trend_at INTEGER, updated_at INTEGER NOT NULL,
    change_24h REAL
);
-- "Rising now" scanner: smaller coins flagged for starting to rise on unusual volume.
CREATE TABLE IF NOT EXISTS movers (
    coin TEXT PRIMARY KEY, first_flagged_at INTEGER NOT NULL, flag_price REAL NOT NULL, last_flagged_at INTEGER NOT NULL,
    last_price REAL NOT NULL, peak_price REAL NOT NULL, score REAL NOT NULL, pick_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trader_quality (
    source TEXT NOT NULL, address TEXT NOT NULL, max_drawdown REAL NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY (source, address)
);
CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY, last_attempt INTEGER, last_ok INTEGER, last_error TEXT,
    n_traders INTEGER, n_positions INTEGER, duration_s REAL
);

-- Track record: every pick is one paper trade, from when it first appears until it exits.
CREATE TABLE IF NOT EXISTS pick_trades (
    id INTEGER PRIMARY KEY, market_key TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'long',
    strength TEXT NOT NULL, opened_at INTEGER NOT NULL, entry_price REAL NOT NULL, stop_price REAL NOT NULL,
    target_price REAL NOT NULL, hold_until INTEGER NOT NULL, cost_pct REAL NOT NULL, traders_at_entry INTEGER,
    btc_entry REAL, checks TEXT, status TEXT NOT NULL DEFAULT 'open', closed_at INTEGER, exit_price REAL,
    exit_reason TEXT, net_return REAL, btc_return REAL, last_price REAL,
    style TEXT NOT NULL DEFAULT 'pick',  -- 'pick' (three checks), 'early' (rising now) or 'pump' (pump ride)
    trail_pct REAL, peak_price REAL,     -- trailing stop: exit when price falls trail_pct below peak_price
    features TEXT                        -- the pick's inputs at entry (JSON), for learning
);
CREATE INDEX IF NOT EXISTS pick_trades_status ON pick_trades (status, market_key);

-- Each user's own positions. source: 'manual' (entered by hand), 'synced' (from their OKX account),
-- 'demo' (pretend money, closes by itself; auto = 1 when the demo auto-trader opened it).
CREATE TABLE IF NOT EXISTS my_positions (
    id INTEGER PRIMARY KEY, opened_at INTEGER NOT NULL, market_key TEXT NOT NULL, symbol TEXT NOT NULL,
    direction TEXT NOT NULL, price_key TEXT NOT NULL, entry_price REAL NOT NULL, size_usd REAL NOT NULL,
    stop_price REAL, target_price REAL, hold_until INTEGER, traders_at_entry INTEGER,
    auto INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open', closed_at INTEGER, exit_price REAL,
    last_price REAL, advice TEXT, advice_reasons TEXT,
    -- exchange_check: does the user's OKX account agree ('ok' | 'qty_mismatch' | 'missing')
    qty REAL, source TEXT NOT NULL DEFAULT 'manual', exchange_check TEXT, stop_order_price REAL, exit_reason TEXT,
    stop_order_kind TEXT,
    cost_pct REAL, btc_entry REAL, net_return REAL, btc_return REAL, fees_usd REAL,  -- results
    style TEXT NOT NULL DEFAULT 'pick', trail_pct REAL, peak_price REAL,
    user_id INTEGER, strength TEXT,
    features TEXT  -- the pick's inputs when it was opened (JSON), for learning which inputs predict winners
);
-- Latest pump analysis per coin (5-minute candles), used for pump rides and sell alerts.
CREATE TABLE IF NOT EXISTS pumps (
    coin TEXT PRIMARY KEY, phase TEXT NOT NULL, pump_like INTEGER NOT NULL, rise REAL, from_peak REAL,
    gain_now REAL, minutes REAL, volume_spike REAL, base_price REAL, peak_price REAL, price REAL,
    trail_pct REAL, summary TEXT, updated_at INTEGER NOT NULL
);

-- Each user's OKX account mirror (read-only key). Re-fetchable from OKX at any time.
CREATE TABLE IF NOT EXISTS user_holdings (
    user_id INTEGER NOT NULL, coin TEXT NOT NULL, exchange TEXT NOT NULL, qty REAL NOT NULL, locked REAL NOT NULL,
    price REAL, value_usd REAL, avg_cost REAL, cost_known INTEGER NOT NULL, opened_at INTEGER, stop_price REAL,
    stop_qty REAL, stop_kind TEXT, target_price REAL, updated_at INTEGER NOT NULL, PRIMARY KEY (user_id, coin)
);
CREATE TABLE IF NOT EXISTS user_trades (
    user_id INTEGER NOT NULL, exchange TEXT NOT NULL, pair TEXT NOT NULL, id TEXT NOT NULL, order_id TEXT,
    price REAL NOT NULL, qty REAL NOT NULL, quote_qty REAL NOT NULL, commission REAL, commission_asset TEXT,
    time INTEGER NOT NULL, is_buyer INTEGER NOT NULL, PRIMARY KEY (user_id, exchange, pair, id)
);
CREATE TABLE IF NOT EXISTS user_orders (
    user_id INTEGER NOT NULL, exchange TEXT NOT NULL, pair TEXT NOT NULL, order_id TEXT NOT NULL, kind TEXT NOT NULL,
    price REAL, stop_price REAL, qty REAL, time INTEGER, PRIMARY KEY (user_id, exchange, pair, order_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, position_id INTEGER NOT NULL, kind TEXT NOT NULL,
    level TEXT NOT NULL, message TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0, user_id INTEGER,
    UNIQUE (position_id, kind)
);
-- App-wide values (scanner state, admin defaults as "setting.<name>").
CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Accounts and security.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
    last_login_at INTEGER, failed_logins INTEGER NOT NULL DEFAULT 0, locked_until INTEGER,
    password_changed_at INTEGER, totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0  -- temporary password: only a password change is allowed
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL, expires_at INTEGER NOT NULL, ip TEXT, user_agent TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, user_id INTEGER, username TEXT, action TEXT NOT NULL,
    detail TEXT, ip TEXT, level TEXT NOT NULL DEFAULT 'info'
);
CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts);
CREATE INDEX IF NOT EXISTS audit_log_user ON audit_log (user_id, ts);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (user_id, key)
);
-- Encrypted per-user secrets (OKX key, secret, passphrase).
CREATE TABLE IF NOT EXISTS user_secrets (
    user_id INTEGER NOT NULL, name TEXT NOT NULL, value_enc TEXT NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, name)
);
-- Demo account value over time (for the progress chart), with BTC for comparison.
CREATE TABLE IF NOT EXISTS demo_snapshots (
    user_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL, btc_price REAL, PRIMARY KEY (user_id, ts)
);

-- Trend bot (trendbot.py): one account per user, demo money. last_run_day = the day (00:00 UTC) of the last check.
CREATE TABLE IF NOT EXISTS bot_accounts (
    user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'demo', enabled INTEGER NOT NULL DEFAULT 1,
    start_balance REAL NOT NULL, cash REAL NOT NULL, started_at INTEGER NOT NULL, btc_start REAL,
    last_run_day INTEGER, last_run_at INTEGER, targets TEXT
);
CREATE TABLE IF NOT EXISTS bot_holdings (
    user_id INTEGER NOT NULL, coin TEXT NOT NULL, qty REAL NOT NULL, cost_usd REAL NOT NULL, PRIMARY KEY (user_id, coin)
);
CREATE TABLE IF NOT EXISTS bot_trades (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, ts INTEGER NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, value_usd REAL NOT NULL, fee_usd REAL NOT NULL, reason TEXT
);
CREATE INDEX IF NOT EXISTS bot_trades_user ON bot_trades (user_id, ts);
CREATE TABLE IF NOT EXISTS bot_snapshots (
    user_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL, btc_price REAL, PRIMARY KEY (user_id, ts)
);
"""

_STAT_COLS = ["source", "address", "window", "name", "pnl", "roi", "volume", "account_value", "win_rate", "score"]
_POS_COLS = [f.name for f in fields(Position)]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _rename_columns(conn)
    _add_missing_columns(conn)
    conn.executescript(POST_SCHEMA)
    _migrate(conn)
    return conn


# Indexes on columns that older databases only get from _add_missing_columns, so they're created afterwards.
POST_SCHEMA = """
CREATE INDEX IF NOT EXISTS my_positions_user ON my_positions (user_id, status);
"""


RENAMED_COLUMNS = [("my_positions", "binance_check", "exchange_check")]


def _rename_columns(conn: sqlite3.Connection) -> None:
    for table, old, new in RENAMED_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if old in cols and new not in cols:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """One-off data moves between versions (safe to run every start)."""
    with conn:
        conn.execute("UPDATE my_positions SET source = 'synced' WHERE source = 'binance'")
        for old in ("binance_holdings", "binance_trades", "binance_orders"):  # replaced by account_* tables
            conn.execute(f"DROP TABLE IF EXISTS {old}")
        # OKX is the only exchange now: drop Binance-era market data and settings (all re-fetched from OKX).
        conn.execute("DELETE FROM prefs WHERE key IN ('binance_status', 'exchange')")
        if conn.execute("SELECT 1 FROM markets WHERE pair NOT LIKE '%-%' LIMIT 1").fetchone():
            for table in ("markets", "movers", "pumps"):
                conn.execute(f"DELETE FROM {table}")
        # The single-user account mirror became per-user (user_holdings/trades/orders); it's re-fetched from OKX.
        for old in ("account_holdings", "account_trades", "account_orders"):
            conn.execute(f"DROP TABLE IF EXISTS {old}")
        conn.execute("DELETE FROM prefs WHERE key = 'account_status'")


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Tiny migration: add columns that newer versions of SCHEMA declare but an older database lacks."""
    scratch = sqlite3.connect(":memory:")
    scratch.executescript(SCHEMA)
    for (table,) in scratch.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for cid, name, type_, notnull, default, pk in scratch.execute(f"PRAGMA table_info({table})"):
            if name not in have:
                ddl = f"ALTER TABLE {table} ADD COLUMN {name} {type_}"
                if default is not None:
                    ddl += f" DEFAULT {default}"
                if notnull and default is not None:
                    ddl += " NOT NULL"
                conn.execute(ddl)
    conn.commit()
    scratch.close()


def replace_source(
    conn: sqlite3.Connection,
    source: str,
    stats: list[TraderStat],
    followed: set[tuple[str, str]],
    positions: list[Position],
    ts: int,
) -> None:
    """Swap in one source's fresh traders and positions. `followed` holds (window, address) pairs."""
    with conn:
        conn.execute("DELETE FROM trader_stats WHERE source = ?", (source,))
        conn.execute("DELETE FROM positions WHERE source = ?", (source,))
        conn.executemany(
            f"INSERT INTO trader_stats ({', '.join(_STAT_COLS)}, followed, updated_at) "
            f"VALUES ({', '.join('?' * len(_STAT_COLS))}, ?, ?)",
            [[getattr(s, c) for c in _STAT_COLS] + [int((s.window, s.address) in followed), ts] for s in stats],
        )
        conn.executemany(
            f"INSERT INTO positions ({', '.join(_POS_COLS)}, updated_at) VALUES ({', '.join('?' * len(_POS_COLS))}, ?)",
            [list(asdict(p).values()) + [ts] for p in positions],
        )


def replace_positions(conn: sqlite3.Connection, source: str, positions: list[Position], fetched: set[str],
                      ts: int) -> None:
    """Swap in fresh positions for the traders in `fetched`. Traders whose request failed keep their last known
    positions (a failed request must not look like a sell-off)."""
    with conn:
        conn.executemany("DELETE FROM positions WHERE source = ? AND address = ?", [(source, a) for a in fetched])
        conn.executemany(
            f"INSERT INTO positions ({', '.join(_POS_COLS)}, updated_at) VALUES ({', '.join('?' * len(_POS_COLS))}, ?)",
            [list(asdict(p).values()) + [ts] for p in positions if p.address in fetched],
        )


def load_positions(conn: sqlite3.Connection) -> list[Position]:
    return [Position(**{c: row[c] for c in _POS_COLS}) for row in conn.execute("SELECT * FROM positions")]


def load_followed_scores(conn: sqlite3.Connection, window: str) -> dict[tuple[str, str], float]:
    rows = conn.execute("SELECT source, address, score FROM trader_stats WHERE window = ? AND followed = 1", (window,))
    return {(r["source"], r["address"]): r["score"] for r in rows}


# How much each window counts toward a trader's overall score.
COMPOSITE_WEIGHTS = {"day": 0.15, "week": 0.35, "month": 0.35, "allTime": 0.15}


def load_composite_scores(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Blend each followed trader's window scores into one number."""
    case = " ".join(f"WHEN '{w}' THEN {x}" for w, x in COMPOSITE_WEIGHTS.items())
    rows = conn.execute(f"SELECT source, address, SUM(score * CASE window {case} ELSE 0 END) AS s FROM trader_stats GROUP BY 1, 2")
    return {(r["source"], r["address"]): r["s"] for r in rows if r["s"] > 0}


def save_positioning(conn: sqlite3.Connection, rows: list[Positioning], ts: int) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO positioning VALUES (?, ?, ?, ?, ?, ?)",
            [(p.exchange, p.symbol, p.long_share, p.long_share_24h, p.funding, ts) for p in rows],
        )


def load_positioning(conn: sqlite3.Connection, max_age_s: int, now: int) -> dict[str, list[Positioning]]:
    out: dict[str, list[Positioning]] = {}
    for r in conn.execute("SELECT * FROM positioning WHERE updated_at >= ?", (now - max_age_s,)):
        out.setdefault(r["symbol"], []).append(
            Positioning(r["exchange"], r["symbol"], r["long_share"], r["long_share_24h"], r["funding"])
        )
    return out


def save_markets(conn: sqlite3.Connection, markets: dict, ts: int) -> None:
    """Upsert prices for every coin; trend fields only overwrite when freshly computed."""
    with conn:
        conn.executemany(
            """INSERT INTO markets (coin, pair, price, bid, ask, volume_usd, ma20, ma50, ret30, daily_vol, trend_at, updated_at,
                 change_24h)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(coin) DO UPDATE SET pair = excluded.pair, price = excluded.price, bid = excluded.bid,
                 ask = excluded.ask, volume_usd = excluded.volume_usd, updated_at = excluded.updated_at,
                 change_24h = COALESCE(excluded.change_24h, change_24h),
                 ma20 = COALESCE(excluded.ma20, ma20), ma50 = COALESCE(excluded.ma50, ma50),
                 ret30 = COALESCE(excluded.ret30, ret30), daily_vol = COALESCE(excluded.daily_vol, daily_vol),
                 trend_at = COALESCE(excluded.trend_at, trend_at)""",
            [(m.coin, m.pair, m.price, m.bid, m.ask, m.volume_usd, m.ma20, m.ma50, m.ret30, m.daily_vol,
              ts if m.ma50 is not None else None, ts, m.change_24h) for m in markets.values()],
        )


def load_markets(conn: sqlite3.Connection) -> dict:
    from .market import Market

    out = {}
    for r in conn.execute("SELECT * FROM markets"):
        out[r["coin"]] = Market(r["coin"], r["pair"], r["price"], r["bid"], r["ask"], r["volume_usd"],
                                r["change_24h"], r["ma20"], r["ma50"], r["ret30"], r["daily_vol"])
    return out


def trend_ages(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT coin, trend_at FROM markets WHERE trend_at IS NOT NULL")}


def save_drawdowns(conn: sqlite3.Connection, source: str, dds: dict[str, float], ts: int) -> None:
    with conn:
        conn.executemany("INSERT OR REPLACE INTO trader_quality VALUES (?, ?, ?, ?)",
                         [(source, a, d, ts) for a, d in dds.items()])


def load_drawdowns(conn: sqlite3.Connection, source: str) -> dict[str, tuple[float, int]]:
    rows = conn.execute("SELECT address, max_drawdown, updated_at FROM trader_quality WHERE source = ?", (source,))
    return {r[0]: (r[1], r[2]) for r in rows}


def set_status(conn: sqlite3.Connection, source: str, **values) -> None:
    cols = ", ".join(values)
    updates = ", ".join(f"{k} = excluded.{k}" for k in values)
    with conn:
        conn.execute(
            f"INSERT INTO source_status (source, {cols}) VALUES (?, {', '.join('?' * len(values))}) "
            f"ON CONFLICT(source) DO UPDATE SET {updates}",
            [source, *values.values()],
        )


def get_pref(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM prefs WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_pref(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO prefs VALUES (?, ?)", (key, value))
