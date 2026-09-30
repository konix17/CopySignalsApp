"""Admin panel API: users, sessions, audit log, system status, app settings. Admins only."""

import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import app_settings, users
from .logs import audit
from .web import Ctx, client_ip, ctx, require_admin

router = APIRouter(prefix="/api/admin")


def _now() -> int:
    return int(time.time())


# --- users -------------------------------------------------------------------------

@router.get("/users")
async def list_users(admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx)):
    rows = c.conn.execute(
        "SELECT u.id, u.username, u.role, u.disabled, u.created_at, u.last_login_at, u.locked_until, u.totp_enabled, "
        "(SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS sessions, "
        "(SELECT 1 FROM user_secrets k WHERE k.user_id = u.id AND k.name = 'okx_api_key') AS okx "
        "FROM users u ORDER BY u.id").fetchall()
    return [dict(r) | {"okx": bool(r["okx"]), "locked": bool(r["locked_until"] and r["locked_until"] > _now())}
            for r in rows]


class NewUser(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=512)
    role: str = Field(default="user", pattern="^(admin|user)$")


@router.post("/users")
async def create_user(body: NewUser, request: Request, admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx)):
    try:
        user = users.create(c.conn, body.username, body.password, body.role)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    audit(c.conn, "admin.user_created", user_id=admin.id, username=admin.username, ip=client_ip(request),
          detail={"new_user": user.username, "role": user.role})
    return user.public()


class UserChange(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    disabled: bool | None = None
    new_password: str | None = Field(default=None, min_length=1, max_length=512)
    unlock: bool | None = None


@router.patch("/users/{uid}")
async def change_user(uid: int, body: UserChange, request: Request, admin: users.User = Depends(require_admin),
                      c: Ctx = Depends(ctx)):
    target = users.get(c.conn, uid)
    if target is None:
        raise HTTPException(404, "Not found.")
    removing_admin = target.is_admin and (body.role == "user" or body.disabled)
    if removing_admin and users.count_admins(c.conn) <= 1:
        raise HTTPException(400, "There must always be at least one active admin.")
    if uid == admin.id and (body.disabled or body.role == "user"):
        raise HTTPException(400, "You can't disable or demote your own account.")
    done = {}
    with c.conn:
        if body.role is not None:
            c.conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, uid))
            done["role"] = body.role
        if body.disabled is not None:
            c.conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), uid))
            done["disabled"] = body.disabled
        if body.unlock:
            c.conn.execute("UPDATE users SET locked_until = NULL, failed_logins = 0 WHERE id = ?", (uid,))
            done["unlocked"] = True
    if body.new_password is not None:
        try:
            users.set_password(c.conn, uid, body.new_password)
        except users.UserError as e:
            raise HTTPException(400, str(e))
        done["password_reset"] = True
    if body.disabled or body.new_password is not None or body.role is not None:
        done["sessions_ended"] = users.end_user_sessions(c.conn, uid)
    audit(c.conn, "admin.user_changed", user_id=admin.id, username=admin.username, ip=client_ip(request),
          level="warning" if (body.role or body.disabled or body.new_password) else "info",
          detail={"target": target.username, **done})
    return {"ok": True, **done}


# --- sessions ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_sessions(admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx)):
    rows = c.conn.execute(
        "SELECT substr(s.token_hash, 1, 12) AS id, s.user_id, u.username, s.created_at, s.last_seen, s.expires_at, "
        "s.ip, s.user_agent FROM sessions s JOIN users u ON u.id = s.user_id ORDER BY s.last_seen DESC").fetchall()
    return [dict(r) for r in rows]


@router.delete("/sessions/{sid}")
async def end_session(sid: str, request: Request, admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx)):
    if len(sid) != 12 or not all(ch in "0123456789abcdef" for ch in sid):
        raise HTTPException(400, "Invalid session id.")
    with c.conn:
        cur = c.conn.execute("DELETE FROM sessions WHERE substr(token_hash, 1, 12) = ?", (sid,))
    audit(c.conn, "admin.session_ended", user_id=admin.id, username=admin.username, ip=client_ip(request),
          detail={"session": sid, "ended": cur.rowcount})
    return {"ended": cur.rowcount}


