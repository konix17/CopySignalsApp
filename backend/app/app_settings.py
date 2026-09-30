"""App-wide defaults an admin can change in the admin panel (stored in prefs as "setting.<name>").

Each entry: (default, minimum, maximum, description). Values outside the range are rejected.
"""

import sqlite3

from . import db

SPEC: dict[str, tuple[float, float, float, str]] = {
    "refresh_minutes": (10, 2, 60, "How often leaderboards, crowding and trends are refreshed (minutes); "
                                   "positions, prices, picks and accounts update every minute"),
    "default_bankroll": (1000, 10, 100_000_000, "Bankroll for users without a connected OKX balance ($)"),
    "default_demo_balance": (10_000, 100, 100_000_000, "Starting balance of a new or reset demo account ($)"),
    "default_risk_budget_pct": (0.10, 0, 0.5, "High-risk budget as a share of the bankroll, unless a user sets one"),
}


def get(conn: sqlite3.Connection, name: str) -> float:
    default = SPEC[name][0]
    return float(db.get_pref(conn, f"setting.{name}", str(default)))


def all_values(conn: sqlite3.Connection) -> dict:
    return {k: {"value": get(conn, k), "default": d, "min": lo, "max": hi, "label": label}
            for k, (d, lo, hi, label) in SPEC.items()}


def set_value(conn: sqlite3.Connection, name: str, value: float) -> None:
    if name not in SPEC:
        raise ValueError(f"Unknown setting {name!r}")
    _, lo, hi, _ = SPEC[name]
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    db.set_pref(conn, f"setting.{name}", str(value))
