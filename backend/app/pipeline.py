"""Background work: OKX prices and accounts, the trend bot, and the long/short paper test.

- Every `MARKETS_SECONDS`: OKX spot prices (to value OKX holdings) and each connected user's OKX account.
- The trend bot (trendbot.py) checks once a day after the 00:00 UTC close; prices come from OKX's live stream.
- The long/short test (longshort.py) runs once a day after the close: new market data (lsdata.py), a model retrained
  every `RETRAIN_DAYS` (lsmodel.py, in a worker thread), then the paper book is rebalanced at live prices.
"""

import asyncio
import dataclasses
import json
import logging
import sqlite3
import time

import httpx

from . import backtest, db, history, longshort, lsdata, lsmodel, notify, trendbot, users
from .accounts import build_account
from .config import Settings
from .logs import audit
from .market import OkxSpot
from .stream import TickerStream

log = logging.getLogger(__name__)
MARKETS_SECONDS = 300  # OKX prices and accounts
BOT_SECONDS = 60  # how often the trend bot checks whether its daily run is due (and records account values)
LS_SECONDS = 60  # how often the long/short test checks whether its daily run is due
LS_RETRY_S = 600  # after a failed or early attempt (the day's data isn't out yet)
RETRAIN_DAYS = 30
LS_PRICE_TTL = 30  # live prices shown on the long/short page are at most this old
UA = {"User-Agent": "copy-signals/0.5"}


def account_status(conn: sqlite3.Connection, user_id: int) -> dict | None:
    return users.get_setting(conn, user_id, "account_status")


