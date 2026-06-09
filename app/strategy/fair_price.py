"""Fair probability model for terminal price markets ("asset above/below $K at time T").

Treats the market as a zero-drift digital option:
    P(S_T >= K) = N(d2),  d2 = (ln(S/K) - sigma^2 * t / 2) / (sigma * sqrt(t))

sigma is an EWMA realized volatility estimated from 1-minute log returns and
annualized. If there is not enough candle history the model returns None and
the market is skipped (no estimate -> no trade).
"""
from __future__ import annotations

import math

from ..storage.models import Market

SECONDS_PER_YEAR = 365.0 * 24 * 3600
MINUTES_PER_YEAR = 365.0 * 24 * 60


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ewma_vol_per_minute(closes: list[float], lam: float = 0.94, min_obs: int = 60) -> float | None:
    """EWMA stdev of 1-minute log returns. Returns None if history is insufficient."""
    if len(closes) < min_obs + 1:
        return None
    var: float | None = None
    prev = closes[0]
    for c in closes[1:]:
        if prev <= 0 or c <= 0:
            prev = c
            continue
        r = math.log(c / prev)
        prev = c
        var = r * r if var is None else lam * var + (1.0 - lam) * r * r
    if var is None or var <= 0:
        return None
    return math.sqrt(var)


def annualized_vol(closes: list[float], lam: float = 0.94, min_obs: int = 60) -> float | None:
    v = ewma_vol_per_minute(closes, lam=lam, min_obs=min_obs)
    if v is None:
        return None
    return v * math.sqrt(MINUTES_PER_YEAR)


def prob_above(spot: float, strike: float, sigma_ann: float | None, t_seconds: float) -> float:
    """P(S_T >= K) under zero-drift GBM."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_seconds <= 0 or sigma_ann is None or sigma_ann <= 0:
        return 1.0 if spot >= strike else 0.0
    t = t_seconds / SECONDS_PER_YEAR
    d2 = (math.log(spot / strike) - 0.5 * sigma_ann * sigma_ann * t) / (sigma_ann * math.sqrt(t))
    return norm_cdf(d2)


def fair_yes_probability(market: Market, spot: float, sigma_ann: float | None, now: float) -> float:
    """Fair YES probability for an above/below terminal market, clamped away from 0/1."""
    p = prob_above(spot, market.strike, sigma_ann, market.end_ts - now)
    if market.direction == "below":
        p = 1.0 - p
    return min(0.999, max(0.001, p))
