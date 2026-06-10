"""YES/NO arbitrage scanner (record-only in Phase 1~3).

In a binary market YES + NO must resolve to exactly $1. If
    yes_ask + no_ask < 1 - 2 * cost
buying both legs locks in a riskless profit at resolution. Real execution has
leg risk (only one side fills), so for now we only record opportunities to
measure how often they actually appear and how deep they are.

Every candidate that clears the raw edge threshold is validated against the
usual ways a "36% riskless edge" turns out to be a bug: stale books, books
recorded far apart in time, closed/expiring markets, and empty/thin asks.
Rejected candidates are still persisted (with status=<reason>) so outliers can
be audited later; only status='ok' rows count as real opportunities.
"""
from __future__ import annotations

from ..config import Settings
from ..storage.models import ArbOpportunity, Market, OrderBook


def scan(
    market: Market,
    yes_book: OrderBook | None,
    no_book: OrderBook | None,
    now: float,
    cfg: Settings,
) -> ArbOpportunity | None:
    if yes_book is None or no_book is None:
        return None
    ya, na = yes_book.best_ask, no_book.best_ask
    if ya is None or na is None:
        return None
    total = ya + na
    edge = 1.0 - total - 2.0 * cfg.cost
    if edge < cfg.arb_min_edge:
        return None

    status = _validate(market, yes_book, no_book, now, cfg)
    return ArbOpportunity(
        ts=now,
        condition_id=market.condition_id,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        yes_ask=ya,
        no_ask=na,
        total=total,
        edge=edge,
        yes_size=yes_book.ask_size_at_or_below(ya),
        no_size=no_book.ask_size_at_or_below(na),
        yes_book_ts=yes_book.ts,
        no_book_ts=no_book.ts,
        status=status,
    )


def _validate(market: Market, yes_book: OrderBook, no_book: OrderBook,
              now: float, cfg: Settings) -> str:
    """Return 'ok' or the first rejection reason."""
    # crossed book = corrupted state; the #1 source of fake "riskless" edges
    if yes_book.crossed or no_book.crossed:
        return "crossed_book"
    if market.closed or not market.active:
        return "market_closed"
    if now >= market.end_ts:
        return "market_expired"
    if (market.end_ts - now) <= cfg.expiry_cutoff_s:
        return "near_expiry"
    # both legs must belong to this market (guards against token mapping bugs)
    if yes_book.token_id != market.yes_token_id or no_book.token_id != market.no_token_id:
        return "token_mismatch"
    # stale book on either side
    yes_age = now - yes_book.ts
    no_age = now - no_book.ts
    if max(yes_age, no_age) > cfg.arb_max_book_age_s:
        return "stale_book"
    # books observed too far apart from each other
    if abs(yes_book.ts - no_book.ts) > cfg.arb_max_book_gap_s:
        return "book_ts_gap"
    # enough visible depth on both asks to actually take both legs
    if (yes_book.ask_size_at_or_below(yes_book.best_ask) < cfg.arb_min_depth
            or no_book.ask_size_at_or_below(no_book.best_ask) < cfg.arb_min_depth):
        return "thin_depth"
    return "ok"