def user_fee(conn: sqlite3.Connection, settings: Settings, user_id: int, account: dict | None = None) -> tuple[float, str]:
    """The taker fee used for a user's costs: what they entered in Settings, else what OKX reports, else the default.
    Taker, because the bot trades at market."""
    own = users.get_setting(conn, user_id, "fee_taker")
    if own is not None:
        return float(own), "yours"
    account = account or account_status(conn, user_id)
    if account and account.get("fee_rate"):
        return float(account["fee_rate"]), "okx"
    return settings.fee_rate, "default"


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self.spot = OkxSpot(settings.okx_region)
        self.stream = TickerStream(settings.okx_region)
        self.stream.want(trendbot.PAIRS)
        self.history = history.connect(settings.history_path)
        lsdata.init(self.history)
        longshort.init(conn)
        self.model_path = settings.history_path.with_name("ls_model.pkl")
        self.bot_signal: dict | None = None  # today's trend-bot targets and the reasons behind them
        self.bot_backtest: dict | None = None
        self.bot_error: str | None = None
        self.bot_wake = asyncio.Event()  # set to run the bot's check now (e.g. right after it's switched on)
        self.ls_wake = asyncio.Event()
        self.ls_state = "starting"  # what the long/short test is doing, shown on its page
        self.ls_error: str | None = None
        self.ls_next_try = 0.0
        self.ls_prices: tuple[float, dict[str, float]] = (0.0, {})
        self.updated_at: int | None = None
        with conn:  # sources of the removed swing copies
            conn.execute("DELETE FROM source_status WHERE source NOT IN "
                         "('spot_prices', 'ls_data', 'ls_model', 'ls_run')")

    # --- OKX prices and accounts ---------------------------------------------------------------

    async def _refresh_markets(self, client: httpx.AsyncClient, ts: int) -> None:
        started = time.monotonic()
        db.set_status(self.conn, "spot_prices", last_attempt=ts)
        try:
            markets = await self.spot.markets(client)
            db.save_markets(self.conn, markets, ts)
            db.set_status(self.conn, "spot_prices", last_ok=int(time.time()), last_error=None, n_traders=None,
                          n_positions=len(markets), duration_s=round(time.monotonic() - started, 1))
        except Exception as e:
            log.exception("%s spot refresh failed", self.spot.label)
            db.set_status(self.conn, "spot_prices", last_error=f"{type(e).__name__}: {e}"[:500],
                          duration_s=round(time.monotonic() - started, 1))

    async def sync_account(self, client: httpx.AsyncClient, user_id: int, ts: int) -> dict | None:
        """Mirror one user's OKX account; the result is stored as their account status."""
        account = build_account(users.okx_credentials(self.conn, user_id))
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

    async def refresh(self) -> None:
        ts = int(time.time())
        async with httpx.AsyncClient(timeout=30, headers=UA, follow_redirects=True) as client:
            await self._refresh_markets(client, ts)
            for user_id in users.users_with_okx(self.conn):
                await self.sync_account(client, user_id, ts)
        users.purge_sessions(self.conn, ts)
        self.updated_at = ts

    async def run_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("market/account refresh failed")
            await asyncio.sleep(MARKETS_SECONDS)

    async def _notify(self, client: httpx.AsyncClient, alerts: list[dict]) -> None:
        """Push to each user's notification address (admins fall back to NOTIFY_WEBHOOK_URL)."""
        for a in alerts:
            uid = a["user_id"]
            url = users.get_setting(self.conn, uid, "notify_url", "")
            if not url:
                u = users.get(self.conn, uid)
                url = self.settings.notify_webhook_url if u and u.is_admin else ""
            await notify.send(client, url, a["message"])

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
                    alerts.append({"user_id": uid, "message": "Trend bot (demo): " + "; ".join(
                        f"{'bought' if t.side == 'buy' else 'sold'} {t.qty:.6g} {t.coin} (${t.value_usd:,.0f})"
                        for t in trades)})
            await self._notify(client, alerts)
            if done:
                log.info("trend bot: %d account(s) checked, targets %s", len(done), sig["weights"])
        trendbot.snapshot(self.conn, prices, now)

    async def run_bots(self) -> None:
        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
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

    # --- long/short paper test ---------------------------------------------------------------------

    def model_info(self) -> dict | None:
        text = db.get_pref(self.conn, "ls_model", "")
        return json.loads(text) if text else None

    async def ls_live_prices(self, client: httpx.AsyncClient, coins: list[str]) -> dict[str, float]:
        """Binance spot prices, cached for LS_PRICE_TTL seconds."""
        at, cached = self.ls_prices
        want = set(coins)
        if time.time() - at < LS_PRICE_TTL and want <= set(cached):
            return cached
        prices = await lsdata.prices(client, sorted(want | set(cached)))
        self.ls_prices = (time.time(), prices)
        return prices

    def _history_conn(self) -> sqlite3.Connection:
        """A connection of its own for work in a thread."""
        return sqlite3.connect(self.settings.history_path, timeout=60)

    def _load_or_train(self, decision_day: int) -> lsmodel.Model:
        """(Runs in a thread.) The saved model, retrained when it's older than RETRAIN_DAYS."""
        model = lsmodel.load_model(self.model_path)
        stale = model is None or decision_day - model.trained_through > (RETRAIN_DAYS + lsmodel.HORIZON) * lsmodel.DAY
        if stale:
            conn = self._history_conn()
            try:
                started = time.monotonic()
                model = lsmodel.train(lsmodel.load(conn, since=1_514_764_800))  # 2018-01-01
            finally:
                conn.close()
            lsmodel.save(model, self.model_path)
            info = {"trained_at": int(time.time()), "trained_through": model.trained_through, "rows": model.rows,
                    "seconds": round(time.monotonic() - started)}
            db.set_pref(self.conn, "ls_model", json.dumps(info))
            log.info("long/short model retrained: %s", info)
        return model

    def _score(self, model: lsmodel.Model, day: int) -> tuple[dict, dict, set]:
        """(Runs in a thread.) Scores, 30-day volumes and coins with a perpetual, for `day`."""
        conn = self._history_conn()
        try:
            d = lsmodel.load(conn, since=day - lsmodel.WINDOW * lsmodel.DAY)
        finally:
            conn.close()
        if int(d.days[-1]) != day:
            raise longshort_stale(day)
        scores = lsmodel.scores_for_day(model, d)
        _, vol30 = lsmodel.universe(d)
        last = d.T - 1
        volume30 = {c: float(vol30[last, j]) for j, c in enumerate(d.coins) if vol30[last, j] == vol30[last, j]}
        has_perp = {c for j, c in enumerate(d.coins) if d.funding[last, j] == d.funding[last, j]}
        return scores, volume30, has_perp

    async def ls_tick(self, client: httpx.AsyncClient) -> None:
        now = int(time.time())
        day = longshort.decision_day(now)
        acct = longshort.account(self.conn)
        if acct is not None and not longshort.due(self.conn, now):
            self.ls_state, self.ls_error = "waiting", None
            held = list(longshort.positions(self.conn))
            longshort.snapshot(self.conn, await self.ls_live_prices(client, held + ["BTC"]), now)
            return
        if time.time() < self.ls_next_try:
            return
        try:
            if lsdata.latest_day(self.history) in (None, 0) and self.settings.research_market_db.exists():
                self.ls_state = "importing history"
                added = await asyncio.to_thread(self._import_research)
                log.info("long/short: imported %d days x coins from the research download", added)
            if (lsdata.latest_day(self.history) or 0) < day:
                self.ls_state = "updating market data"
                db.set_status(self.conn, "ls_data", last_attempt=now)
                started = time.monotonic()
                counts = await lsdata.update(self.history, now)
                db.set_status(self.conn, "ls_data", last_ok=int(time.time()), last_error=None,
                              n_positions=counts.get("spot"), duration_s=round(time.monotonic() - started, 1))
            if (lsdata.latest_day(self.history) or 0) < day:
                raise longshort_stale(day)
            self.ls_state = "training the model" if not self.model_path.exists() else "scoring coins"
            db.set_status(self.conn, "ls_model", last_attempt=now)
            model = await asyncio.to_thread(self._load_or_train, day)
            db.set_status(self.conn, "ls_model", last_ok=int(time.time()), last_error=None)
            self.ls_state = "scoring coins"
            scores, volume30, has_perp = await asyncio.to_thread(self._score, model, day)
            held = list(longshort.positions(self.conn))
            prices = await lsdata.prices(client, sorted(set(scores) | set(held) | {"BTC"}))
            ranked = longshort.eligible(scores, volume30, has_perp, prices)
            if longshort.account(self.conn) is None:  # first run: open the paper account
                longshort.start(self.conn, longshort.DEFAULT_BALANCE, now, prices.get("BTC"))
                audit(self.conn, "ls.started", now=now, detail={"balance": longshort.DEFAULT_BALANCE})
            last = longshort.account(self.conn)["last_run_day"]
            funding = self._funding_for(last if last is not None else day - lsmodel.DAY, day)
            result = longshort.rebalance(self.conn, day, ranked, scores, prices, funding, now)
            longshort.snapshot(self.conn, prices, now, every=0)
            db.set_status(self.conn, "ls_run", last_attempt=now, last_ok=int(time.time()), last_error=None,
                          n_positions=len(result["longs"]) + len(result["shorts"]))
            audit(self.conn, "ls.rebalance", now=now, detail={
                "day": day, "longs": result["longs"], "shorts": result["shorts"],
                "fees_usd": round(result["fees_usd"], 2), "funding_usd": round(result["funding_usd"], 2),
                "equity": round(result["equity"], 2)})
            v = longshort.value(self.conn, prices)
            await self._notify(client, [{"user_id": u.id, "message":
                f"Long/short test (paper): bought {', '.join(result['longs']) or '-'}; shorted "
                f"{', '.join(result['shorts']) or '-'}. Account ${v['equity']:,.0f} ({v['pnl_pct'] * 100:+.1f}%)."}
                for u in users.all_active(self.conn) if u.is_admin])
            log.info("long/short: day %s, long %s, short %s", time.strftime("%Y-%m-%d", time.gmtime(day)),
                     result["longs"], result["shorts"])
            self.ls_state, self.ls_error, self.ls_next_try = "waiting", None, 0.0
        except LongShortStale as e:
            self.ls_state, self.ls_error, self.ls_next_try = "waiting for data", str(e), time.time() + LS_RETRY_S
        except Exception as e:
            log.exception("long/short run failed")
            db.set_status(self.conn, "ls_run", last_attempt=now, last_error=f"{type(e).__name__}: {e}"[:500])
            self.ls_state, self.ls_next_try = "error", time.time() + LS_RETRY_S
            self.ls_error = "The last run failed; it tries again in 10 minutes."

    def _import_research(self) -> int:
        conn = self._history_conn()
        try:
            return lsdata.import_research(conn, self.settings.research_market_db)
        finally:
            conn.close()

    def _funding_for(self, after: int, day: int) -> dict[str, float]:
        """Funding per coin for every day since the last run (`after`, exclusive) up to `day`: if the app was off
        for a few days, the positions were still held and paid or received funding on each of them."""
        return {c: f for c, f in self.history.execute(
            "SELECT coin, SUM(rate * n) FROM ls_funding WHERE day > ? AND day <= ? GROUP BY coin", (after, day))}

    async def run_longshort(self) -> None:
        await asyncio.sleep(10)
        async with httpx.AsyncClient(timeout=30, headers=UA) as client:
            while True:
                try:
                    await self.ls_tick(client)
                except Exception as e:
                    log.warning("long/short check failed: %s", e)
                self.ls_wake.clear()
                try:
                    await asyncio.wait_for(self.ls_wake.wait(), LS_SECONDS)
                except TimeoutError:
                    pass


class LongShortStale(Exception):
    """The day's market data isn't published yet."""


def longshort_stale(day: int) -> LongShortStale:
    return LongShortStale(f"waiting for the {time.strftime('%Y-%m-%d', time.gmtime(day))} daily close from Binance")

