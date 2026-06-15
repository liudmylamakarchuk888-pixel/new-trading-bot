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

    # master switch — when false, evaluate/log only; no new orders or exits
    trade_enabled: bool = False

    # strategy
    min_edge: float = 0.10              # enter when edge >= this (hysteresis high)
    exit_edge: float = 0.01             # legacy hysteresis low bound (see cancel_if_edge_below)
    cancel_if_edge_below: float = 0.00  # cancel resting order when edge drops below this
    cost: float = 0.01
    maker_rebate: float = 0.0           # added to real_edge when filled as maker
    max_spread: float = 0.08
    expiry_cutoff_min: float = 5.0
    eval_interval_s: float = 5.0
    tick: float = 0.01
    vol_lambda: float = 0.94
    min_vol_candles: int = 60
    max_spot_age_s: float = 120.0
    allow_no_side: bool = False         # disable NO entries until a separate model exists

    # order churn control
    order_ttl_seconds: float = 20.0     # cancel stale resting orders after this
    cancel_if_adverse_spot_bps: float = 5.0  # cancel if spot moved against us since order
    replace_only_if_price_improves_ticks: int = 2  # min tick improvement to replace quote
    order_cooldown_s: float = 90.0      # same token: wait after a (non-replace) cancel
    market_cooldown_s: float = 90.0     # same market: min gap between new orders
    replace_edge_threshold: float = 0.01  # replace only if |fair-old| >= this
    expiry_taper_min: float = 15.0        # halve order size this close to expiry
    expiry_cancel_min: float = 2.0        # cancel all open orders this close to expiry
    zero_fill_cancel_limit: int = 25      # consecutive zero-fill cancels -> market cooldown
    zero_fill_cooldown_s: float = 3600.0  # stop quoting a market for this long

    # paper universe (backtest replays all recorded markets; paper filters by TTE)
    paper_max_tte_hours: float = 72.0   # skip markets expiring later than this
    paper_min_tte_hours: float = 24.0   # skip markets expiring sooner than this

    # paper settlement (Gamma is preferred; fallback matches backtest)
    paper_settle_fallback: bool = True
    paper_settle_grace_s: float = 300.0   # wait after expiry before spot fallback

    # quote placement: conservative (default) | join_bid | improve_tick (fill test)
    quote_mode: str = "conservative"

    # position exit (paper sells simulated at best bid)
    exit_enabled: bool = True
    exit_edge_threshold: float = 0.0    # exit when current edge < this
    exit_take_profit_pct: float = 0.30  # partial exit when bid >= avg + this
    exit_take_profit_frac: float = 0.50 # fraction of position to sell on take-profit
    exit_near_expiry_hours: float = 2.0
    exit_near_expiry_prob: float = 0.98  # reduce/exit when prob below this near expiry

    # calibration gate — only enter setups with proven historical edge
    require_calibrated_bucket: bool = True
    min_bucket_settlements: int = 50
    max_adverse_move_pct: float = 60.0  # block setup if post-fill adverse rate exceeds this

    # YES/NO arbitrage scanner (record-only)
    arb_min_edge: float = 0.005
    arb_log_interval_s: float = 60.0
    arb_max_book_age_s: float = 30.0      # either book older than this vs now -> stale
    arb_max_book_gap_s: float = 1.0       # |yes_book.ts - no_book.ts| above this -> reject
    arb_min_depth: float = 5.0            # min visible shares at best ask on both legs
    block_crossed_book_arb: bool = True   # never log crossed-book arb candidates

    # risk (fraction-based defaults kept for backward compat)
    bankroll: float = 500.0
    max_trade_frac: float = 0.01
    max_market_exposure_frac: float = 0.03
    daily_loss_frac: float = 0.03
    weekly_loss_frac: float = 0.08
    max_consecutive_losses: int = 3

    # risk (absolute USD caps — used when > 0, otherwise fall back to frac * bankroll)
    max_trade_usd: float = 0.0
    max_market_exposure_usd: float = 5.0
    max_underlying_day_exposure_usd: float = 25.0
    max_total_exposure_usd: float = 100.0
    daily_loss_limit_usd: float = 10.0

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

    def effective_trade_size_usd(self) -> float:
        if self.max_trade_usd > 0:
            return self.max_trade_usd
        return self.bankroll * self.max_trade_frac

    def effective_max_market_exposure_usd(self) -> float:
        if self.max_market_exposure_usd > 0:
            return self.max_market_exposure_usd
        return self.bankroll * self.max_market_exposure_frac

    def effective_daily_loss_limit_usd(self) -> float:
        if self.daily_loss_limit_usd > 0:
            return self.daily_loss_limit_usd
        return self.bankroll * self.daily_loss_frac


def load_settings() -> Settings:
    return Settings()
