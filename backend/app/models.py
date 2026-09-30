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
class Signal:
    """Aggregated view of what the followed traders hold in one market."""

    market_key: str
    asset_class: str
    symbol: str
    title: str
    direction: str
    price_key: str
    conviction: float  # score-weighted support for `direction` minus support for the other side
    agreement: float  # share of total weighted support on `direction` (0..1)
    n_traders: int
    n_opposing: int
    total_size_usd: float
    avg_entry: float
    mark_price: float
    move_since_entry: float  # signed so positive = holders are in profit
    sources: list[str] = field(default_factory=list)
    url: str | None = None


@dataclass
class Positioning:
    """How an exchange's top traders are positioned on one coin."""

    exchange: str
    symbol: str
    long_share: float  # 0..1 share of top-trader positions that are long
    long_share_24h: float | None  # same, 24h ago
    funding: float | None = None  # current funding rate per 8h (fraction)

    @property
    def change_24h(self) -> float | None:
        return None if self.long_share_24h is None else self.long_share - self.long_share_24h


@dataclass
class Flow:
    """Followed traders entering and leaving one side of a market in the last 24h."""

    buyers: int = 0
    sellers: int = 0


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Pick:
    """A concrete spot trade suggestion: buy `size_usd` of `symbol` on your exchange."""

    market_key: str
    symbol: str
    pair: str  # OKX spot pair, e.g. DOGE-USDT
    strength: str  # "Strong" (3 of 3 checks) | "Good" (2 of 3)
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
    trail_pct: float | None = None  # pump rides: trailing stop distance below the highest price since buying
    features: dict = field(default_factory=dict)  # numeric inputs behind the pick, recorded for learning

    @property
    def direction(self) -> str:
        return "long"
