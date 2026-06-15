"""Risk engine: position sizing and hard loss limits.

Enforced identically in paper trading and backtest:
  - per-trade size:        max_trade_usd or bankroll * max_trade_frac
  - per-market exposure:   max_market_exposure_usd or bankroll * max_market_exposure_frac
  - per underlying/day:    max_underlying_day_exposure_usd
  - total portfolio:       max_total_exposure_usd
  - daily realized loss:   daily_loss_limit_usd or bankroll * daily_loss_frac
  - weekly realized loss:  bankroll * weekly_loss_frac
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
        self.halted_reason: str | None = None

    def trade_size_usd(self) -> float:
        return self.cfg.effective_trade_size_usd()

    def max_market_exposure_usd(self) -> float:
        return self.cfg.effective_max_market_exposure_usd()

    def daily_loss_limit_usd(self) -> float:
        return self.cfg.effective_daily_loss_limit_usd()

    def check_entry(
        self,
        now: float,
        market_exposure_usd: float,
        add_usd: float,
        total_exposure_usd: float = 0.0,
        underlying_day_exposure_usd: float = 0.0,
    ) -> tuple[bool, str]:
        if self.halted_reason:
            return False, self.halted_reason
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halted_reason = "consecutive_losses"
            log.warning("RISK HALT: %d consecutive losses", self.consecutive_losses)
            return False, self.halted_reason
        if self.daily_pnl.get(_day_key(now), 0.0) <= -self.daily_loss_limit_usd():
            return False, "daily_loss_limit"
        if self.weekly_pnl.get(_week_key(now), 0.0) <= -self.cfg.bankroll * self.cfg.weekly_loss_frac:
            return False, "weekly_loss_limit"
        if market_exposure_usd + add_usd > self.max_market_exposure_usd():
            return False, "market_exposure_limit"
        if (self.cfg.max_total_exposure_usd > 0
                and total_exposure_usd + add_usd > self.cfg.max_total_exposure_usd):
            return False, "total_exposure_limit"
        if (self.cfg.max_underlying_day_exposure_usd > 0
                and underlying_day_exposure_usd + add_usd > self.cfg.max_underlying_day_exposure_usd):
            return False, "underlying_day_exposure_limit"
        return True, ""

    def on_settlement(self, pnl: float, now: float) -> None:
        self.daily_pnl[_day_key(now)] = self.daily_pnl.get(_day_key(now), 0.0) + pnl
        self.weekly_pnl[_week_key(now)] = self.weekly_pnl.get(_week_key(now), 0.0) + pnl
        if pnl < 0:
            self.consecutive_losses += 1
        elif pnl > 0:
            self.consecutive_losses = 0

    def on_exit(self, pnl: float, now: float) -> None:
        """Early exit PnL counts toward daily/weekly limits."""
        self.on_settlement(pnl, now)

    def bootstrap(self, settlements: list[tuple[float, float]]) -> None:
        for ts, pnl in sorted(settlements):
            self.on_settlement(pnl, ts)

    def today_pnl(self, now: float) -> float:
        return self.daily_pnl.get(_day_key(now), 0.0)
