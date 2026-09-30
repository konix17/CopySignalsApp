"""Turn followed traders' open positions into ranked trade signals.

Each holder contributes `score * (0.5 + 0.5 * allocation)` to the side they
hold, where allocation is the position's share of that trader's book on that
source. So a trader counts for at least half their score just by holding, and
up to their full score when it's their biggest bet. Conviction is the winning
side's support minus the opposing sides' support, so contested markets sink.

Positions under MIN_ALLOCATION of the trader's book are dust (leftovers,
hedges, tests) and don't count.

Perps from different venues share a market_key ("perp:BTC"), so a long on
Hyperliquid and a long on GMX add up.
"""

from collections import defaultdict

from .models import PERP, Position, Signal

MIN_ALLOCATION = 0.02


def meaningful_positions(positions: list[Position]) -> list[Position]:
    """Drop dust: positions under MIN_ALLOCATION of their trader's book."""
    book: dict[tuple[str, str], float] = defaultdict(float)
    for p in positions:
        book[(p.source, p.address)] += p.size_usd
    return [p for p in positions if p.size_usd > 0 and p.size_usd >= MIN_ALLOCATION * book[(p.source, p.address)]]


def build_signals(positions: list[Position], scores: dict[tuple[str, str], float]) -> list[Signal]:
    """`scores` maps (source, address) -> score for the window being ranked.
    Positions from traders not in `scores` are ignored."""
    book: dict[tuple[str, str], float] = defaultdict(float)
    for p in positions:
        book[(p.source, p.address)] += p.size_usd

    # market_key -> direction -> list of (position, contribution)
    grouped: dict[str, dict[str, list[tuple[Position, float]]]] = defaultdict(lambda: defaultdict(list))
    for p in positions:
        score = scores.get((p.source, p.address), 0.0)
        if score <= 0 or p.size_usd <= 0:
            continue
        alloc = p.size_usd / book[(p.source, p.address)]
        if alloc < MIN_ALLOCATION:
            continue
        grouped[p.market_key][p.direction].append((p, score * (0.5 + 0.5 * alloc)))

    signals = []
    for market_key, sides in grouped.items():
        support = {d: sum(c for _, c in rows) for d, rows in sides.items()}
        direction = max(support, key=support.get)
        total = sum(support.values())
        rows = sides[direction]
        holders = {(p.source, p.address) for p, _ in rows}
        opposing = {(p.source, p.address) for d, rs in sides.items() if d != direction for p, _ in rs}
        size = sum(p.size_usd for p, _ in rows)
        avg_entry = sum(p.entry_price * p.size_usd for p, _ in rows) / size
        # Latest mark: take it from the largest holder (all holders see ~the same price).
        lead = max(rows, key=lambda r: r[0].size_usd)[0]
        move = (lead.mark_price / avg_entry - 1) if avg_entry else 0.0
        if lead.asset_class == PERP and direction == "short":
            move = -move
        signals.append(
            Signal(
                market_key=market_key,
                asset_class=lead.asset_class,
                symbol=lead.symbol,
                title=lead.title,
                direction=direction,
                price_key=lead.price_key,
                conviction=round(support[direction] - (total - support[direction]), 2),
                agreement=round(support[direction] / total, 3),
                n_traders=len(holders),
                n_opposing=len(opposing),
                total_size_usd=round(size, 2),
                avg_entry=avg_entry,
                mark_price=lead.mark_price,
                move_since_entry=round(move, 4),
                sources=sorted({p.source for p, _ in rows}),
                url=lead.url,
            )
        )
    signals.sort(key=lambda s: -s.conviction)
    return signals
