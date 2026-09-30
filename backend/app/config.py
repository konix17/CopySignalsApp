"""Runtime settings, read from environment variables or a local `.env` file (see README)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", str(ROOT / "data" / "trading.db"))))
    # Encryption key for secrets stored in the database (created on first start, readable only by you).
    secret_key_path: Path = field(default_factory=lambda: Path(_env("SECRET_KEY_PATH", str(ROOT / "data" / "secret.key"))))
    log_dir: Path = field(default_factory=lambda: Path(_env("LOG_DIR", str(ROOT / "data" / "logs"))))
    # Daily price history for backtests and the trend bot (history.py), kept apart from the app's database.
    history_path: Path = field(default_factory=lambda: Path(_env("HISTORY_PATH", str(ROOT / "data" / "history.db"))))
    # Optional: research/collect_market.py's download, used once to seed the long/short test's market history.
    research_market_db: Path = field(default_factory=lambda: Path(_env("RESEARCH_MARKET_DB",
                                                                       str(ROOT / "data" / "market.db"))))
    # Host names the app answers to (blocks DNS-rebinding attacks). Add your domain when you host it.
    allowed_hosts: tuple[str, ...] = tuple(h.strip() for h in _env("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip())
    # Send cookies only over HTTPS. Turn on as soon as the app is served over HTTPS.
    https: bool = _env("HTTPS", "0") == "1"

    # --- Costs ---
    # Per side, used until a user enters their own OKX fees in Settings (OKX EU spot: 0.20% taker, 0.10% maker).
    fee_rate: float = float(_env("FEE_RATE", "0.002"))  # taker: market orders and triggered stops
    maker_fee_rate: float = float(_env("MAKER_FEE_RATE", "0.001"))
    slippage: float = float(_env("SLIPPAGE", "0.0005"))  # per side, on top of half the spread

    # --- Alerts ---
    notify_webhook_url: str = _env("NOTIFY_WEBHOOK_URL", "")

    # --- OKX market data. Each user's read-only OKX key is stored encrypted with their account. ---
    # "eea" for Europe (my.okx.com; API on eea.okx.com), "global" otherwise.
    okx_region: str = _env("OKX_REGION", "eea")


settings = Settings()
