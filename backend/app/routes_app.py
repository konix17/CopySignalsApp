"""The app's own API: picks, rising now, portfolio (real and demo), the trend bot, alerts, settings. Everything here is scoped
to the logged-in user; shared market data is the same for everyone."""

import asyncio
import dataclasses
import json
import time
from dataclasses import asdict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from . import app_settings, autotrade, db, movers, portfolio, pumps, tracker, trendbot, users
from .accounts import OkxAccount, UnsafeKeyError
from .logs import audit
from .pipeline import account_status, bankroll_info, market_view, reference_bankroll, user_fee
from .web import Ctx, client_ip, ctx, current_user

router = APIRouter(prefix="/api")
REFRESH_THROTTLE_S = 60


def _now() -> int:
    return int(time.time())


def _trade_url(c: Ctx, coin: str) -> str:
    return c.pipeline.spot.url(coin)


def _open_symbols(c: Ctx, user_id: int, demo: bool) -> set[str]:
    return {r[0] for r in c.conn.execute(
        f"SELECT symbol FROM my_positions WHERE user_id = ? AND status = 'open' AND source {'=' if demo else '!='} 'demo'",
        (user_id,))}


def _pick_out(c: Ctx, p, user_id: int, held: set[str], demo: set[str]) -> dict:
    return asdict(p) | {"held": p.symbol in held, "demo_running": p.symbol in demo, "trade_url": _trade_url(c, p.symbol),
                        "qty": p.size_usd / p.price if p.price else None}


def _rescaled(p, size: float):
    """A copy of a shared rising-now pick resized for one user's high-risk budget."""
    factor = size / p.size_usd if p.size_usd else 0
    return type(p)(**{**asdict(p), "checks": p.checks, "size_usd": size,
                      "net_win_usd": round(p.net_win_usd * factor, 2), "net_loss_usd": round(p.net_loss_usd * factor, 2)})


# --- status ------------------------------------------------------------------------

