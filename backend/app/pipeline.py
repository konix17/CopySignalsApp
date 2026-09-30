"""Background work: following traders, swing copies, syncing accounts, running the trend bot.

Two speeds. Every minute: the followed traders' open positions (who bought and sold), OKX prices and OKX accounts,
then swing copies and every open trade are updated. Every `refresh_minutes` (admin setting, 10 by default) a full
refresh also re-reads the leaderboards to choose who to follow and trader drawdowns, which change slowly. Live
prices for open trades come from the price stream every 2 seconds.

Market-wide work (traders, swing copies, the track record) is shared by everyone and uses a reference bankroll for
sizes. Per-user work (bankroll, OKX account sync, alerts, demo auto-trading) runs for each user separately.
"""

import asyncio
import dataclasses
import logging
import sqlite3
import time
from dataclasses import dataclass, field

import httpx

from . import activity, app_settings, autotrade, backtest, db, history, notify, portfolio, swing, tracker, trendbot, users
from .accounts import build_account
from .config import Settings
from .logs import audit
from .market import Market, OkxSpot
from .models import Pick
from .scoring import score_stats, top_traders
from .signals import meaningful_positions
from .sources import Source, build_sources
from .stream import TickerStream

log = logging.getLogger(__name__)
TREND_MAX_AGE = 3 * 3600
TREND_COINS = ["BTC"]  # daily averages are only needed for BTC: below its 50-day average, copies are half size
DRAWDOWN_MAX_AGE = 12 * 3600
DRAWDOWNS_PER_REFRESH = 25
ACCOUNT_FRESH = 3600
LIVE_SECONDS = 2  # how often open positions and paper trades are re-priced from the price stream
REST_SECONDS = 10  # the same, while the stream is down and prices come from REST
MIN_ACCOUNT_BANKROLL = 10.0
POSITIONS_SECONDS = 60  # the fast cycle: followed traders' positions, prices, copies, accounts
BOT_SECONDS = 60  # how often the trend bot checks whether its daily run is due (and records account values)


@dataclass
class MarketView:
    regime: dict
    markets: dict[str, Market]
    bankroll: dict
    copies: list[Pick] = field(default_factory=list)  # swing copies (swing.py): longs held 12 h or more


def account_status(conn: sqlite3.Connection, user_id: int) -> dict | None:
    return users.get_setting(conn, user_id, "account_status")


def reference_bankroll(conn: sqlite3.Connection, settings: Settings) -> dict:
    """What shared copies are sized for; each user sees them rescaled to their own bankroll."""
    return {"amount": app_settings.get(conn, "default_bankroll"), "source": "reference", "fee_rate": settings.fee_rate}


def bankroll_info(conn: sqlite3.Connection, settings: Settings, now: int, user_id: int) -> dict:
    """The user's OKX trading-account total once it holds money; otherwise the default bankroll."""
    acct = account_status(conn, user_id)
    fresh = acct and acct.get("ok") and now - acct.get("synced_at", 0) < ACCOUNT_FRESH
    fee, fee_source = user_fee(conn, settings, user_id, acct if fresh else None)
    if fresh and acct["total_usd"] >= MIN_ACCOUNT_BANKROLL:
        info = {"amount": acct["total_usd"], "source": "exchange", "fee_rate": fee}
    else:
        amount = float(users.get_setting(conn, user_id, "bankroll", app_settings.get(conn, "default_bankroll")))
        info = {"amount": amount, "source": "manual", "fee_rate": fee, "spot_empty": bool(fresh)}
    info["fee_source"] = fee_source
    info["maker_fee_rate"] = float(users.get_setting(conn, user_id, "fee_maker", settings.maker_fee_rate))
    return info


def user_fee(conn: sqlite3.Connection, settings: Settings, user_id: int, account: dict | None = None) -> tuple[float, str]:
    """The taker fee used for a user's costs: what they entered in Settings, else what OKX reports, else the default.
    Taker, because buys and stops go at market."""
    own = users.get_setting(conn, user_id, "fee_taker")
    if own is not None:
        return float(own), "yours"
    if account and account.get("fee_rate"):
        return float(account["fee_rate"]), "okx"
    return settings.fee_rate, "default"


def refresh_minutes(conn: sqlite3.Connection) -> float:
    return app_settings.get(conn, "refresh_minutes")