# --- audit log ----------------------------------------------------------------------------

@router.get("/audit")
async def audit_log(admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx),
                    username: str | None = Query(default=None, max_length=32),
                    action: str | None = Query(default=None, max_length=64),
                    level: str | None = Query(default=None, pattern="^(info|warning|alert)$"),
                    since: int | None = None, until: int | None = None,
                    limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    where, args = [], []
    if username:
        where.append("username = ? COLLATE NOCASE")
        args.append(username)
    if action:
        where.append("action LIKE ?")
        args.append(action.replace("%", "").replace("_", r"\_") + "%")
        where[-1] += r" ESCAPE '\'"
    if level:
        where.append("level = ?")
        args.append(level)
    if since:
        where.append("ts >= ?")
        args.append(since)
    if until:
        where.append("ts <= ?")
        args.append(until)
    sql = "FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "")
    total = c.conn.execute(f"SELECT COUNT(*) {sql}", args).fetchone()[0]
    rows = c.conn.execute(f"SELECT * {sql} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


# --- system status and settings ---------------------------------------------------------------

@router.get("/status")
async def system_status(admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx)):
    now = _now()
    db_path = c.settings.db_path
    size = sum(os.path.getsize(p) for p in (db_path, f"{db_path}-wal") if os.path.exists(p))
    one = lambda sql, *a: c.conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "sources": [dict(r) for r in c.conn.execute("SELECT * FROM source_status ORDER BY source")],
        "refreshing": c.pipeline.running, "live_at": c.pipeline.live_at, "stream": c.pipeline.stream.status(),
        "db_bytes": size,
        "counts": {
            "users": one("SELECT COUNT(*) FROM users"),
            "active_sessions": one("SELECT COUNT(*) FROM sessions"),
            "followed_traders": one("SELECT COUNT(DISTINCT source || address) FROM trader_stats WHERE followed = 1"),
            "open_demo_trades": one("SELECT COUNT(*) FROM my_positions WHERE source = 'demo' AND status = 'open'"),
            "open_real_positions": one("SELECT COUNT(*) FROM my_positions WHERE source != 'demo' AND status = 'open'"),
            "tracked_copies_open": one("SELECT COUNT(*) FROM pick_trades WHERE status = 'open' AND style = 'copy'"),
            "tracked_copies_closed": one("SELECT COUNT(*) FROM pick_trades WHERE status = 'closed' AND style = 'copy'"),
        },
        "security": {
            "alerts_24h": one("SELECT COUNT(*) FROM audit_log WHERE level = 'alert' AND ts >= ?", now - 86400),
            "warnings_24h": one("SELECT COUNT(*) FROM audit_log WHERE level = 'warning' AND ts >= ?", now - 86400),
            "failed_logins_24h": one("SELECT COUNT(*) FROM audit_log WHERE action = 'auth.login_failed' AND ts >= ?",
                                     now - 86400),
            "locked_accounts": one("SELECT COUNT(*) FROM users WHERE locked_until > ?", now),
        },
    }


@router.get("/settings")
async def get_app_settings(admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx)):
    return app_settings.all_values(c.conn)


class SettingChange(BaseModel):
    values: dict[str, float] = Field(max_length=10)


@router.put("/settings")
async def put_app_settings(body: SettingChange, request: Request, admin: users.User = Depends(require_admin),
                           c: Ctx = Depends(ctx)):
    try:
        for name, value in body.values.items():
            app_settings.set_value(c.conn, name, value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(c.conn, "admin.settings_changed", user_id=admin.id, username=admin.username, ip=client_ip(request),
          detail=body.values)
    return app_settings.all_values(c.conn)