@router.get("/status")
async def status(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    rows = [dict(r) for r in c.conn.execute("SELECT source, last_ok, last_error FROM source_status ORDER BY source")]
    now = _now()
    acct = account_status(c.conn, user.id)
    return {
        "user": user.public(),
        "refreshing": c.pipeline.running,
        "updated_at": max((r["last_ok"] or 0 for r in rows), default=0) or None,
        "errors": [{"source": r["source"], "last_error": r["last_error"]} for r in rows if r["last_error"]],
        "bankroll": bankroll_info(c.conn, c.settings, now, user.id),
        "exchange": {"name": c.pipeline.spot.name, "label": c.pipeline.spot.label},
        "account": {
            "configured": users.okx_credentials(c.conn, user.id) is not None,
            "ok": bool(acct and acct.get("ok")),
            "error": acct.get("error") if acct and not acct.get("ok") else None,
            "synced_at": acct.get("synced_at") if acct else None,
            "can_trade": acct.get("can_trade") if acct else None,
        },
        "demo_mode": bool(users.get_setting(c.conn, user.id, "demo_mode", False)),
        "notifications": bool(users.get_setting(c.conn, user.id, "notify_url", "")),
    }


@router.post("/refresh")
async def refresh(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    last = int(db.get_pref(c.conn, "last_manual_refresh", "0"))
    if c.pipeline.running or _now() - last < REFRESH_THROTTLE_S:
        return {"started": False}
    db.set_pref(c.conn, "last_manual_refresh", str(_now()))
    asyncio.create_task(c.pipeline.refresh())
    return {"started": True}


# --- picks -------------------------------------------------------------------------

@router.get("/picks")
async def picks(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    view = market_view(c.conn, c.settings, _now(), user.id)
    held, demo = _open_symbols(c, user.id, False), _open_symbols(c, user.id, True)
    return {
        "regime": view.regime,
        "bankroll": view.bankroll,
        "picks": [_pick_out(c, p, user.id, held, demo) for p in view.picks],
        "copies": [_pick_out(c, p, user.id, held, demo) for p in view.copies],
        "exiting": view.exiting,
    }


def _user_movers(c: Ctx, user_id: int, now: int):
    rising, earlier, info = movers.load(c.conn, now)
    bank = bankroll_info(c.conn, c.settings, now, user_id)
    size = movers.risky_size(bank["risk_budget"], bank["risk_budget_free"], c.settings.risky_trade_share)
    return [_rescaled(p, size) for p in rising], earlier, info, bank


@router.get("/movers")
async def rising(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    now = _now()
    rising_now, earlier, info, bank = _user_movers(c, user.id, now)
    states = pumps.phases(c.conn, now=now)
    held, demo = _open_symbols(c, user.id, False), _open_symbols(c, user.id, True)
    return {
        "scanned_at": c.pipeline.scan_at or int(db.get_pref(c.conn, "scan_at", "0")) or None,
        "budget": {k: bank[k] for k in ("amount", "risk_budget", "risk_budget_used", "risk_budget_free")}
                  | {"per_trade": bank["risk_budget"] * c.settings.risky_trade_share},
        "rising": [_pick_out(c, p, user.id, held, demo) | {"flag": info[p.symbol], "pump": states.get(p.symbol)}
                   for p in rising_now],
        "avoid": sorted((s for s in states.values() if s["phase"] in ("topping", "dumping")), key=lambda s: s["from_peak"]),
        "earlier": [e | {"pump": states.get(e["symbol"])} for e in earlier],
    }


def _find_pick(c: Ctx, user_id: int, symbol: str, view):
    """A current pick or a coin flagged as rising now, sized for this user."""
    pick = next((p for p in [*view.picks, *view.copies] if p.symbol == symbol), None)
    if pick is None:
        pick = next((p for p in _user_movers(c, user_id, _now())[0] if p.symbol == symbol), None)
    return pick


# --- positions (real) --------------------------------------------------------------

def _position_out(c: Ctx, r) -> dict:
    d = dict(r)
    d.pop("features", None)
    price = d["exit_price"] if d["status"] == "closed" else d["last_price"]
    d["pnl_pct"] = portfolio.pnl(r, price) if price else None
    d["value_usd"] = (d["qty"] or 0) * price if price else None
    d["pnl_usd"] = d["pnl_pct"] * d["size_usd"] if d["pnl_pct"] is not None else None
    d["advice_reasons"] = json.loads(d["advice_reasons"] or "[]")
    d["trade_url"] = _trade_url(c, d["symbol"])
    if d["status"] == "open":
        cost = d["cost_pct"] or 0
        live = d["last_price"] or d["entry_price"]
        d["stop_now"] = portfolio.effective_stop(d)  # rises with the price for trailing stops
        d["net_now"] = live / d["entry_price"] - 1 - cost
        d["if_target_usd"] = d["size_usd"] * (d["target_price"] / d["entry_price"] - 1 - cost)
        d["if_stop_usd"] = d["size_usd"] * (d["stop_now"] / d["entry_price"] - 1 - cost)
    return d


def _owned(c: Ctx, user_id: int, pid: int):
    row = c.conn.execute("SELECT * FROM my_positions WHERE id = ? AND user_id = ?", (pid, user_id)).fetchone()
    if row is None:  # same answer whether it doesn't exist or belongs to someone else
        raise HTTPException(404, "Not found.")
    return row


@router.get("/portfolio")
async def portfolio_view(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    rows = c.conn.execute(
        "SELECT * FROM my_positions WHERE user_id = ? AND source != 'demo' AND (status = 'open' OR closed_at >= ?) "
        "ORDER BY status DESC, COALESCE(closed_at, opened_at) DESC", (user.id, _now() - 30 * 86400)).fetchall()
    return {
        "account": portfolio.real_account(c.conn, user.id, account_status(c.conn, user.id)),
        "live_at": c.pipeline.live_at,
        "positions": [_position_out(c, r) for r in rows],
        "results_by_type": portfolio.results_by_type(c.conn, user.id, demo=False),
    }


class NewPosition(BaseModel):
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9]+$")
    size_usd: float = Field(gt=0, le=100_000_000)
    entry_price: float = Field(gt=0)


@router.post("/positions")
async def add_position(body: NewPosition, request: Request, user: users.User = Depends(current_user),
                       c: Ctx = Depends(ctx)):
    now = _now()
    view = market_view(c.conn, c.settings, now, user.id)
    pick = _find_pick(c, user.id, body.symbol, view)
    if not pick and body.symbol not in view.markets:
        raise HTTPException(404, "Unknown coin.")
    btc = view.markets.get("BTC")
    pid = portfolio.open_position(c.conn, user_id=user.id, market_key=f"perp:{body.symbol}", symbol=body.symbol,
                                  entry_price=body.entry_price, size_usd=body.size_usd, pick=pick, now=now,
                                  btc_entry=btc.price if btc else None,
                                  cost_pct=None if pick else 2 * view.bankroll["fee_rate"] + 0.001)
    audit(c.conn, "position.added", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"position": pid, "symbol": body.symbol, "size_usd": body.size_usd})
    return _position_out(c, c.conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone())


class ClosePosition(BaseModel):
    exit_price: float | None = Field(default=None, gt=0)


@router.post("/positions/{pid}/close")
async def close(pid: int, body: ClosePosition, request: Request, user: users.User = Depends(current_user),
                c: Ctx = Depends(ctx)):
    row = _owned(c, user.id, pid)
    if row["status"] != "open" or row["source"] not in ("manual", "demo"):
        raise HTTPException(400, "Only open positions you entered by hand or demo trades can be closed here.")
    markets = db.load_markets(c.conn)
    btc = markets.get("BTC")
    if row["source"] == "demo":  # demo trades always sell at the live price, so results can't be made up
        live = markets.get(row["symbol"])
        exit_price = (live.price if live else None) or row["last_price"] or row["entry_price"]
    else:
        exit_price = body.exit_price or row["last_price"]
    portfolio.close_position(c.conn, pid, exit_price, _now(), reason="sold", btc_price=btc.price if btc else None)
    audit(c.conn, "position.closed", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"position": pid, "symbol": row["symbol"]})
    return {"closed": pid}


@router.delete("/positions/{pid}")
async def delete(pid: int, request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    row = _owned(c, user.id, pid)
    if row["source"] == "synced":
        raise HTTPException(400, "Positions synced from OKX follow your OKX account and can't be deleted here.")
    with c.conn:
        c.conn.execute("DELETE FROM alerts WHERE position_id = ? AND user_id = ?", (pid, user.id))
        c.conn.execute("DELETE FROM my_positions WHERE id = ? AND user_id = ?", (pid, user.id))
    audit(c.conn, "position.deleted", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"position": pid, "symbol": row["symbol"], "source": row["source"]})
    return {"deleted": pid}


@router.get("/account")
async def account(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    if users.okx_credentials(c.conn, user.id) is None:
        return {"configured": False}
    q = lambda sql: [dict(r) for r in c.conn.execute(sql, (user.id,))]  # noqa: E731
    return {"configured": True, "status": account_status(c.conn, user.id),
            "holdings": q("SELECT * FROM user_holdings WHERE user_id = ? ORDER BY value_usd DESC"),
            "orders": q("SELECT * FROM user_orders WHERE user_id = ? ORDER BY time DESC"),
            "trades": q("SELECT * FROM user_trades WHERE user_id = ? ORDER BY time DESC LIMIT 25")}


@router.post("/account/sync")
async def account_sync(request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    """Quick re-read of the user's OKX account (e.g. right after placing an order)."""
    if users.okx_credentials(c.conn, user.id) is None:
        raise HTTPException(400, "Connect your OKX key in Settings first.")
    now = _now()
    async with httpx.AsyncClient(timeout=30) as client:
        status = await c.pipeline.sync_account(client, user.id, now)
    view = c.pipeline.last_view or market_view(c.conn, c.settings, now)
    portfolio.update_positions(c.conn, view.signals, view.positioning, view.markets, now)
    return {"status": status}


# --- demo ----------------------------------------------------------------------------

class DemoBuy(BaseModel):
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9]+$")
    size_usd: float = Field(gt=0, le=100_000_000)


@router.post("/demo")
async def demo_buy(body: DemoBuy, request: Request, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    """Buy a current pick with pretend money at the OKX price now, following the pick's plan."""
    now = _now()
    view = market_view(c.conn, c.settings, now, user.id)
    pick = _find_pick(c, user.id, body.symbol, view)
    if not pick:
        raise HTTPException(404, f"{body.symbol} is no longer a pick or rising right now.")
    if body.symbol in _open_symbols(c, user.id, True):
        raise HTTPException(409, f"A demo trade for {body.symbol} is already running.")
    cash = portfolio.demo_account(c.conn, user.id, autotrade.demo_start(c.conn, user.id))["cash"]
    if body.size_usd > cash + 0.01:
        raise HTTPException(400, f"Not enough demo cash: ${cash:,.2f} available.")
    btc = view.markets.get("BTC")
    pid = portfolio.open_position(c.conn, user_id=user.id, market_key=pick.market_key, symbol=pick.symbol,
                                  entry_price=pick.price, size_usd=body.size_usd, pick=pick, now=now, source="demo",
                                  btc_entry=btc.price if btc else None)
    audit(c.conn, "demo.buy", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"position": pid, "symbol": pick.symbol, "size_usd": body.size_usd, "type": pick.strength})
    return _position_out(c, c.conn.execute("SELECT * FROM my_positions WHERE id = ?", (pid,)).fetchone())


@router.get("/demo")
async def demo(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    rows = c.conn.execute("SELECT * FROM my_positions WHERE user_id = ? AND source = 'demo' "
                          "ORDER BY status DESC, COALESCE(closed_at, opened_at) DESC", (user.id,)).fetchall()
    history = [dict(r) for r in c.conn.execute(
        "SELECT ts, value, btc_price FROM demo_snapshots WHERE user_id = ? ORDER BY ts", (user.id,))]
    return {"account": portfolio.demo_account(c.conn, user.id, autotrade.demo_start(c.conn, user.id)),
            "live_at": c.pipeline.live_at, "trades": [_position_out(c, r) for r in rows],
            "autotrade": autotrade.config(c.conn, user.id),
            "results_by_type": portfolio.results_by_type(c.conn, user.id, demo=True), "history": history}


class DemoReset(BaseModel):
    start_balance: float = Field(gt=0, le=100_000_000)


@router.post("/demo/reset")
async def demo_reset(body: DemoReset, request: Request, user: users.User = Depends(current_user),
                     c: Ctx = Depends(ctx)):
    """Start the demo account over. The old results are kept in the audit log first."""
    old = portfolio.demo_account(c.conn, user.id, autotrade.demo_start(c.conn, user.id))
    by_type = portfolio.results_by_type(c.conn, user.id, demo=True)
    with c.conn:
        c.conn.execute("DELETE FROM alerts WHERE user_id = ? AND position_id IN "
                       "(SELECT id FROM my_positions WHERE user_id = ? AND source = 'demo')", (user.id, user.id))
        c.conn.execute("DELETE FROM my_positions WHERE user_id = ? AND source = 'demo'", (user.id,))
        c.conn.execute("DELETE FROM demo_snapshots WHERE user_id = ?", (user.id,))
    users.set_setting(c.conn, user.id, "demo_start_balance", body.start_balance)
    audit(c.conn, "demo.reset", user_id=user.id, username=user.username, ip=client_ip(request),
          detail={"new_balance": body.start_balance, "previous": old, "previous_by_type": by_type})
    return {"account": portfolio.demo_account(c.conn, user.id, body.start_balance)}


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
        "default_balance": autotrade.demo_start(c.conn, user.id),
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


# --- alerts and track record ------------------------------------------------------------

@router.get("/alerts")
async def alerts(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    demo_mode = bool(users.get_setting(c.conn, user.id, "demo_mode", False))
    rows = c.conn.execute(
        "SELECT a.* FROM alerts a JOIN my_positions p ON p.id = a.position_id "
        "WHERE a.user_id = ? AND a.seen = 0 AND (p.status = 'open' OR a.kind = 'demo_done') "
        f"AND p.source {'=' if demo_mode else '!='} 'demo' ORDER BY a.ts DESC", (user.id,))
    return [dict(r) for r in rows]


@router.post("/alerts/{aid}/seen")
async def seen(aid: int, user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    with c.conn:
        cur = c.conn.execute("UPDATE alerts SET seen = 1 WHERE id = ? AND user_id = ?", (aid, user.id))
    if not cur.rowcount:
        raise HTTPException(404, "Not found.")
    return {"seen": aid}


@router.get("/performance")
async def performance(user: users.User = Depends(current_user), c: Ctx = Depends(ctx)):
    return tracker.performance(c.conn, db.load_markets(c.conn))


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
        "bankroll": g("bankroll"), "risk_budget": g("risk_budget"), "notify_url_set": bool(g("notify_url", "")),
        "demo_mode": bool(g("demo_mode", False)), "autotrade": autotrade.config(c.conn, user.id),
        "fees": {"taker": g("fee_taker"), "maker": g("fee_maker"),
                 "okx_reported": (account_status(c.conn, user.id) or {}).get("fee_rate"),
                 "default_taker": c.settings.fee_rate, "default_maker": c.settings.maker_fee_rate},
        "defaults": {"bankroll": app_settings.get(c.conn, "default_bankroll"),
                     "demo_balance": app_settings.get(c.conn, "default_demo_balance")},
    }


class Prefs(BaseModel):
    bankroll: float | None = Field(default=None, gt=0, le=100_000_000)
    risk_budget: float | None = Field(default=None, ge=0, le=100_000_000)
    notify_url: str | None = Field(default=None, max_length=300)
    demo_mode: bool | None = None
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
    return {"bankroll": bankroll_info(c.conn, c.settings, _now(), user.id),
            "demo_mode": bool(users.get_setting(c.conn, user.id, "demo_mode", False))}


class AutoTrade(BaseModel):
    enabled: bool
    types: list[str] = Field(max_length=4)
    max_open: int = Field(ge=1, le=50)
    max_invested_pct: float = Field(ge=0.05, le=1.0)

    @field_validator("types")
    @classmethod
    def known(cls, v):
        bad = [t for t in v if t not in autotrade.TYPES]
        if bad:
            raise ValueError(f"unknown types {bad}")
        return v


async def _autotrade_now(c: Ctx) -> list[dict]:
    """Buy the current picks and rising coins right away (at fresh OKX prices) instead of waiting for the next
    refresh or scan, e.g. just after automatic demo trading is switched on."""
    now = _now()
    view = c.pipeline.last_view or market_view(c.conn, c.settings, now)
    rising = movers.load(c.conn, now)[0]
    markets = dict(view.markets)
    wanted = {p.symbol: markets[p.symbol].pair for p in [*view.picks, *view.copies, *rising] if p.symbol in markets}
    if wanted:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "copy-signals/0.4"}) as client:
            fresh = await c.pipeline.spot.prices(client, list(wanted.values()))
        for coin, pair in wanted.items():
            if pair in fresh:
                markets[coin] = dataclasses.replace(markets[coin], price=fresh[pair])
    bank = reference_bankroll(c.conn, c.settings)
    fee_for = lambda uid: user_fee(c.conn, c.settings, uid)[0]  # noqa: E731
    bought = autotrade.run(c.conn, view.picks + view.copies, bank["amount"], markets, now, reference_fee=bank["fee_rate"],
                           fee_for=fee_for)
    bought += autotrade.run(c.conn, rising, bank["amount"], markets, now, reference_fee=bank["fee_rate"], fee_for=fee_for)
    return bought


@router.put("/settings/autotrade")
async def put_autotrade(body: AutoTrade, request: Request, user: users.User = Depends(current_user),
                        c: Ctx = Depends(ctx)):
    users.set_setting(c.conn, user.id, "autotrade", body.model_dump())
    audit(c.conn, "demo.autotrade_settings", user_id=user.id, username=user.username, ip=client_ip(request),
          detail=body.model_dump())
    if body.enabled:
        try:
            await _autotrade_now(c)
        except httpx.HTTPError:
            pass  # no fresh prices right now: the next refresh or scan buys them
    return autotrade.config(c.conn, user.id)


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
