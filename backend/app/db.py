import sqlite3
import time
from pathlib import Path

SCHEMA = """
-- OKX spot market data: price and liquidity per coin (values OKX holdings).
CREATE TABLE IF NOT EXISTS markets (
    coin TEXT PRIMARY KEY, pair TEXT NOT NULL, price REAL NOT NULL, bid REAL, ask REAL, volume_usd REAL,
    ma20 REAL, ma50 REAL, ret30 REAL, daily_vol REAL, trend_at INTEGER, updated_at INTEGER NOT NULL,
    change_24h REAL
);
CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY, last_attempt INTEGER, last_ok INTEGER, last_error TEXT,
    n_traders INTEGER, n_positions INTEGER, duration_s REAL
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
-- App-wide values (admin defaults as "setting.<name>").
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

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    _migrate(conn, path)
    return conn


# Swing copies (following Hyperliquid/GMX traders, their paper track record, demo trades, position advice) were
# removed; their tables go, after a backup copy of the database is saved in data/backups/.
REMOVED_TABLES = ("trader_stats", "positions", "position_log", "trader_fetch", "trader_quality", "pick_trades",
                  "my_positions", "alerts", "demo_snapshots")
REMOVED_USER_SETTINGS = ("demo_mode", "autotrade", "bankroll", "demo_start_balance")


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    """One-off changes between versions (safe to run every start)."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    old = [t for t in REMOVED_TABLES if t in have]
    if old and path.name != ":memory:":
        backups = path.parent / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(backups / f"{path.stem}-before-removing-copies-{time.strftime('%Y%m%d-%H%M%S')}.db")
        with target:
            conn.backup(target)
        target.close()
    with conn:
        for t in old:
            conn.execute(f"DROP TABLE {t}")
        conn.execute(f"DELETE FROM user_settings WHERE key IN ({','.join('?' * len(REMOVED_USER_SETTINGS))})",
                     REMOVED_USER_SETTINGS)
        conn.execute("DELETE FROM prefs WHERE key IN ('setting.refresh_minutes', 'setting.default_bankroll', "
                     "'last_manual_refresh')")


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
