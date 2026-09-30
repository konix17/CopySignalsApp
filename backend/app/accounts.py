"""Read-only mirror of your OKX account.

Keys go in `.env` (see .env.example) and must be read-only: the app refuses a
key that can withdraw.

Each sync:
1. Checks the key's permissions.
2. Reads balances, open orders (including stop-loss / take-profit / trailing
   stop orders), trades for every coin you hold, and your real trading fee.
3. Works out each holding's average cost from its trades and whether a
   stop-loss order protects it.

The OKX reader turns OKX's API into the normalized shapes in `Snapshot`;
the bookkeeping after that is exchange-neutral, so another exchange could be
added the same way.
"""

import base64
import hashlib
import hmac
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from .market import OKX_DOMAINS, STABLES, Market

log = logging.getLogger(__name__)
DUST_USD = 10.0  # holdings worth less than this are ignored


class UnsafeKeyError(RuntimeError):
    pass


def value_usd(asset: str, qty: float, markets: dict[str, Market]) -> float:
    if asset in STABLES and asset != "EUR":
        return qty
    m = markets.get(asset)
    return qty * m.price if m else 0.0


@dataclass
class Trade:
    pair: str
    id: str
    order_id: str
    price: float
    qty: float
    quote_qty: float
    commission: float
    commission_asset: str
    time: int  # ms
    is_buyer: bool


@dataclass
class Order:
    pair: str
    order_id: str
    kind: str  # "stop" | "trailing" | "target"
    price: float | None  # target / limit price
    stop_price: float | None  # stop trigger price (None for trailing stops)
    qty: float  # still to fill
    time: int  # ms


@dataclass
class Snapshot:
    can_trade: bool
    balances: dict[str, tuple[float, float]]  # asset -> (free, locked)
    orders: list[Order]
    fee_rate: float | None
    funding_usd: float | None = None
    earn_usd: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class Holding:
    coin: str
    qty: float
    locked: float
    price: float | None
    value_usd: float | None
    avg_cost: float | None
    cost_known: bool
    opened_at: int | None


def average_cost(trades: list[dict], coin: str) -> tuple[float, float | None, int | None]:
    """Average-cost bookkeeping over trades (oldest first).
    Returns (qty bought net of sells since the holding last started from zero, avg cost, start time in s)."""
    qty = cost = 0.0
    started = None
    for t in trades:
        q, p = t["qty"], t["price"]
        fee_in_coin = t["commission"] if t["commission_asset"] == coin else 0.0
        if t["is_buyer"]:
            if qty <= 1e-12:
                qty, cost, started = 0.0, 0.0, t["time"] // 1000
            cost += q * p
            qty += q - fee_in_coin
        else:
            if qty > 0:
                cost *= max(0.0, qty - q) / qty
            qty = max(0.0, qty - q - fee_in_coin)
    return qty, (cost / qty if qty > 1e-12 else None), started