def market_view(conn: sqlite3.Connection, settings: Settings, now: int, user_id: int | None = None) -> MarketView:
    """Everything the UI, alerts and tracker need, computed from the database. With `user_id`, copy sizes fit that
    user's bankroll; without, the reference bankroll."""
    scores = db.load_composite_scores(conn)
    markets = db.load_markets(conn)
    regime = swing.market_regime(markets)
    bank = bankroll_info(conn, settings, now, user_id) if user_id is not None else reference_bankroll(conn, settings)
    copies = swing.candidates(conn, markets, scores, now, bank["amount"], bank["fee_rate"], settings.slippage,
                              settings.min_volume_usd, settings.max_spread, regime["risk_on"])
    return MarketView(regime, markets, bank, copies)


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self.sources: list[Source] = build_sources(settings.sources)
        self.spot = OkxSpot(settings.okx_region)
        self.stream = TickerStream(settings.okx_region)
        self.history = history.connect(settings.history_path)
        self.bot_signal: dict | None = None  # today's trend-bot targets and the reasons behind them
        self.bot_backtest: dict | None = None
        self.bot_error: str | None = None
        self.bot_wake = asyncio.Event()  # set to run the bot's check now (e.g. right after it's switched on)
        with conn:
            conn.execute("DELETE FROM source_status WHERE source = 'binance_spot'")  # renamed to spot_prices
        self.lock = asyncio.Lock()  # one data cycle (full or 1-minute) at a time
        self.full_running = False
        self.full_at = 0.0  # when the last full refresh finished
        self.last_view: MarketView | None = None  # prices and copies from the last data cycle, reused by live ticks
        self.live_at: int | None = None

    @property
    def running(self) -> bool:
        """A full refresh is under way (the 1-minute cycle isn't shown as updating)."""
        return self.full_running

    # --- shared data -----------------------------------------------------------

    async def _refresh_source(self, client: httpx.AsyncClient, src: Source, ts: int) -> None:
        started = time.monotonic()
        db.set_status(self.conn, src.name, last_attempt=ts)
        try:
            stats = await src.fetch_stats(client)
            known_dd = db.load_drawdowns(self.conn, src.name)
            score_stats(stats, {a: d for a, (d, _) in known_dd.items()})
            top = top_traders(stats, self.settings.top_n)
            followed = {(w, s.address) for w, rows in top.items() for s in rows}
            addresses = sorted({a for _, a in followed})
            positions, fetched = await src.fetch_positions(client, addresses)
            keep = set(addresses)
            db.replace_source(self.conn, src.name, [s for s in stats if s.address in keep], followed, positions, ts)
            activity.sync_position_log(self.conn, src.name, meaningful_positions(positions), fetched, ts,
                                       stale_after_s=refresh_minutes(self.conn) * 60 * 3)
            # Drawdowns are expensive to fetch: refresh a batch of stale ones each cycle (used from next cycle).
            if hasattr(src, "fetch_drawdowns"):
                stale = [a for a in addresses if a not in known_dd or ts - known_dd[a][1] > DRAWDOWN_MAX_AGE]
                if stale:
                    db.save_drawdowns(self.conn, src.name,
                                      await src.fetch_drawdowns(client, stale[:DRAWDOWNS_PER_REFRESH]), ts)
            db.set_status(self.conn, src.name, last_ok=int(time.time()), last_error=None, n_traders=len(addresses),
                          n_positions=len(positions), duration_s=round(time.monotonic() - started, 1))
            log.info("%s: %d traders followed, %d positions", src.name, len(addresses), len(positions))
        except Exception as e:  # one venue failing must not block the others
            log.exception("%s refresh failed", src.name)
            db.set_status(self.conn, src.name, last_error=f"{type(e).__name__}: {e}"[:500],
                          duration_s=round(time.monotonic() - started, 1))

    async def _refresh_positions(self, client: httpx.AsyncClient, src: Source, ts: int) -> None:
        """1-minute cycle: open positions of the traders already followed (who to follow is decided in the full
        refresh). Entries and exits go into the position log within a minute of happening."""
        name = f"{src.name}_positions"
        addresses = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT address FROM trader_stats WHERE source = ? AND followed = 1", (src.name,))]
        if not addresses:
            return
        started = time.monotonic()
        db.set_status(self.conn, name, last_attempt=ts)
        try:
            positions, fetched = await src.fetch_positions(client, addresses)
            db.replace_positions(self.conn, src.name, positions, fetched, ts)
            activity.sync_position_log(self.conn, src.name, meaningful_positions(positions), fetched, ts,
                                       stale_after_s=POSITIONS_SECONDS * 5)
            db.set_status(self.conn, name, last_ok=int(time.time()), last_error=None, n_traders=len(fetched),
                          n_positions=len(positions), duration_s=round(time.monotonic() - started, 1))
        except Exception as e:
            log.warning("%s positions failed: %s", src.name, e)
            db.set_status(self.conn, name, last_error=f"{type(e).__name__}: {e}"[:500],
                          duration_s=round(time.monotonic() - started, 1))

    async def _refresh_markets(self, client: httpx.AsyncClient, coins: list[str], ts: int) -> None:
        started = time.monotonic()
        db.set_status(self.conn, "spot_prices", last_attempt=ts)
        try:
            markets = await self.spot.markets(client)
            ages = db.trend_ages(self.conn)
            stale = [c for c in coins if c in markets and ts - ages.get(c, 0) > TREND_MAX_AGE]
            await self.spot.add_trends(client, markets, stale)
            db.save_markets(self.conn, markets, ts)
            db.set_status(self.conn, "spot_prices", last_ok=int(time.time()), last_error=None, n_traders=None,
                          n_positions=len(markets), duration_s=round(time.monotonic() - started, 1))
        except Exception as e:
            log.exception("%s spot refresh failed", self.spot.label)
            db.set_status(self.conn, "spot_prices", last_error=f"{type(e).__name__}: {e}"[:500],
                          duration_s=round(time.monotonic() - started, 1))

    # --- per-user --------------------------------------------------------------

    async def sync_account(self, client: httpx.AsyncClient, user_id: int, ts: int) -> dict | None:
        """Mirror one user's OKX account; the result is stored as their account status."""
        creds = users.okx_credentials(self.conn, user_id)
        account = build_account(creds)
        if account is None:
            return None
        try:
            result = await account.sync(client, self.conn, db.load_markets(self.conn), ts, user_id)
            status = {"ok": True, "synced_at": ts, **result}
        except Exception as e:
            log.warning("OKX account sync failed for user %s: %s", user_id, e)
            prev = account_status(self.conn, user_id) or {}
            status = {**prev, "ok": False, "error": str(e)[:300], "exchange": "okx", "label": "OKX"}
        users.set_setting(self.conn, user_id, "account_status", status)
        return status

    async def sync_accounts(self, client: httpx.AsyncClient, ts: int) -> None:
        for user_id in users.users_with_okx(self.conn):
            await self.sync_account(client, user_id, ts)

    async def _notify(self, client: httpx.AsyncClient, alerts: list[dict]) -> None:
        """Push alerts to each user's notification address (admins fall back to NOTIFY_WEBHOOK_URL)."""
        for a in alerts:
            if a.get("level") == "warning" and a.get("auto"):
                continue
            uid = a.get("user_id")
            url = users.get_setting(self.conn, uid, "notify_url", "") if uid else ""
            if not url and uid:
                u = users.get(self.conn, uid)
                url = self.settings.notify_webhook_url if u and u.is_admin else ""
            await notify.send(client, url, a["message"])

    def _record_closed_demo_trades(self, alerts: list[dict], now: int) -> None:
        for a in alerts:
            if a.get("kind") == "demo_done":
                audit(self.conn, "demo.auto_sell" if a.get("auto") else "demo.closed", user_id=a.get("user_id"),
                      detail={"position": a["position_id"], "symbol": a.get("symbol"), "result": a["message"]}, now=now)

    def _demo_snapshots(self, markets: dict[str, Market], now: int) -> None:
        btc = markets.get("BTC")
        rows = self.conn.execute(
            "SELECT DISTINCT user_id FROM my_positions WHERE source = 'demo' AND user_id IS NOT NULL "
            "UNION SELECT user_id FROM user_settings WHERE key = 'autotrade'").fetchall()
        for (uid,) in rows:
            acct = portfolio.demo_account(self.conn, uid, autotrade.demo_start(self.conn, uid))
            portfolio.snapshot_demo(self.conn, uid, acct["value"], btc.price if btc else None, now)

    # --- cycles ------------------------------------------------------------------

    async def _decide(self, client: httpx.AsyncClient, ts: int) -> tuple[MarketView, list[dict], list[dict]]:
        """Rebuild the swing copies from the database and act on them: paper trades, advice, demo trades, alerts."""
        view = market_view(self.conn, self.settings, ts)
        tracker.update_trades(self.conn, view.markets, ts)
        tracker.open_trades(self.conn, view.copies, view.markets, ts)
        alerts = portfolio.update_positions(self.conn, view.markets, ts)
        alerts += portfolio.settle_demo_trades(self.conn, view.markets, ts)
        self._record_closed_demo_trades(alerts, ts)
        bought = autotrade.run(self.conn, view.copies, view.bankroll["amount"], view.markets, ts,
                               reference_fee=view.bankroll["fee_rate"],
                               fee_for=lambda uid: user_fee(self.conn, self.settings, uid)[0])
        self._demo_snapshots(view.markets, ts)
        await self._notify(client, alerts)
        self.last_view = view
        return view, alerts, bought

    async def refresh(self) -> None:
        """Full refresh: leaderboards and who to follow, drawdowns, trends, then the 1-minute work."""
        if self.full_running:
            return
        self.full_running = True
        try:
            async with self.lock:
                ts = int(time.time())
                async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "copy-signals/0.4"},
                                             follow_redirects=True) as client:
                    await asyncio.gather(*(self._refresh_source(client, src, ts) for src in self.sources))
                    await self._refresh_markets(client, TREND_COINS, ts)
                    await self.sync_accounts(client, ts)
                    view, alerts, bought = await self._decide(client, ts)
                users.purge_sessions(self.conn, ts)
                self.full_at = time.time()
                log.info("%d swing copies (%s), %d new alerts, %d demo auto-buys", len(view.copies),
                         "uptrend" if view.regime["risk_on"] else "downtrend", len(alerts), len(bought))
        finally:
            self.full_running = False

    async def refresh_fast(self) -> None:
        """1-minute cycle: followed traders' positions, OKX prices and accounts, then copies and trades."""
        if self.lock.locked():
            return
        async with self.lock:
            ts = int(time.time())
            before = {p.symbol for p in self.last_view.copies} if self.last_view else set()
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "copy-signals/0.4"},
                                         follow_redirects=True) as client:
                await asyncio.gather(*(self._refresh_positions(client, src, ts) for src in self.sources),
                                     self._refresh_markets(client, TREND_COINS, ts))
                await self.sync_accounts(client, ts)
                view, alerts, bought = await self._decide(client, ts)
            now_picks = {p.symbol for p in view.copies}
            if now_picks != before or alerts or bought:
                log.info("1-minute update: swing copies %s, %d new alerts, %d demo auto-buys",
                         ", ".join(sorted(now_picks)) or "none", len(alerts), len(bought))

    async def live_tick(self, client: httpx.AsyncClient) -> None:
        """Re-price every open position and paper trade, from the OKX price stream (REST for anything the stream
        hasn't got fresh). Demo and paper trades that hit their stop or target close; real positions get their
        advice and alerts updated (the app never sells for you). Whether copied traders still hold comes from the
        last 1-minute cycle."""
        coins = {r[0] for r in self.conn.execute("SELECT DISTINCT symbol FROM my_positions WHERE status = 'open' "
                                                 "UNION SELECT symbol FROM pick_trades WHERE status = 'open' "
                                                 "AND style = 'copy'")}
        now = int(time.time())
        if self.last_view is None:  # e.g. right after a restart: use what's in the database
            self.last_view = market_view(self.conn, self.settings, now)
        view = self.last_view
        pairs = {c: view.markets[c].pair for c in coins | {"BTC"} if c in view.markets}
        self.stream.want({*pairs.values(), *trendbot.PAIRS})
        if not coins:
            return
        prices = self.stream.prices(pairs.values())
        missing = [p for p in pairs.values() if p not in prices]
        if missing:
            prices |= await self.spot.prices(client, missing)
        markets = dict(view.markets)
        for coin, pair in pairs.items():
            if pair in prices:
                markets[coin] = dataclasses.replace(view.markets[coin], price=prices[pair])
        alerts = portfolio.settle_demo_trades(self.conn, markets, now)
        alerts += portfolio.update_positions(self.conn, markets, now)
        tracker.update_trades(self.conn, markets, now)
        self._record_closed_demo_trades(alerts, now)
        await self._notify(client, alerts)
        self.live_at = now

    async def run_live(self) -> None:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "copy-signals/0.4"}) as client:
            while True:
                try:
                    await self.live_tick(client)
                except Exception as e:
                    log.warning("live prices failed: %s", e)
                await asyncio.sleep(LIVE_SECONDS if self.stream.connected else REST_SECONDS)

    # --- trend bot -----------------------------------------------------------------------------

    async def bot_prices(self, client: httpx.AsyncClient) -> dict[str, float]:
        """Live prices for the bot's coins: the stream, or REST when it has nothing fresh."""
        prices = self.stream.prices(trendbot.PAIRS)
        missing = [p for p in trendbot.PAIRS if p not in prices]
        if missing:
            prices |= await self.spot.prices(client, missing)
        return {p.split("-")[0]: v for p, v in prices.items()}

    async def refresh_bot_signal(self, now: int) -> dict:
        """Today's targets, from OKX daily closes (fetched once per day). Raises trendbot.StaleData until
        yesterday's close is published."""
        day = trendbot.decision_day(now)
        if self.bot_signal and self.bot_signal["day"] == day:
            return self.bot_signal
        await history.update(self.history, pairs=list(trendbot.PAIRS), region=self.settings.okx_region, now=now,
                             log=lambda *_: None)
        try:
            await history.update_funding(self.history, list(trendbot.PAIRS), now=now)
        except httpx.HTTPError as e:  # without fresh funding the rule just has no signal for the missing days
            log.warning("funding rates unavailable: %s", e)
        panel = backtest.Panel(history.load(self.history, "1Dutc", list(trendbot.PAIRS)))
        funding = history.load_funding(self.history)
        self.bot_signal = trendbot.signal(panel, day, funding)
        self.bot_backtest = trendbot.backtest_summary(panel, funding=funding)
        return self.bot_signal

    async def bot_tick(self, client: httpx.AsyncClient) -> None:
        now = int(time.time())
        try:
            sig = await self.refresh_bot_signal(now)
            self.bot_error = None
        except trendbot.StaleData as e:
            sig, self.bot_error = None, f"Waiting for OKX's daily close: {e}"
        prices = await self.bot_prices(client)
        if sig and trendbot.due(self.conn, now):
            done = trendbot.run_due(self.conn, sig, prices, lambda uid: user_fee(self.conn, self.settings, uid)[0],
                                    self.settings.slippage, now)
            alerts = []
            for uid, trades in done.items():
                audit(self.conn, "bot.rebalance", user_id=uid, now=now, detail={
                    "targets": sig["weights"], "trades": [dataclasses.asdict(t) for t in trades]})
                if trades:
                    alerts.append({"user_id": uid, "level": "info", "message": "Trend bot (demo): " + "; ".join(
                        f"{'bought' if t.side == 'buy' else 'sold'} {t.qty:.6g} {t.coin} (${t.value_usd:,.0f})"
                        for t in trades)})
            await self._notify(client, alerts)
            if done:
                log.info("trend bot: %d account(s) checked, targets %s", len(done), sig["weights"])
        trendbot.snapshot(self.conn, prices, now)

    async def run_bots(self) -> None:
        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "copy-signals/0.4"}) as client:
            while True:
                try:
                    await self.bot_tick(client)
                except Exception as e:
                    log.warning("trend bot check failed: %s", e)
                    self.bot_error = "The last check failed; it retries every minute."
                self.bot_wake.clear()
                try:
                    await asyncio.wait_for(self.bot_wake.wait(), BOT_SECONDS)
                except TimeoutError:
                    pass

    async def run_forever(self) -> None:
        """A full refresh every `refresh_minutes`, the 1-minute cycle in between."""
        while True:
            started = time.monotonic()
            try:
                if time.time() - self.full_at >= refresh_minutes(self.conn) * 60:
                    await self.refresh()
                else:
                    await self.refresh_fast()
            except Exception:
                log.exception("data cycle failed")
            await asyncio.sleep(max(5.0, POSITIONS_SECONDS - (time.monotonic() - started)))
