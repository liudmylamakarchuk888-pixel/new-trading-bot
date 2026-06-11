"""Shared market settlement: recorded outcome or spot vs strike fallback."""
from __future__ import annotations

from ..storage.models import Market

# Cancel reasons that should not increment zero-fill churn counters.
ZERO_FILL_EXCLUDE_CANCEL = frozenset({
    "replace", "market_expiry", "market_closed", "backtest_end", "manual_shutdown",
})


def outcome_from_spot(market: Market, spot: float | None) -> float | None:
    if spot is None:
        return None
    hit = spot >= market.strike if market.direction == "above" else spot <= market.strike
    return 1.0 if hit else 0.0


def resolve_outcome(market: Market, spot: float | None) -> float | None:
    """Prefer a recorded resolution; otherwise derive from spot at expiry."""
    if market.outcome is not None:
        return market.outcome
    return outcome_from_spot(market, spot)
