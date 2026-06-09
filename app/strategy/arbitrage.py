"""YES/NO arbitrage scanner (record-only in Phase 1~3).

In a binary market YES + NO must resolve to exactly $1. If
    yes_ask + no_ask < 1 - 2 * cost
buying both legs locks in a riskless profit at resolution. Real execution has
leg risk (only one side fills), so for now we only record opportunities to
measure how often they actually appear and how deep they are.
"""
from __future__ import annotations

from ..storage.models import ArbOpportunity, Market, OrderBook


def scan(
    market: Market,
    yes_book: OrderBook | None,
    no_book: OrderBook | None,
    now: float,
    cost_per_leg: float,
    min_edge: float,
) -> ArbOpportunity | None:
    if yes_book is None or no_book is None:
        return None
    ya, na = yes_book.best_ask, no_book.best_ask
    if ya is None or na is None:
        return None
    total = ya + na
    edge = 1.0 - total - 2.0 * cost_per_leg
    if edge < min_edge:
        return None
    return ArbOpportunity(
        ts=now,
        condition_id=market.condition_id,
        yes_ask=ya,
        no_ask=na,
        total=total,
        edge=edge,
        yes_size=yes_book.ask_size_at_or_below(ya),
        no_size=no_book.ask_size_at_or_below(na),
    )
