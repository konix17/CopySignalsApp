"""Users, sessions, per-user settings and encrypted secrets."""

import json
import sqlite3
import time
from dataclasses import dataclass

from . import security

SESSION_IDLE_S = 30 * 60
SESSION_MAX_S = 12 * 3600
LOCK_AFTER = 5
LOCK_FOR_S = 15 * 60
ROLES = ("admin", "user")
OKX_SECRET_NAMES = ("okx_api_key", "okx_api_secret", "okx_api_passphrase")


@dataclass
class User:
    id: int
    username: str
    role: str
    disabled: bool
    totp_enabled: bool
    must_change_password: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role, "totp_enabled": self.totp_enabled,
                "must_change_password": self.must_change_password}


def _user(row) -> User:
    return User(row["id"], row["username"], row["role"], bool(row["disabled"]), bool(row["totp_enabled"]),
                bool(row["must_change_password"]))


class UserError(ValueError):
    pass


def get(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user(row) if row else None


def by_name(conn: sqlite3.Connection, username: str):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()


def create(conn: sqlite3.Connection, username: str, password: str, role: str = "user", now: int | None = None,
           temporary: bool = False) -> User:
    """`temporary=True` accepts a password that breaks the rules, but the user must change it before doing
    anything else (used for a first admin whose chosen password is too short)."""
    username = username.strip()
    if not (3 <= len(username) <= 32) or not all(c.isalnum() or c in "._-" for c in username):
        raise UserError("Usernames are 3–32 characters: letters, digits, dot, dash or underscore.")
    if role not in ROLES:
        raise UserError("Unknown role.")
    problem = security.password_problem(password, username)
    if problem and not temporary:
        raise UserError(problem)
    if not password:
        raise UserError("A password is required.")
    if by_name(conn, username):
        raise UserError("That username is taken.")
    now = now or int(time.time())
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, password_changed_at, must_change_password) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, security.hash_password(password), role, now, now, int(temporary)))
    return get(conn, cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str, now: int | None = None) -> None:
    user = get(conn, user_id)
    problem = security.password_problem(password, user.username if user else "")
    if problem:
        raise UserError(problem)
    with conn:
        conn.execute("UPDATE users SET password_hash = ?, password_changed_at = ?, failed_logins = 0, locked_until = NULL, "
                     "must_change_password = 0 WHERE id = ?", (security.hash_password(password), now or int(time.time()), user_id))


def count_admins(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0").fetchone()[0]


# --- Login ---------------------------------------------------------------------

@dataclass
class LoginResult:
    user: User | None
    reason: str  # ok | bad_credentials | locked | disabled | code_required | bad_code


def check_login(conn: sqlite3.Connection, username: str, password: str, code: str | None, now: int) -> LoginResult:
    """Verifies credentials with lockout. The caller gives every failure the same message to the client."""
    row = by_name(conn, username or "")
    ok = security.verify_password(row["password_hash"] if row else None, password or "")
    if row is None:
        return LoginResult(None, "bad_credentials")
    if row["locked_until"] and row["locked_until"] > now:
        return LoginResult(_user(row), "locked")
    if not ok:
        failed = row["failed_logins"] + 1
        locked_until = now + LOCK_FOR_S if failed >= LOCK_AFTER else None
        with conn:
            conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (0 if locked_until else failed, locked_until, row["id"]))
        return LoginResult(_user(row), "locked" if locked_until else "bad_credentials")
    if row["disabled"]:
        return LoginResult(_user(row), "disabled")
    if row["totp_enabled"]:
        if not code:
            return LoginResult(_user(row), "code_required")
        secret = code_secret(conn, row)
        if not secret or not security.verify_totp(secret, code):
            return LoginResult(_user(row), "bad_code")
    with conn:
        conn.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                     (now, row["id"]))
        if security.needs_rehash(row["password_hash"]):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (security.hash_password(password), row["id"]))
    return LoginResult(_user(row), "ok")


_box: security.SecretBox | None = None


def init_secrets(box: security.SecretBox) -> None:
    global _box
    _box = box


def box() -> security.SecretBox:
    if _box is None:
        raise RuntimeError("secret storage not initialised")
    return _box


