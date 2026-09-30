"""Trader scoring: who counts as a trader worth following.

Research on copy trading shows leaderboard returns alone persist poorly (short
hot streaks, survivorship), while behaviour (consistency, controlled drawdowns,
not being a bot) persists better. So:

Eligibility (otherwise score 0):
- Profitable in at least 2 of week / month / all time: no one-streak wonders.
- Not a market maker or bot: huge volume relative to account size with a tiny
  return means they're providing liquidity, not betting on direction.
- Not reckless: lost less than 50% of their typical balance at some point in
  the last month (only venues that publish PnL history: Hyperliquid).

Score, percentile based within a source and window (scale free, comparable
across venues):
    score = 100 * (0.45 * roi_pct + 0.35 * pnl_pct + 0.20 * consistency) * (1 - drawdown penalty)
- ROI above a sanity cap (deposit-timing artifacts, e.g. +6000% in a month) is
  ignored and PnL rank is used instead.
- Drawdown penalty: the worst peak-to-trough drop in the last month, as a share
  of the trader's typical balance, subtracted from the score (below the 50% cutoff).
"""

import statistics
from collections import defaultdict

from .models import WINDOWS, TraderStat

W_ROI, W_PNL, W_CONSISTENCY = 0.45, 0.35, 0.20
ROI_CAP = {"day": 1.0, "week": 3.0, "month": 10.0, "allTime": 50.0}
LONG_WINDOWS = ("week", "month", "allTime")
MM_TURNOVER = 200  # monthly volume / account size
MM_MAX_ROI = 0.03
MAX_DRAWDOWN = 0.5  # at or above this, the trader is excluded


def percentile_ranks(values: list[float]) -> list[float]:
    """Rank each value in [0, 1]; ties share their average rank."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def eligible_traders(stats: list[TraderStat], drawdowns: dict[str, float] | None = None) -> set[tuple[str, str]]:
    drawdowns = drawdowns or {}
    rows: dict[tuple[str, str], dict[str, TraderStat]] = defaultdict(dict)
    for s in stats:
        rows[(s.source, s.address)][s.window] = s
    out = set()
    for key, by_window in rows.items():
        positive = sum(1 for w in LONG_WINDOWS if w in by_window and by_window[w].pnl > 0)
        if positive < 2 or drawdowns.get(key[1], 0.0) >= MAX_DRAWDOWN:
            continue
        month = by_window.get("month")
        if month and month.volume and month.account_value:
            turnover = month.volume / month.account_value
            if turnover > MM_TURNOVER and (month.roi or 0) < MM_MAX_ROI:
                continue
        out.add(key)
    return out


def consistency_by_trader(stats: list[TraderStat]) -> dict[tuple[str, str], float]:
    windows: dict[tuple[str, str], list[TraderStat]] = defaultdict(list)
    for s in stats:
        windows[(s.source, s.address)].append(s)
    out = {}
    for key, rows in windows.items():
        positive = sum(1 for r in rows if r.pnl > 0) / len(rows)
        rates = [r.win_rate for r in rows if r.win_rate is not None]
        out[key] = (positive + sum(rates) / len(rates)) / 2 if rates else positive
    return out


def score_stats(stats: list[TraderStat], drawdowns: dict[str, float] | None = None) -> None:
    """Set `.score` on every stat in place. `drawdowns` maps address -> max drawdown (0..1)."""
    drawdowns = drawdowns or {}
    eligible = eligible_traders(stats, drawdowns)
    consistency = consistency_by_trader(stats)
    cohorts: dict[tuple[str, str], list[TraderStat]] = defaultdict(list)
    for s in stats:
        s.score = 0.0
        if s.pnl > 0 and (s.source, s.address) in eligible:
            cohorts[(s.source, s.window)].append(s)

    for (_, window), cohort in cohorts.items():
        pnl_pct = percentile_ranks([s.pnl for s in cohort])
        cap = ROI_CAP.get(window, float("inf"))
        with_roi = [i for i, s in enumerate(cohort) if s.roi is not None and s.roi <= cap]
        roi_pct = dict(zip(with_roi, percentile_ranks([cohort[i].roi for i in with_roi])))
        for i, s in enumerate(cohort):
            r = roi_pct.get(i, pnl_pct[i])
            c = consistency[(s.source, s.address)]
            penalty = drawdowns.get(s.address, 0.0)
            s.score = round(100 * (W_ROI * r + W_PNL * pnl_pct[i] + W_CONSISTENCY * c) * (1 - penalty), 2)


def max_drawdown(pnl_history: list[float], equity_history: list[float]) -> float:
    """Worst peak-to-trough fall in cumulative PnL, relative to the median account balance.
    PnL (not balance) is used so deposits and withdrawals don't look like gains or losses,
    and the median balance so a deposit mid-month doesn't shrink or inflate the ratio."""
    if not pnl_history or not equity_history:
        return 0.0
    worst, peak = 0.0, float("-inf")
    for pnl in pnl_history:
        peak = max(peak, pnl)
        worst = max(worst, peak - pnl)
    typical = statistics.median(equity_history)
    return min(worst / typical, 1.0) if typical > 0 else 0.0


def top_traders(stats: list[TraderStat], n: int) -> dict[str, list[TraderStat]]:
    """Top-n scored traders per window (for a single source)."""
    by_window: dict[str, list[TraderStat]] = {w: [] for w in WINDOWS}
    for s in stats:
        if s.score > 0 and s.window in by_window:
            by_window[s.window].append(s)
    return {w: sorted(rows, key=lambda s: -s.score)[:n] for w, rows in by_window.items()}
