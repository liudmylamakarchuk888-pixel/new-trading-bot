"""Edge detection: trade only when fair probability beats the market price by enough.

    edge = fair - best_ask - cost
    enter only if edge >= min_edge, spread is sane, and expiry is not imminent.

Detection is deliberately conservative (measured against the ask), while the
actual paper order is placed as a maker order below the ask.

Exit hysteresis (edge dropped below exit_edge) is applied in StrategyEngine so
a resting order is not cancelled just because edge fell from 3% to 2%.
"""
from __future__ import annotations

import math

from ..config import Settings
from ..storage.models import Market, OrderBook, Signal


def evaluate_market(
    market: Market,
    fair_yes: float,
    yes_book: OrderBook | None,
    no_book: OrderBook | None,
    now: float,
    cfg: Settings,
) -> list[Signal]:
    """One Signal per side (YES / NO). action='enter' only when every filter passes."""
    out: list[Signal] = []
    sides = [
        ("YES", market.yes_token_id, fair_yes, yes_book),
        ("NO", market.no_token_id, 1.0 - fair_yes, no_book),
    ]
    expiring = (market.end_ts - now) <= cfg.expiry_cutoff_s

    for label, token_id, fair, book in sides:
        bb = book.best_bid if book else None
        ba = book.best_ask if book else None
        spread = book.spread if book else None
        edge = (fair - ba - cfg.cost) if ba is not None else None

        if book is None or ba is None or bb is None:
            action, reason = "skip", "no_liquidity"
        elif book.crossed:
            # corrupted book state (bid >= ask): prices cannot be trusted
            action, reason = "skip", "crossed_book"
        elif expiring:
            action, reason = "skip", "expiry_cutoff"
        elif spread is not None and spread > cfg.max_spread:
            action, reason = "skip", "wide_spread"
        elif edge is None or edge < cfg.min_edge:
            action, reason = "skip", "low_edge"
        else:
            action, reason = "enter", ""

        out.append(Signal(
            ts=now, condition_id=market.condition_id, token_id=token_id, label=label,
            fair=fair, best_bid=bb, best_ask=ba, spread=spread, edge=edge,
            action=action, reason=reason,
        ))
    return out


def maker_price(fair: float, best_bid: float, best_ask: float, cfg: Settings) -> float | None:
    """Maker buy price. Mode controls aggressiveness (fill test vs production).

    conservative: improve bid by one tick, capped at fair - cost - min_edge
    join_bid:     rest at best bid (max fill probability for testing)
    improve_tick: one tick above bid, no fair cap (fill test only)
    """
    mode = cfg.quote_mode.lower()
    if mode == "join_bid":
        p = best_bid
    elif mode == "improve_tick":
        p = min(best_bid + cfg.tick, best_ask - cfg.tick)
    else:
        cap = fair - cfg.cost - cfg.min_edge
        p = min(best_bid + cfg.tick, best_ask - cfg.tick, cap)
    p = math.floor(p / cfg.tick + 1e-9) * cfg.tick
    p = round(p, 4)
    if p < 0.01 or p >= best_ask:
        return None
    return p
