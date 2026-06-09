"""Risk engine: position sizing and hard loss limits.

Enforced identically in paper trading and backtest so the rules themselves get
validated before any real money is involved:
  - per-trade size:        bankroll * max_trade_frac
  - per-market exposure:   bankroll * max_market_exposure_frac
  - daily realized loss:   bankroll * daily_loss_frac   (resets next UTC day)
  - weekly realized loss:  bankroll * weekly_loss_frac  (resets next ISO week)
  - N consecutive losses:  permanent halt until restart
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Settings

log = logging.getLogger(__name__)


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _week_key(ts: float) -> str:
    d = datetime.fromtimestamp(ts, tz=timezone.utc).isocalendar()
    return f"{d.year}-W{d.week:02d}"


class RiskEngine:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.daily_pnl: dict[str, float] = {}
        self.weekly_pnl: dict[str, float] = {}
        self.consecutive_losses = 0
        self.halted_reason: str | None = None  # permanent halt (consecutive losses)

    # sizing -----------------------------------------------------------------

    def trade_size_usd(self) -> float:
        return self.cfg.bankroll * self.cfg.max_trade_frac

    def max_market_exposure_usd(self) -> float:
        return self.cfg.bankroll * self.cfg.max_market_exposure_frac

    # entry gate ----------------------------------------------------------------

    def check_entry(self, now: float, market_exposure_usd: float, add_usd: float) -> tuple[bool, str]:
        if self.halted_reason:
            return False, self.halted_reason
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halted_reason = "consecutive_losses"
            log.warning("RISK HALT: %d consecutive losses", self.consecutive_losses)
            return False, self.halted_reason
        if self.daily_pnl.get(_day_key(now), 0.0) <= -self.cfg.bankroll * self.cfg.daily_loss_frac:
            return False, "daily_loss_limit"
        if self.weekly_pnl.get(_week_key(now), 0.0) <= -self.cfg.bankroll * self.cfg.weekly_loss_frac:
            return False, "weekly_loss_limit"
        if market_exposure_usd + add_usd > self.max_market_exposure_usd():
            return False, "market_exposure_limit"
        return True, ""

    # settlement feedback ---------------------------------------------------------

    def on_settlement(self, pnl: float, now: float) -> None:
        self.daily_pnl[_day_key(now)] = self.daily_pnl.get(_day_key(now), 0.0) + pnl
        self.weekly_pnl[_week_key(now)] = self.weekly_pnl.get(_week_key(now), 0.0) + pnl
        if pnl < 0:
            self.consecutive_losses += 1
        elif pnl > 0:
            self.consecutive_losses = 0

    def bootstrap(self, settlements: list[tuple[float, float]]) -> None:
        """Restore state from prior (ts, pnl) settlements, oldest first."""
        for ts, pnl in sorted(settlements):
            self.on_settlement(pnl, ts)

    # reporting -----------------------------------------------------------------

    def today_pnl(self, now: float) -> float:
        return self.daily_pnl.get(_day_key(now), 0.0)
