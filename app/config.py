"""Bot configuration. Override via .env or environment variables (BOT_ prefix)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BOT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # storage (PostgreSQL). Switch local/VPS via BOT_DATABASE_URL in .env
    database_url: str = "postgresql://postgres:postgres@localhost:5432/trading_bot"
    flush_interval_s: float = 1.0
    book_snapshot_interval_s: float = 2.0

    # universe
    assets: list[str] = ["BTC", "ETH"]
    market_window_days: float = 7.0
    market_refresh_s: float = 300.0

    # strategy
    min_edge: float = 0.05              # enter when edge >= this (hysteresis high)
    exit_edge: float = 0.01             # keep resting order until edge drops below this
    cost: float = 0.01
    max_spread: float = 0.08
    expiry_cutoff_min: float = 5.0
    eval_interval_s: float = 5.0
    tick: float = 0.01
    vol_lambda: float = 0.94
    min_vol_candles: int = 60
    max_spot_age_s: float = 120.0

    # order churn control
    order_cooldown_s: float = 90.0      # same token: wait after a (non-replace) cancel
    market_cooldown_s: float = 90.0     # same market: min gap between new orders
    replace_edge_threshold: float = 0.01  # replace only if |fair-old| >= this
    expiry_taper_min: float = 15.0        # halve order size this close to expiry
    expiry_cancel_min: float = 2.0        # cancel all open orders this close to expiry
    zero_fill_cancel_limit: int = 25      # consecutive zero-fill cancels -> market cooldown
    zero_fill_cooldown_s: float = 3600.0  # stop quoting a market for this long

    # paper universe (backtest replays all recorded markets; paper filters by TTE)
    paper_max_tte_hours: float = 48.0   # skip markets expiring later than this
    paper_min_tte_hours: float = 0.0    # skip markets expiring sooner (0 = expiry_cutoff only)

    # paper settlement (Gamma is preferred; fallback matches backtest)
    paper_settle_fallback: bool = True
    paper_settle_grace_s: float = 300.0   # wait after expiry before spot fallback

    # quote placement: conservative (default) | join_bid | improve_tick (fill test)
    quote_mode: str = "conservative"

    # YES/NO arbitrage scanner (record-only)
    arb_min_edge: float = 0.005
    arb_log_interval_s: float = 60.0
    arb_max_book_age_s: float = 30.0      # either book older than this vs now -> stale
    arb_max_book_gap_s: float = 1.0       # |yes_book.ts - no_book.ts| above this -> reject
    arb_min_depth: float = 5.0            # min visible shares at best ask on both legs

    # risk
    bankroll: float = 500.0
    max_trade_frac: float = 0.01
    max_market_exposure_frac: float = 0.03
    daily_loss_frac: float = 0.03
    weekly_loss_frac: float = 0.08
    max_consecutive_losses: int = 3

    # kill switch
    kill_switch_file: str = "KILL_SWITCH"

    @property
    def expiry_cutoff_s(self) -> float:
        return self.expiry_cutoff_min * 60.0

    @property
    def paper_max_tte_s(self) -> float:
        return self.paper_max_tte_hours * 3600.0

    @property
    def paper_min_tte_s(self) -> float:
        return self.paper_min_tte_hours * 3600.0

    @property
    def expiry_taper_s(self) -> float:
        return self.expiry_taper_min * 60.0

    @property
    def expiry_cancel_s(self) -> float:
        return self.expiry_cancel_min * 60.0


def load_settings() -> Settings:
    return Settings()
