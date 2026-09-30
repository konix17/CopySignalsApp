"""Application log (rotating files) and the audit log (who did what, in the database).

Secrets never reach either: a filter masks anything that looks like a password, key, secret, passphrase,
token or session cookie before a line is written.
"""

import json
import logging
import logging.handlers
import re
import sqlite3
import time
from pathlib import Path

SENSITIVE = re.compile(
    r"""(?ix)
    (?P<key>["']?(?:password|passwd|passphrase|secret|api[_-]?key|token|csrf|cs_session|authorization|
                   ok-access-[a-z]+|x-mbx-apikey|totp|code)["']?\s*[:=]\s*)
    (?P<val>"[^"]*"|'[^']*'|[^\s,;&}]+)
    """
)


def redact(text: str) -> str:
    return SENSITIVE.sub(lambda m: f"{m.group('key')}***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(log_dir / "app.log", maxBytes=5_000_000, backupCount=5,
                                                        encoding="utf-8")
    console = logging.StreamHandler()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in (file_handler, console):
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


# --- Audit log -----------------------------------------------------------------

FAILED_LOGIN_ALERT = 10  # failed logins across all accounts within ALERT_WINDOW_S raise an admin alert
ALERT_WINDOW_S = 600


def audit(conn: sqlite3.Connection, action: str, *, user_id: int | None = None, username: str | None = None,
          detail: dict | None = None, ip: str | None = None, level: str = "info", now: int | None = None) -> None:
    """level: info | warning | alert (alerts are shown to admins and pushed if notifications are set up)."""
    payload = redact(json.dumps(detail, default=str)) if detail else None
    with conn:
        conn.execute(
            "INSERT INTO audit_log (ts, user_id, username, action, detail, ip, level) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now or int(time.time()), user_id, username, action, payload, ip, level),
        )
    if level in ("warning", "alert"):
        logging.getLogger("audit").warning("%s %s user=%s ip=%s %s", level.upper(), action, username, ip, payload or "")


def failed_logins_recent(conn: sqlite3.Connection, now: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'auth.login_failed' AND ts >= ?",
                        (now - ALERT_WINDOW_S,)).fetchone()[0]
