"""The app's own API: status, the OKX account mirror, the trend bot, the long/short paper test and settings.
Per-user data (bot account, OKX account, settings) is scoped to the logged-in user; the long/short test is shared."""

import asyncio
import json
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from . import app_settings, db, longshort, lsmodel, trendbot, users
from .accounts import OkxAccount, UnsafeKeyError
from .logs import audit
from .pipeline import account_status, user_fee
from .web import Ctx, client_ip, ctx, current_user, require_admin

router = APIRouter(prefix="/api")


def _now() -> int:
    return int(time.time())


# --- status ------------------------------------------------------------------------

@router.get("/status")
async def status(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    rows = [dict(r) for r in c.conn.execute("SELECT source, last_ok, last_error FROM source_status ORDER BY source")]
    acct = account_status(c.conn, user.id)
    return {
        "user": user.public(),
        "updated_at": max((r["last_ok"] or 0 for r in rows), default=0) or None,
        "errors": [{"source": r["source"], "last_error": r["last_error"]} for r in rows if r["last_error"]],
        "exchange": {"name": c.pipeline.spot.name, "label": c.pipeline.spot.label},
        "account": {
            "configured": users.okx_credentials(c.conn, user.id) is not None,
            "ok": bool(acct and acct.get("ok")),
            "error": acct.get("error") if acct and not acct.get("ok") else None,
            "synced_at": acct.get("synced_at") if acct else None,
            "can_trade": acct.get("can_trade") if acct else None,
        },
        "notifications": bool(users.get_setting(c.conn, user.id, "notify_url", "")),
    }


# --- OKX account (read-only mirror) ---------------------------------------------------

@router.get("/account")
async def account(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    if users.okx_credentials(c.conn, user.id) is None:
        return {"configured": False}
    q = lambda sql: [dict(r) for r in c.conn.execute(sql, (user.id,))]  # noqa: E731
    return {"configured": True, "status": account_status(c.conn, user.id),
            "holdings": q("SELECT * FROM user_holdings WHERE user_id = ? ORDER BY value_usd DESC"),
            "orders": q("SELECT * FROM user_orders WHERE user_id = ? ORDER BY time DESC"),
            "trades": q("SELECT * FROM user_trades WHERE user_id = ? ORDER BY time DESC LIMIT 25"),
            "trade_url": c.pipeline.spot.url("BTC").rsplit("/", 1)[0]}


@router.post("/account/sync")
async def account_sync(request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    """Quick re-read of the user's OKX account (e.g. right after placing an order)."""
    if users.okx_credentials(c.conn, user.id) is None:
        raise HTTPException(400, "Connect your OKX key in Settings first.")
    async with httpx.AsyncClient(timeout=30) as client:
        status = await c.pipeline.sync_account(client, user.id, _now())
    return {"status": status}


# --- trend bot ------------------------------------------------------------------------------

def _bot_prices(c: Ctx) -> dict[str, float]:
    markets = db.load_markets(c.conn)
    prices = {coin: markets[coin].price for coin in trendbot.COINS if coin in markets}
    return prices | {p.split("-")[0]: v for p, v in c.pipeline.stream.prices(trendbot.PAIRS).items()}


def _bot_out(c: Ctx, user: users.User) -> dict:
    now = _now()
    prices = _bot_prices(c)
    trades = [dict(r) for r in c.conn.execute(
        "SELECT ts, coin, side, qty, price, value_usd, fee_usd, reason FROM bot_trades WHERE user_id = ? "
        "ORDER BY ts DESC, id DESC LIMIT 100", (user.id,))]
    history = [[r[0], r[1], r[2]] for r in c.conn.execute(
        "SELECT ts, value, btc_price FROM bot_snapshots WHERE user_id = ? ORDER BY ts", (user.id,))]
    return {
        "strategy": {"name": trendbot.NAME, "coins": trendbot.COINS,
                     "lookbacks": trendbot.STRATEGY.lookbacks},
        "account": trendbot.value(c.conn, user.id, prices), "prices": prices,
        "signal": c.pipeline.bot_signal, "error": c.pipeline.bot_error,
        "next_check": trendbot.decision_day(now) + trendbot.DAY + trendbot.CHECK_AFTER_S,
        "trades": trades, "history": history, "backtest": c.pipeline.bot_backtest,
        "fee_rate": user_fee(c.conn, c.settings, user.id)[0], "slippage": c.settings.slippage,
        "default_balance": app_settings.get(c.conn, "default_demo_balance"),
    }


@router.get("/bot")
async def bot(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    return _bot_out(c, user)


class BotStart(BaseModel):
    balance: float = Field(ge=100, le=100_000_000)


@router.post("/bot/start")
async def bot_start(body: BotStart, request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    """Start the trend bot with a fresh demo account. Its first check runs right away."""
    if trendbot.account(c.conn, user.id) is not None:
        raise HTTPException(409, "The bot already has an account. Reset it first to start over.")
    trendbot.start(c.conn, user.id, body.balance, _now(), _bot_prices(c).get("BTC"))
    c.pipeline.bot_wake.set()
    audit(c.conn, "bot.started", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"mode": "demo", "balance": body.balance})
    return _bot_out(c, user)


class BotSwitch(BaseModel):
    enabled: bool


@router.put("/bot/enabled")
async def bot_enabled(body: BotSwitch, request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    """Pause (holdings are kept as they are, no more trades) or resume."""
    if trendbot.account(c.conn, user.id) is None:
        raise HTTPException(404, "Start the bot first.")
    trendbot.set_enabled(c.conn, user.id, body.enabled)
    if body.enabled:
        c.pipeline.bot_wake.set()
    audit(c.conn, "bot.resumed" if body.enabled else "bot.paused", user_id=user.id, username=user.username,
          ip=client_ip(request))
    return _bot_out(c, user)


@router.post("/bot/reset")
async def bot_reset(request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    """Delete the bot's demo account and its history. The final result is kept in the audit log."""
    old = trendbot.value(c.conn, user.id, _bot_prices(c))
    trendbot.reset(c.conn, user.id)
    audit(c.conn, "bot.reset", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"previous": {k: old[k] for k in ("start_balance", "total", "pnl_pct", "started_at")} if old else None})
    return _bot_out(c, user)


# --- long/short paper test (shared) ----------------------------------------------------------

def _ls_backtest(c: Ctx) -> dict | None:
    """The walk-forward backtest of this exact model and book (python -m app.manage ls-backtest), if it was run."""
    path = c.settings.history_path.with_name("ls_backtest.json")
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


async def _ls_out(c: Ctx, user: users.User) -> dict:
    now = _now()
    held = list(longshort.positions(c.conn))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            prices = await c.pipeline.ls_live_prices(client, held + ["BTC"])
    except httpx.HTTPError:
        prices = c.pipeline.ls_prices[1]
    days = [dict(r) for r in c.conn.execute("SELECT * FROM ls_days ORDER BY day DESC LIMIT 60")]
    for d in days:
        d["longs"], d["shorts"], d["ranking"] = json.loads(d["longs"]), json.loads(d["shorts"]), json.loads(d["ranking"])
    latest = days[0] if days else None
    for d in days[1:]:
        d.pop("ranking")
    return {
        "state": c.pipeline.ls_state, "error": c.pipeline.ls_error, "is_admin": user.is_admin,
        "account": longshort.value(c.conn, prices), "prices_at": int(c.pipeline.ls_prices[0]) or None,
        "model": c.pipeline.model_info(), "next_run": longshort.decision_day(now) + 2 * longshort.DAY + longshort.RUN_AFTER_S,
        "ranking": latest["ranking"] if latest else [], "ranking_day": latest["day"] if latest else None,
        "longs": latest["longs"] if latest else [], "shorts": latest["shorts"] if latest else [],
        "days": days,
        "trades": [dict(r) for r in c.conn.execute("SELECT * FROM ls_trades ORDER BY ts DESC, id DESC LIMIT 60")],
        "history": [[r[0], r[1], r[2]] for r in c.conn.execute("SELECT ts, equity, btc_price FROM ls_snapshots ORDER BY ts")],
        "rules": {"top_coins": longshort.TOP_COINS, "fraction": longshort.FRACTION, "keep": longshort.KEEP,
                  "cost": longshort.COST, "horizon_days": lsmodel.HORIZON, "features": len(lsmodel.FEATURES),
                  "retrain_days": 30, "run_after_s": longshort.RUN_AFTER_S},
        "backtest": _ls_backtest(c),
    }


@router.get("/longshort")
async def longshort_view(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    return await _ls_out(c, user)


@router.post("/longshort/reset")
async def longshort_reset(request: Request, admin: users.User = Depends(require_admin), c: Ctx = Depends(ctx)):
    """Start the shared paper test over (admins). The final result is kept in the audit log; the next run opens a
    fresh account right away."""
    held = list(longshort.positions(c.conn))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            prices = await c.pipeline.ls_live_prices(client, held + ["BTC"])
    except httpx.HTTPError:
        prices = {}
    old = longshort.value(c.conn, prices)
    longshort.reset(c.conn)
    audit(c.conn, "ls.reset", user_id=admin.id, username=admin.username, ip=client_ip(request),
          detail={"previous": {k: old[k] for k in ("start_balance", "equity", "pnl_pct", "started_at")} if old else None})
    c.pipeline.ls_next_try = 0.0
    c.pipeline.ls_wake.set()
    return await _ls_out(c, admin)


# --- settings ----------------------------------------------------------------------------

@router.get("/settings")
async def get_settings(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    key = users.get_secret(c.conn, user.id, "okx_api_key")
    updated = c.conn.execute("SELECT MAX(updated_at) FROM user_secrets WHERE user_id = ? AND name LIKE 'okx_%'",
                             (user.id,)).fetchone()[0]
    g = lambda k, d=None: users.get_setting(c.conn, user.id, k, d)  # noqa: E731
    return {
        "user": user.public(),
        "okx": {"configured": users.okx_credentials(c.conn, user.id) is not None,
                "key_hint": key[-4:] if key else None, "region": g("okx_region", "eea"), "updated_at": updated},
        "notify_url_set": bool(g("notify_url", "")),
        "fees": {"taker": g("fee_taker"), "maker": g("fee_maker"),
                 "okx_reported": (account_status(c.conn, user.id) or {}).get("fee_rate"),
                 "default_taker": c.settings.fee_rate, "default_maker": c.settings.maker_fee_rate},
    }


class Prefs(BaseModel):
    notify_url: str | None = Field(default=None, max_length=300)
    fee_taker: float | None = Field(default=None, ge=0, le=0.01)  # fractions: 0.002 = 0.20%
    fee_maker: float | None = Field(default=None, ge=0, le=0.01)

    @field_validator("notify_url")
    @classmethod
    def https_only(cls, v):
        if v and not v.startswith("https://"):
            raise ValueError("must start with https://")
        return v


@router.put("/settings/prefs")
async def put_prefs(body: Prefs, request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    changed = body.model_dump(exclude_none=True)
    for key, value in changed.items():
        users.set_setting(c.conn, user.id, key, value)
    if changed:
        audit(c.conn, "settings.changed", user_id=user.id, username=user.username, ip=client_ip(request),
              detail={k: ("(set)" if k == "notify_url" else v) for k, v in changed.items()})
    return {"fees": {"taker": users.get_setting(c.conn, user.id, "fee_taker"),
                     "maker": users.get_setting(c.conn, user.id, "fee_maker")}}


class OkxKey(BaseModel):
    api_key: str = Field(min_length=8, max_length=128)
    api_secret: str = Field(min_length=8, max_length=256)
    passphrase: str = Field(min_length=1, max_length=128)
    region: str = Field(default="eea", pattern="^(eea|global)$")


@router.put("/settings/okx")
async def put_okx(body: OkxKey, request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    """Test the key first; only a working, read-only key is saved (encrypted)."""
    clean = {k: v.strip().strip('"').strip("'") for k, v in body.model_dump().items()}
    account = OkxAccount(clean["api_key"], clean["api_secret"], clean["passphrase"], clean["region"])
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            check = await account.check(client)
    except UnsafeKeyError as e:
        audit(c.conn, "okx.key_rejected", user_id=user.id, username=user.username, ip=client_ip(request),
              level="warning", detail={"reason": "withdraw permission"})
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "OKX didn't accept this key. Check the key, secret, passphrase and region.")
    users.set_secret(c.conn, user.id, "okx_api_key", clean["api_key"])
    users.set_secret(c.conn, user.id, "okx_api_secret", clean["api_secret"])
    users.set_secret(c.conn, user.id, "okx_api_passphrase", clean["passphrase"])
    users.set_setting(c.conn, user.id, "okx_region", clean["region"])
    audit(c.conn, "okx.key_saved", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"key_hint": clean["api_key"][-4:], "region": clean["region"], "can_trade": check["can_trade"]})
    asyncio.create_task(_sync_soon(c, user.id))
    return {"ok": True, "can_trade": check["can_trade"], "key_hint": clean["api_key"][-4:]}


async def _sync_soon(c: Ctx, user_id: int) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await c.pipeline.sync_account(client, user_id, _now())


@router.post("/settings/okx/test")
async def test_okx(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    creds = users.okx_credentials(c.conn, user.id)
    if not creds:
        raise HTTPException(400, "No OKX key saved.")
    account = OkxAccount(creds["okx_api_key"], creds["okx_api_secret"], creds["okx_api_passphrase"], creds["okx_region"])
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            check = await account.check(client)
    except UnsafeKeyError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "OKX didn't accept the saved key. It may have been deleted or expired on OKX.")
    return {"ok": True, **check}


@router.delete("/settings/okx")
async def delete_okx(request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    users.delete_secrets(c.conn, user.id, users.OKX_SECRET_NAMES)
    with c.conn:
        for table in ("user_holdings", "user_orders"):
            c.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user.id,))
        c.conn.execute("DELETE FROM user_settings WHERE user_id = ? AND key = 'account_status'", (user.id,))
    audit(c.conn, "okx.key_removed", user_id=user.id, username=user.username, ip=client_ip(request))
    return {"ok": True}
