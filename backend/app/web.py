"""Shared web plumbing: app context, security middleware, auth dependencies, error handling.

Every /api route requires a logged-in session except login itself. Every state-changing request needs the
CSRF token that belongs to the session (sent in the X-CSRF-Token header) and a same-origin Origin header.
"""

import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from . import users
from .config import Settings
from .security import RateLimiter, same

log = logging.getLogger("web")

SESSION_COOKIE = "cs_session"
CSRF_COOKIE = "cs_csrf"
CSRF_HEADER = "x-csrf-token"
PUBLIC_API = {"/api/auth/login"}
# With a temporary password, only these work until the password is changed.
TEMPORARY_PASSWORD_API = {"/api/auth/me", "/api/auth/password", "/api/auth/logout", "/api/status"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
PROTECTED_PAGES = {"/", "/index.html"}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@dataclass
class Ctx:
    conn: sqlite3.Connection
    settings: Settings
    pipeline: Any
    login_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(10, 60))        # per IP
    username_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(5, 15 * 60))  # per unknown name
    api_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(600, 60))          # per IP


def ctx(request: Request) -> Ctx:
    return request.app.state.ctx


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:  # non-browser clients and some same-origin requests omit it; CSRF token still required
        return True
    origin_host = origin.split("://", 1)[-1]
    if origin_host == request.headers.get("host", ""):
        return True
    # Behind an HTTPS reverse proxy (e.g. Tailscale Serve on a server) the Host header can be the proxy's target
    # rather than the address in the browser. The app's own configured names count, over HTTPS on the default port.
    return origin.startswith("https://") and origin_host in request.app.state.ctx.settings.allowed_hosts


async def security_middleware(request: Request, call_next):
    c: Ctx = request.app.state.ctx
    path, method = request.url.path, request.method
    now = int(time.time())
    is_api = path.startswith("/api/")

    if is_api and not c.api_limiter.hit(client_ip(request)):
        return _headers(JSONResponse({"detail": "Too many requests. Slow down a little."}, status_code=429), request)

    session = users.load_session(c.conn, request.cookies.get(SESSION_COOKIE), now)
    request.state.session = session

    if is_api and path not in PUBLIC_API:
        if session is None:
            return _headers(JSONResponse({"detail": "Please log in."}, status_code=401), request)
        if session.user.must_change_password and path not in TEMPORARY_PASSWORD_API:
            return _headers(JSONResponse({"detail": "Change your temporary password first.",
                                          "must_change_password": True}, status_code=403), request)
        if method in UNSAFE:
            if not _same_origin(request) or not same(request.headers.get(CSRF_HEADER), session.csrf_token):
                return _headers(JSONResponse({"detail": "This request was blocked (security check failed). "
                                                        "Reload the page and try again."}, status_code=403), request)
    elif is_api and method in UNSAFE and not _same_origin(request):
        return _headers(JSONResponse({"detail": "Cross-site request blocked."}, status_code=403), request)

    if not is_api:
        if path in PROTECTED_PAGES and session is None:
            return _headers(RedirectResponse("/login.html", status_code=303), request)
        if path == "/login.html" and session is not None:
            return _headers(RedirectResponse("/", status_code=303), request)

    response = await call_next(request)
    return _headers(response, request)


def _headers(response, request: Request):
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.app.state.ctx.settings.https:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    h["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


# --- dependencies ------------------------------------------------------------------

def current_session(request: Request) -> users.Session:
    session = getattr(request.state, "session", None)
    if session is None:  # the middleware already blocks this; fail closed anyway
        raise HTTPException(401, "Please log in.")
    return session


def current_user(session: users.Session = Depends(current_session)) -> users.User:
    return session.user


def require_admin(user: users.User = Depends(current_user)) -> users.User:
    if not user.is_admin:
        raise HTTPException(403, "Admins only.")
    return user


def set_login_cookies(response, token: str, csrf: str, secure: bool) -> None:
    common = {"samesite": "strict", "secure": secure, "path": "/", "max_age": users.SESSION_MAX_S}
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)  # read by the page, sent back as a header


def clear_login_cookies(response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


# --- errors ------------------------------------------------------------------------

async def validation_error(request: Request, exc: RequestValidationError):
    """Say which field is wrong without echoing what was typed (it may be a password or key)."""
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        problems.append(f"{loc}: {err.get('msg', 'invalid value')}")
    return _headers(JSONResponse({"detail": "Invalid input. " + "; ".join(problems[:5])}, status_code=422), request)


async def unexpected_error(request: Request, exc: Exception):
    """Fail closed with a reference number; the details only go to the log."""
    ref = secrets.token_hex(4)
    log.exception("unhandled error %s on %s %s", ref, request.method, request.url.path)
    return _headers(JSONResponse({"detail": f"Something went wrong (reference {ref})."}, status_code=500), request)