class ExchangeAccount:
    """Shared sync logic. Subclasses implement `snapshot` and `new_trades`."""

    name = "exchange"
    label = "Exchange"

    async def snapshot(self, client: httpx.AsyncClient, markets: dict[str, Market]) -> Snapshot:
        raise NotImplementedError

    async def new_trades(self, client: httpx.AsyncClient, pair: str, last_id: str | None) -> list[Trade]:
        """Trades newer than `last_id` (or the most recent ones if None)."""
        raise NotImplementedError

    async def sync(self, client: httpx.AsyncClient, conn: sqlite3.Connection, markets: dict[str, Market], now: int,
                   user_id: int) -> dict:
        """Mirror one user's account into user_holdings/user_trades/user_orders."""
        self.uid = user_id
        snap = await self.snapshot(client, markets)
        cash = sum(f + l for a, (f, l) in snap.balances.items() if a in STABLES and a != "EUR")
        coins = [a for a in snap.balances if a in markets and a not in STABLES]
        for coin in coins:
            pair = markets[coin].pair
            last = conn.execute("SELECT id FROM user_trades WHERE user_id = ? AND exchange = ? AND pair = ? "
                                "ORDER BY time DESC LIMIT 1", (user_id, self.name, pair)).fetchone()
            trades = await self.new_trades(client, pair, last[0] if last else None)
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO user_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(user_id, self.name, t.pair, t.id, t.order_id, t.price, t.qty, t.quote_qty, t.commission,
                      t.commission_asset, t.time, int(t.is_buyer)) for t in trades],
                )
        with conn:
            conn.execute("DELETE FROM user_orders WHERE user_id = ?", (user_id,))
            conn.executemany("INSERT OR REPLACE INTO user_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             [(user_id, self.name, o.pair, o.order_id, o.kind, o.price, o.stop_price, o.qty, o.time)
                              for o in snap.orders])

        holdings = self._holdings(conn, snap.balances, markets, coins, now)
        return {"exchange": self.name, "label": self.label, "cash_usd": cash,
                "total_usd": cash + sum(h.value_usd or 0 for h in holdings), "funding_usd": snap.funding_usd,
                "earn_usd": snap.earn_usd, "can_trade": snap.can_trade, "fee_rate": snap.fee_rate,
                "holdings": len(holdings)}

    # --- shared bookkeeping (all scoped to self.uid) ---------------------------

    def _trades_for(self, conn, pair: str) -> list[dict]:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM user_trades WHERE user_id = ? AND exchange = ? AND pair = ? ORDER BY time, id",
            (self.uid, self.name, pair))]

    def _holdings(self, conn, balances, markets, coins, now) -> list[Holding]:
        out = []
        with conn:
            conn.execute("DELETE FROM user_holdings WHERE user_id = ?", (self.uid,))
            for coin in coins:
                free, locked = balances.get(coin, (0.0, 0.0))
                total = free + locked
                m = markets.get(coin)
                value = total * m.price if m else None
                if value is None or value < DUST_USD:
                    continue
                traded_qty, avg, started = average_cost(self._trades_for(conn, m.pair), coin)
                cost_known = avg is not None and traded_qty >= 0.95 * total
                stop = conn.execute(
                    "SELECT MAX(stop_price), SUM(qty), MAX(kind = 'trailing') FROM user_orders "
                    "WHERE user_id = ? AND pair = ? AND kind IN ('stop', 'trailing')", (self.uid, m.pair)).fetchone()
                target = conn.execute("SELECT MIN(price) FROM user_orders WHERE user_id = ? AND pair = ? AND kind = 'target'",
                                      (self.uid, m.pair)).fetchone()[0]
                h = Holding(coin, total, locked, m.price, value, avg if cost_known else None, cost_known, started)
                conn.execute(
                    "INSERT INTO user_holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.uid, coin, self.name, total, locked, m.price, value, h.avg_cost, int(cost_known), started,
                     stop[0], stop[1], "trailing" if stop[2] else ("stop" if stop[1] else None), target, now),
                )
                out.append(h)
        return out


# --- OKX -----------------------------------------------------------------------

