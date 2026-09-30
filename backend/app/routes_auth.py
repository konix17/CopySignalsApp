"""Login, logout, password change and two-step login (TOTP)."""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import security, users
from .logs import FAILED_LOGIN_ALERT, audit, failed_logins_recent
from .web import (Ctx, clear_login_cookies, client_ip, ctx, current_session, current_user, set_login_cookies,
                  SESSION_COOKIE)

router = APIRouter(prefix="/api/auth")
GENERIC = "Wrong username or password."


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)
    code: str | None = Field(default=None, max_length=12)


@router.post("/login")
async def login(body: Login, request: Request, c: Ctx = Depends(ctx)):
    now, ip = int(time.time()), client_ip(request)
    if not c.login_limiter.hit(ip):
        audit(c.conn, "auth.rate_limited", username=body.username, ip=ip, level="warning", now=now)
        raise HTTPException(429, "Too many login attempts. Wait a minute and try again.")

    result = users.check_login(c.conn, body.username, body.password, body.code, now)
    if result.user is None and not c.username_limiter.hit(body.username.lower()):
        result = users.LoginResult(None, "locked")  # unknown names lock the same way real ones do

    if result.reason == "ok":
        token, csrf = users.start_session(c.conn, result.user, ip, request.headers.get("user-agent"), now)
        audit(c.conn, "auth.login", user_id=result.user.id, username=result.user.username, ip=ip, now=now)
        response = JSONResponse({"user": result.user.public()})
        set_login_cookies(response, token, csrf, c.settings.https)
        return response

    if result.reason == "code_required":
        return JSONResponse({"detail": "Enter the 6-digit code from your authenticator app.", "code_required": True},
                            status_code=401)
    uid = result.user.id if result.user else None
    level = "warning" if result.reason in ("locked", "disabled") else "info"
    audit(c.conn, "auth.login_failed", user_id=uid, username=body.username, ip=ip, level=level, now=now,
          detail={"reason": result.reason})
    if failed_logins_recent(c.conn, now) >= FAILED_LOGIN_ALERT and not c.conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'security.many_failed_logins' AND ts >= ?", (now - 600,)).fetchone():
        audit(c.conn, "security.many_failed_logins", ip=ip, level="alert", now=now,
              detail={"failed_last_10_min": failed_logins_recent(c.conn, now)})
    if result.reason == "locked":
        raise HTTPException(429, "Too many failed attempts. This account is locked for 15 minutes.")
    if result.reason == "bad_code":
        raise HTTPException(401, "That code isn't right. Check your authenticator app and try again.")
    raise HTTPException(401, GENERIC)


@router.post("/logout")
async def logout(request: Request, session: users.Session = Depends(current_session), c: Ctx = Depends(ctx)):
    users.end_session(c.conn, request.cookies.get(SESSION_COOKIE, ""))
    audit(c.conn, "auth.logout", user_id=session.user.id, username=session.user.username, ip=client_ip(request))
    response = JSONResponse({"ok": True})
    clear_login_cookies(response)
    return response


@router.get("/me")
async def me(session: users.Session = Depends(current_session)):
    return {"user": session.user.public(), "csrf": session.csrf_token, "expires_at": session.expires_at,
            "idle_timeout_s": users.SESSION_IDLE_S}


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=512)
    new: str = Field(min_length=1, max_length=512)


@router.post("/password")
async def change_password(body: PasswordChange, request: Request, session: users.Session = Depends(current_session),
                          c: Ctx = Depends(ctx)):
    user = session.user
    row = users.by_name(c.conn, user.username)
    if not security.verify_password(row["password_hash"], body.current):
        audit(c.conn, "auth.password_change_failed", user_id=user.id, username=user.username, ip=client_ip(request),
              level="warning")
        raise HTTPException(400, "Your current password isn't right.")
    if body.new == body.current:
        raise HTTPException(400, "Choose a password different from the current one.")
    try:
        users.set_password(c.conn, user.id, body.new)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    ended = users.end_user_sessions(c.conn, user.id, keep_hash=session.token_hash)
    audit(c.conn, "auth.password_changed", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"other_sessions_ended": ended})
    return {"ok": True, "other_sessions_ended": ended}


# --- two-step login ---------------------------------------------------------------

@router.post("/2fa/setup")
async def totp_setup(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    secret = security.new_totp_secret()
    users.set_setting(c.conn, user.id, "totp_pending", users.box().encrypt(secret))
    return {"secret": secret, "uri": security.totp_uri(secret, user.username)}


class TotpCode(BaseModel):
    code: str = Field(min_length=6, max_length=12)


@router.post("/2fa/enable")
async def totp_enable(body: TotpCode, request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    pending = users.get_setting(c.conn, user.id, "totp_pending")
    secret = users.box().decrypt(pending) if pending else None
    if not secret or not security.verify_totp(secret, body.code):
        raise HTTPException(400, "That code isn't right. Make sure your phone's clock is correct and try again.")
    with c.conn:
        c.conn.execute("UPDATE users SET totp_secret = ?, totp_enabled = 1 WHERE id = ?", (pending, user.id))
        c.conn.execute("DELETE FROM user_settings WHERE user_id = ? AND key = 'totp_pending'", (user.id,))
    audit(c.conn, "auth.2fa_enabled", user_id=user.id, username=user.username, ip=client_ip(request))
    return {"ok": True}


class TotpDisable(BaseModel):
    password: str = Field(min_length=1, max_length=512)
    code: str = Field(min_length=6, max_length=12)


@router.post("/2fa/disable")
async def totp_disable(body: TotpDisable, request: Request, user: users.User = Depends(current_user),
                       c: Ctx = Depends(ctx)):
    row = users.by_name(c.conn, user.username)
    secret = users.code_secret(c.conn, row)
    if not security.verify_password(row["password_hash"], body.password) or not secret \
            or not security.verify_totp(secret, body.code):
        audit(c.conn, "auth.2fa_disable_failed", user_id=user.id, username=user.username, ip=client_ip(request),
              level="warning")
        raise HTTPException(400, "Password or code isn't right.")
    with c.conn:
        c.conn.execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0 WHERE id = ?", (user.id,))
    audit(c.conn, "auth.2fa_disabled", user_id=user.id, username=user.username, ip=client_ip(request), level="warning")
    return {"ok": True}