def code_secret(conn, row) -> str | None:
    return box().decrypt(row["totp_secret"]) if row["totp_secret"] else None


# --- Sessions ------------------------------------------------------------------

@dataclass
class Session:
    token_hash: str
    user: User
    csrf_token: str
    expires_at: int


def start_session(conn: sqlite3.Connection, user: User, ip: str | None, user_agent: str | None, now: int) -> tuple[str, str]:
    """Returns (session token for the cookie, CSRF token)."""
    token, csrf = security.new_token(), security.new_token()
    with conn:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (security.token_hash(token), user.id, csrf, now, now, now + SESSION_MAX_S, ip,
                      (user_agent or "")[:200]))
    return token, csrf


def load_session(conn: sqlite3.Connection, token: str | None, now: int) -> Session | None:
    if not token:
        return None
    th = security.token_hash(token)
    row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (th,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= now or row["last_seen"] + SESSION_IDLE_S <= now:
        end_session(conn, token)
        return None
    user = get(conn, row["user_id"])
    if user is None or user.disabled:
        end_session(conn, token)
        return None
    if now - row["last_seen"] >= 30:  # keep writes rare
        with conn:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, th))
    return Session(th, user, row["csrf_token"], row["expires_at"])


def end_session(conn: sqlite3.Connection, token: str) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (security.token_hash(token),))


def end_user_sessions(conn: sqlite3.Connection, user_id: int, keep_hash: str | None = None) -> int:
    with conn:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep_hash or ""))
    return cur.rowcount


def purge_sessions(conn: sqlite3.Connection, now: int) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR last_seen + ? <= ?", (now, SESSION_IDLE_S, now))


# --- Per-user settings and secrets -----------------------------------------------

def get_setting(conn: sqlite3.Connection, user_id: int, key: str, default=None):
    row = conn.execute("SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, user_id: int, key: str, value) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO user_settings VALUES (?, ?, ?)", (user_id, key, json.dumps(value)))


def set_secret(conn: sqlite3.Connection, user_id: int, name: str, value: str, now: int | None = None) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO user_secrets VALUES (?, ?, ?, ?)",
                     (user_id, name, box().encrypt(value), now or int(time.time())))


def get_secret(conn: sqlite3.Connection, user_id: int, name: str) -> str | None:
    row = conn.execute("SELECT value_enc FROM user_secrets WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
    return box().decrypt(row[0]) if row else None


def delete_secrets(conn: sqlite3.Connection, user_id: int, names) -> None:
    with conn:
        conn.executemany("DELETE FROM user_secrets WHERE user_id = ? AND name = ?", [(user_id, n) for n in names])


def okx_credentials(conn: sqlite3.Connection, user_id: int) -> dict | None:
    values = {n: get_secret(conn, user_id, n) for n in OKX_SECRET_NAMES}
    if not all(values.values()):
        return None
    values["okx_region"] = get_setting(conn, user_id, "okx_region", "eea")
    return values


def users_with_okx(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT user_id FROM user_secrets WHERE name = 'okx_api_key' "
                        "AND user_id IN (SELECT id FROM users WHERE disabled = 0)")
    return [r[0] for r in rows]


def all_active(conn: sqlite3.Connection) -> list[User]:
    return [_user(r) for r in conn.execute("SELECT * FROM users WHERE disabled = 0")]


def adopt_orphans(conn: sqlite3.Connection, user_id: int) -> dict:
    """Give data created before accounts existed (positions, alerts, old settings) to `user_id`."""
    with conn:
        moved = conn.execute("UPDATE my_positions SET user_id = ? WHERE user_id IS NULL", (user_id,)).rowcount
        conn.execute("UPDATE alerts SET user_id = (SELECT user_id FROM my_positions p WHERE p.id = alerts.position_id) "
                     "WHERE user_id IS NULL")
    carried = {}
    for key in ("bankroll",):
        row = conn.execute("SELECT value FROM prefs WHERE key = ?", (key,)).fetchone()
        if row:
            set_setting(conn, user_id, key, float(row[0]))
            carried[key] = float(row[0])
            with conn:
                conn.execute("DELETE FROM prefs WHERE key = ?", (key,))
    return {"positions": moved, "settings": carried}