class OkxAccount(ExchangeAccount):
    """OKX API v5. Create the key under Profile → API keys with only the "Read" permission;
    OKX also asks for a passphrase you choose, which goes in OKX_API_PASSPHRASE.
    European accounts (my.okx.com) must use the eea.okx.com API domain: region="eea"."""

    name = "okx"
    label = "OKX"
    MAX_PAGES = 5

    def __init__(self, key: str, secret: str, passphrase: str, region: str = "eea"):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.api = OKX_DOMAINS.get(region, OKX_DOMAINS["eea"])[0]

    async def _signed(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> list:
        request_path = f"{path}?{urlencode(params)}" if params else path
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        sign = base64.b64encode(hmac.new(self.secret.encode(), f"{ts}GET{request_path}".encode(), hashlib.sha256).digest())
        resp = await client.get(f"{self.api}{request_path}", headers={
            "OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign.decode(), "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase})
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400 or body.get("code") != "0":
            raise RuntimeError(f"OKX {path}: {body.get('msg') or resp.status_code}")
        return body["data"]

    async def check(self, client: httpx.AsyncClient) -> dict:
        """Test the key: it must work and must not be able to withdraw. Returns its permissions."""
        config = (await self._signed(client, "/api/v5/account/config"))[0]
        perms = {p.strip() for p in config.get("perm", "").split(",") if p.strip()}
        if "withdraw" in perms:
            raise UnsafeKeyError("This OKX key allows withdrawals. Delete it on OKX and create one with only the "
                                 "'Read' permission.")
        return {"permissions": sorted(perms), "can_trade": "trade" in perms}

    async def snapshot(self, client: httpx.AsyncClient, markets: dict[str, Market]) -> Snapshot:
        perms = set((await self.check(client))["permissions"])
        details = (await self._signed(client, "/api/v5/account/balance"))[0].get("details", [])
        balances = {d["ccy"]: (float(d.get("availBal") or 0), float(d.get("frozenBal") or 0)) for d in details
                    if float(d.get("cashBal") or 0) > 0}

        pairs_to_coin = {m.pair: c for c, m in markets.items()}
        orders = []
        for o in await self._signed(client, "/api/v5/trade/orders-pending", {"instType": "SPOT"}):
            if o["side"] == "sell" and o["instId"] in pairs_to_coin and o.get("px"):
                orders.append(Order(o["instId"], o["ordId"], "target", float(o["px"]), None,
                                    float(o["sz"]) - float(o.get("accFillSz") or 0), int(o["cTime"])))
        for kinds in ("conditional,oco", "move_order_stop"):
            for a in await self._signed(client, "/api/v5/trade/orders-algo-pending", {"instType": "SPOT", "ordType": kinds}):
                if a["side"] != "sell":
                    continue
                qty, t = float(a.get("sz") or 0), int(a.get("cTime") or 0)
                if a["ordType"] == "move_order_stop":
                    orders.append(Order(a["instId"], a["algoId"], "trailing", None, None, qty, t))
                    continue
                if a.get("slTriggerPx"):
                    orders.append(Order(a["instId"], a["algoId"] + ":sl", "stop", None, float(a["slTriggerPx"]), qty, t))
                if a.get("tpTriggerPx"):
                    orders.append(Order(a["instId"], a["algoId"] + ":tp", "target", float(a["tpTriggerPx"]), None, qty, t))

        return Snapshot(can_trade="trade" in perms, balances=balances, orders=orders,
                        fee_rate=await self._fee(client), funding_usd=await self._funding_usd(client, markets),
                        earn_usd=await self._earn_usd(client, markets))

    async def new_trades(self, client: httpx.AsyncClient, pair: str, last_id: str | None) -> list[Trade]:
        """Fills newer than `last_id` (bill ids), paging forward; or the latest few pages on first sync."""
        out, cursor = [], last_id
        for _ in range(self.MAX_PAGES):
            params = {"instType": "SPOT", "instId": pair, "limit": 100}
            if cursor:
                params["before" if last_id else "after"] = cursor
            rows = await self._signed(client, "/api/v5/trade/fills-history", params)
            if not rows:
                break
            out += [Trade(pair, r["billId"], r["ordId"], float(r["fillPx"]), float(r["fillSz"]),
                          float(r["fillPx"]) * float(r["fillSz"]), abs(float(r.get("fee") or 0)), r.get("feeCcy") or "",
                          int(r["ts"]), r["side"] == "buy") for r in rows]
            ids = [int(r["billId"]) for r in rows]
            cursor = str(max(ids) if last_id else min(ids))  # newest-first pages: move toward newer or older
            if len(rows) < 100:
                break
        return out

    async def _funding_usd(self, client, markets: dict[str, Market]) -> float | None:
        try:
            rows = await self._signed(client, "/api/v5/asset/balances")
        except Exception as e:
            log.info("funding account not readable: %s", e)
            return None
        return sum(value_usd(r["ccy"], float(r.get("bal") or 0), markets) for r in rows)

    async def _earn_usd(self, client, markets: dict[str, Market]) -> float:
        try:
            rows = await self._signed(client, "/api/v5/finance/savings/balance")
        except Exception:
            return 0.0
        return sum(value_usd(r["ccy"], float(r.get("amt") or 0), markets) for r in rows)

    async def _fee(self, client) -> float | None:
        try:
            row = (await self._signed(client, "/api/v5/account/trade-fee", {"instType": "SPOT"}))[0]
            return abs(float(row["taker"]))
        except Exception:
            return None


def build_account(creds: dict | None) -> ExchangeAccount | None:
    """An OKX connector for one user's decrypted credentials (users.okx_credentials), or None."""
    if not creds:
        return None
    return OkxAccount(creds["okx_api_key"], creds["okx_api_secret"], creds["okx_api_passphrase"],
                      creds.get("okx_region") or "eea")
