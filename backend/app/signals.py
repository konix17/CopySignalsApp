"""Which of the followed traders' positions count.

Positions under MIN_ALLOCATION of the trader's book are dust (leftovers, hedges, tests) and don't count: they're
left out of the position log, so they never become swing copies.
"""

from collections import defaultdict

from .models import Position

MIN_ALLOCATION = 0.02


def meaningful_positions(positions: list[Position]) -> list[Position]:
    """Drop dust: positions under MIN_ALLOCATION of their trader's book."""
    book: dict[tuple[str, str], float] = defaultdict(float)
    for p in positions:
        book[(p.source, p.address)] += p.size_usd
    return [p for p in positions if p.size_usd > 0 and p.size_usd >= MIN_ALLOCATION * book[(p.source, p.address)]]
