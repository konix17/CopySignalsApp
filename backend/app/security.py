"""Security building blocks: password hashing, tokens, encryption at rest, TOTP, rate limiting.

- Passwords: Argon2id (argon2-cffi defaults), rehashed automatically when the parameters change.
- Session tokens: 256 random bits; only their SHA-256 is stored, so a leaked database can't be used to log in.
- Secrets at rest (exchange API keys, TOTP seeds): Fernet (AES-128-CBC + HMAC-SHA256) with a key kept in its
  own file, readable only by the owner.
- TOTP: RFC 6238 (30 s steps, 6 digits, SHA-1), as used by every authenticator app.
"""

import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so a wrong username takes as long as a wrong password.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

PASSWORD_MIN, PASSWORD_MAX = 12, 256


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def password_problem(password: str, username: str = "") -> str | None:
    """None if the password is acceptable, otherwise the reason in plain words."""
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"Use at most {PASSWORD_MAX} characters."
    if username and username.lower() in password.lower():
        return "Don't include your username in the password."
    if len(set(password)) < 5:
        return "Use a less repetitive password."
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


# --- Encryption at rest --------------------------------------------------------

class SecretBox:
    """Encrypts small secrets with a key stored in `key_path` (created on first use, mode 600)."""

    def __init__(self, key_path: Path):
        self.key_path = key_path
        if not key_path.exists():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        os.chmod(key_path, 0o600)
        self._fernet = Fernet(key_path.read_bytes().strip())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    return "•" * max(4, len(value) - keep) + value[-keep:]


# --- TOTP (two-step login) -----------------------------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret: str, at: float | None = None) -> str:
    return _totp_at(secret, int((at or time.time()) // 30))


def verify_totp(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    """Accepts the current code and one step either side (clock drift)."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return False
    step = int((at or time.time()) // 30)
    return any(hmac.compare_digest(_totp_at(secret, step + d), code) for d in range(-window, window + 1))


def totp_uri(secret: str, username: str, issuer: str = "Copy Signals") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}"


# --- Rate limiting -------------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter kept in memory: at most `limit` hits per `window_s` per key."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> bool:
        """Record a hit; False if the key is over its limit."""
        now = now or time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True
