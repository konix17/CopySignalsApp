from dataclasses import dataclass, field

WINDOWS = ("day", "week", "month", "allTime")
PERP = "perp"


@dataclass
class TraderStat:
    """One trader's performance over one window, as reported by a source."""

    source: str
    address: str
    window: str
    pnl: float
    roi: float | None  # fraction: 0.25 == +25%
    volume: float | None = None
    account_value: float | None = None
    win_rate: float | None = None
    name: str | None = None
    score: float = 0.0  # filled in by scoring.score_stats


@dataclass
class Position:
    """An open position held by a followed trader."""

    source: str
    address: str
    market_key: str  # "perp:BTC": same key across venues, so positions merge
    asset_class: str
    symbol: str
    title: str
    direction: str  # "long" | "short"
    size_usd: float
    entry_price: float
    mark_price: float
    price_key: str
    leverage: float | None = None
    unrealized_pnl: float | None = None
    opened_at: int | None = None  # exact open time when the venue exposes it (GMX)
    url: str | None = None


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Pick:
    """A concrete spot trade suggestion: buy `size_usd` of `symbol` on your exchange (a swing copy, swing.py)."""

    market_key: str
    symbol: str
    pair: str  # OKX spot pair, e.g. DOGE-USDT
    strength: str  # "Copy"
    score: float  # ranking only
    checks: list[Check]
    price: float  # exchange spot price now
    stop_price: float
    target_price: float
    stop_pct: float  # negative
    target_pct: float
    cost_pct: float  # round-trip fees + spread + slippage
    hold_days: float
    hold_basis: str  # "observed" | "estimated"
    size_usd: float
    net_win_usd: float
    net_loss_usd: float
    n_traders: int
    buyers_24h: int
    sellers_24h: int
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)  # numeric inputs behind the pick, recorded for learning

    @property
    def direction(self) -> str:
        return "long"
