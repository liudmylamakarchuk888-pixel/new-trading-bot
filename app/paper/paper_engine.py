"""Paper trading core: maker-only virtual broker + strategy engine.

The same StrategyEngine drives live paper trading (fed by websockets) and the
backtester (fed by recorded events) - both push books/trades into a DataHub
and call evaluate()/on_trade()/on_book() with an explicit `now`.

Fill model (deliberately conservative, maker-only):
  - a resting buy order fills only when the market trades *through* its price
    (trade strictly below our bid), assuming we are last in the queue, or
  - the book crosses our bid (best ask <= our price), capped by visible ask size.
Market orders are never simulated.
"""
from __future__ import annotations

import logging
import uuid

from ..config import Settings
from ..data.recorder import DataHub
from ..risk import kill_switch
from ..risk.risk_engine import RiskEngine
from ..storage.db import Sink
from ..storage.models import Market, OrderBook, PaperOrder, Signal, TradeTick
from ..strategy import arbitrage
from ..strategy.edge_detector import evaluate_market, maker_price
from ..strategy.fair_price import annualized_vol, fair_yes_probability
from .position_manager import PositionManager
from .settlement import ZERO_FILL_EXCLUDE_CANCEL

log = logging.getLogger(__name__)

EPS = 1e-9

# skip reason -> cancel_reason persisted on paper_orders
_CANCEL_REASON = {
    "expiry_cutoff": "market_expiry",
    "crossed_book": "stale_book",
    "no_liquidity": "stale_book",
    "wide_spread": "price_moved",
    "low_edge": "edge_dropped",
}


class PaperBroker:
    """Holds open virtual maker orders; at most one order per token."""

    def __init__(self):
        self.orders: dict[str, PaperOrder] = {}        # order_id -> order
        self._by_token: dict[str, str] = {}            # token_id -> order_id

    def order_for_token(self, token_id: str) -> PaperOrder | None:
        oid = self._by_token.get(token_id)
        return self.orders.get(oid) if oid else None

    def place(self, order: PaperOrder) -> None:
        self.orders[order.id] = order
        self._by_token[order.token_id] = order.id

    def remove(self, order: PaperOrder) -> None:
        self.orders.pop(order.id, None)
        if self._by_token.get(order.token_id) == order.id:
            del self._by_token[order.token_id]

    def open_value_usd(self, condition_id: str) -> float:
        return sum(
            o.remaining * o.price for o in self.orders.values()
            if o.condition_id == condition_id and o.status == "open"
        )

    def orders_for_market(self, condition_id: str) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.condition_id == condition_id]

    # fill simulation ---------------------------------------------------------

    def match_trade(self, tick: TradeTick) -> list[tuple[PaperOrder, float, float]]:
        """Trade printed strictly below our bid -> our level was traded through."""
        order = self.order_for_token(tick.token_id)
        if order is None or order.status != "open":
            return []
        if tick.price < order.price - EPS and tick.size > 0:
            qty = min(order.remaining, tick.size)
            return [(order, order.price, qty)]
        return []

    def match_book(self, book: OrderBook) -> list[tuple[PaperOrder, float, float]]:
        """Ask crossed our bid -> we would have been matched at our limit price."""
        order = self.order_for_token(book.token_id)
        if order is None or order.status != "open":
            return []
        if book.crossed:
            return []  # corrupted book: a bogus low ask must not create fake fills
        ba = book.best_ask
        if ba is None or ba > order.price + EPS:
            return []
        avail = book.ask_size_at_or_below(order.price)
        qty = min(order.remaining, avail)
        return [(order, order.price, qty)] if qty > 0 else []


class StrategyEngine:
    def __init__(self, cfg: Settings, hub: DataHub, sink: Sink, risk: RiskEngine, mode: str):
        self.cfg = cfg
        self.hub = hub
        self.sink = sink
        self.risk = risk
        self.mode = mode
        self.broker = PaperBroker()
        self.positions = PositionManager()
        self._skip_log: dict[tuple[str, str], float] = {}   # (token, reason) -> last persist ts
        self._arb_log: dict[tuple[str, str], float] = {}    # (condition_id, status) -> last persist ts
        self._cooldown: dict[str, float] = {}               # token_id -> last non-replace cancel ts
        self._market_cooldown: dict[str, float] = {}        # condition_id -> last new order ts
        self._zero_fill_count: dict[str, int] = {}         # condition_id -> consecutive zero-fill cancels
        self._zero_fill_ban: dict[str, float] = {}          # condition_id -> banned until ts
        self._kill_logged = False
        self.stats = {"orders": 0, "fills": 0, "settlements": 0, "arbs": 0}

    # ---- periodic evaluation ---------------------------------------------------

    def evaluate(self, now: float) -> None:
        if kill_switch.is_active(self.cfg.kill_switch_file):
            if not self._kill_logged:
                reason = kill_switch.read_reason(self.cfg.kill_switch_file)
                n_open = sum(1 for o in self.broker.orders.values() if o.status == "open")
                detail = f": {reason}" if reason else ""
                log.warning(
                    "KILL SWITCH active (%s%s): cancelling %d open orders, no new entries",
                    self.cfg.kill_switch_file, detail, n_open,
                )
                self._kill_logged = True
            self.cancel_all(now, "kill_switch")
            return
        if self._kill_logged:
            log.info("KILL SWITCH cleared (%s): resuming paper trading", self.cfg.kill_switch_file)
            self._kill_logged = False

        vol_cache: dict[str, float | None] = {}
        for market in list(self.hub.markets.values()):
            if market.closed or (market.end_ts - now) <= self.cfg.expiry_cancel_s:
                # too close to expiry (or already done): flatten open orders, stop quoting
                self.cancel_market_orders(market.condition_id, now, "market_expiry")
                continue
            tte = market.end_ts - now
            if self.mode == "paper":
                if self.cfg.paper_max_tte_hours > 0 and tte > self.cfg.paper_max_tte_s:
                    continue
                if self.cfg.paper_min_tte_hours > 0 and tte < self.cfg.paper_min_tte_s:
                    continue
            if now < self._zero_fill_ban.get(market.condition_id, 0.0):
                continue
            spot = self.hub.spot.get(market.asset)
            if spot is None or now - spot[0] > self.cfg.max_spot_age_s:
                continue
            if market.asset not in vol_cache:
                vol_cache[market.asset] = annualized_vol(
                    self.hub.close_series(market.asset),
                    lam=self.cfg.vol_lambda, min_obs=self.cfg.min_vol_candles,
                )
            sigma = vol_cache[market.asset]
            if sigma is None:
                continue  # no volatility estimate -> no trade

            fair_yes = fair_yes_probability(market, spot[1], sigma, now)
            yes_book = self.hub.get_book(market.yes_token_id)
            no_book = self.hub.get_book(market.no_token_id)

            self._scan_arb(market, yes_book, no_book, now)

            for sig in evaluate_market(market, fair_yes, yes_book, no_book, now, self.cfg):
                self._persist_signal(sig)
                if sig.action == "enter":
                    book = yes_book if sig.token_id == market.yes_token_id else no_book
                    self._manage_entry(market, sig, book, yes_book, no_book, now)
                else:
                    existing = self.broker.order_for_token(sig.token_id)
                    if existing is not None and self._should_cancel(sig):
                        reason = _CANCEL_REASON.get(sig.reason, "edge_dropped")
                        self._cancel(existing, now, reason)

    def _should_cancel(self, sig: Signal) -> bool:
        """Hysteresis: low_edge alone does not cancel until edge < exit_edge."""
        if sig.reason in ("expiry_cutoff", "crossed_book", "no_liquidity", "wide_spread"):
            return True
        if sig.reason == "low_edge":
            return sig.edge is None or sig.edge < self.cfg.exit_edge
        return False

    def _scan_arb(self, market: Market, yes_book, no_book, now: float) -> None:
        opp = arbitrage.scan(market, yes_book, no_book, now, self.cfg)
        if opp is None:
            return
        key = (market.condition_id, opp.status)
        if now - self._arb_log.get(key, 0.0) < self.cfg.arb_log_interval_s:
            return
        self.sink.arb(opp)
        self._arb_log[key] = now
        if opp.status == "ok":
            self.stats["arbs"] += 1
            log.info("ARB %s yes=%.3f no=%.3f edge=%.3f gap=%.2fs",
                     market.question[:60], opp.yes_ask, opp.no_ask, opp.edge,
                     opp.book_ts_gap)
        else:
            log.debug("ARB rejected (%s) %s yes=%.3f no=%.3f edge=%.3f gap=%.2fs",
                      opp.status, market.question[:60], opp.yes_ask, opp.no_ask,
                      opp.edge, opp.book_ts_gap)

    def _manage_entry(self, market: Market, sig: Signal, book: OrderBook | None,
                      yes_book: OrderBook | None, no_book: OrderBook | None, now: float) -> None:
        if book is None or sig.best_bid is None or sig.best_ask is None:
            return
        # no averaging down: never add to an existing position in the same token
        if self.positions.has_position(sig.token_id):
            self._persist_skip(sig, now, "existing_position")
            return
        price = maker_price(sig.fair, sig.best_bid, sig.best_ask, self.cfg)
        if price is None:
            return

        existing = self.broker.order_for_token(sig.token_id)
        if existing is not None:
            # keep the resting order unless fair moved enough AND price moves >= 1 tick
            fair_moved = abs(sig.fair - existing.fair) >= self.cfg.replace_edge_threshold
            price_changed = abs(existing.price - price) >= self.cfg.tick - EPS
            if not (fair_moved and price_changed):
                self._persist_skip(sig, now, "existing_open_order")
                return
            self._cancel(existing, now, "replace")
        else:
            # cooldown after a non-replace cancel on this token
            if now - self._cooldown.get(sig.token_id, 0.0) < self.cfg.order_cooldown_s:
                self._persist_skip(sig, now, "cooldown")
                return
            if now - self._market_cooldown.get(market.condition_id, 0.0) < self.cfg.market_cooldown_s:
                self._persist_skip(sig, now, "market_cooldown")
                return
            if now < self._zero_fill_ban.get(market.condition_id, 0.0):
                self._persist_skip(sig, now, "zero_fill_cooldown")
                return

        exposure = (self.positions.exposure_usd(market.condition_id)
                    + self.broker.open_value_usd(market.condition_id))
        add_usd = self.risk.trade_size_usd()
        if (market.end_ts - now) <= self.cfg.expiry_taper_s:
            add_usd *= 0.5  # ramp size down close to expiry
        ok, reason = self.risk.check_entry(now, exposure, add_usd)
        if not ok:
            self._persist_skip(sig, now, f"risk:{reason}")
            return

        size = round(add_usd / price, 2)
        if size <= 0:
            return
        order = PaperOrder(
            id=uuid.uuid4().hex[:12], created_ts=now,
            condition_id=market.condition_id, token_id=sig.token_id, label=sig.label,
            side="BUY", price=price, size=size, fair=sig.fair, edge=sig.edge or 0.0,
        )
        self.broker.place(order)
        self.sink.order_insert(order)
        self.stats["orders"] += 1
        self._market_cooldown[market.condition_id] = now
        fair_yes = sig.fair if sig.label == "YES" else 1.0 - sig.fair
        log.info(
            "PAPER ORDER selected_side=%s %.0f @ %.2f edge=%.4f | market_question=%r "
            "condition_id=%s yes_token_id=%s no_token_id=%s "
            "yes_best_bid=%s yes_best_ask=%s no_best_bid=%s no_best_ask=%s "
            "fair_yes=%.4f fair_no=%.4f",
            sig.label, size, price, sig.edge or 0.0, market.question,
            market.condition_id, market.yes_token_id, market.no_token_id,
            yes_book.best_bid if yes_book else None,
            yes_book.best_ask if yes_book else None,
            no_book.best_bid if no_book else None,
            no_book.best_ask if no_book else None,
            fair_yes, 1.0 - fair_yes,
        )

    def mark_prices(self) -> dict[str, float]:
        """Latest mid per token for unrealized PnL."""
        marks: dict[str, float] = {}
        for token_id in self.positions.positions:
            book = self.hub.get_book(token_id)
            if book is None or book.crossed:
                continue
            mid = book.mid
            if mid is not None:
                marks[token_id] = mid
        return marks

    def unrealized_summary(self):
        return self.positions.unrealized(self.mark_prices())

    # ---- event hooks -------------------------------------------------------------

    def on_trade(self, tick: TradeTick, now: float) -> None:
        self._apply_fills(self.broker.match_trade(tick), now)

    def on_book(self, book: OrderBook, now: float) -> None:
        self._apply_fills(self.broker.match_book(book), now)

    def _apply_fills(self, fills: list[tuple[PaperOrder, float, float]], now: float) -> None:
        for order, price, qty in fills:
            fair_at_fill, spot_at_fill, tte_s = self._fill_context(order, now)
            order.filled = min(order.size, order.filled + qty)
            self.sink.fill(order, now, price, qty, fair_at_fill, spot_at_fill, tte_s)
            self.positions.on_fill(order.condition_id, order.token_id, order.label, price, qty)
            self._zero_fill_count[order.condition_id] = 0
            self._zero_fill_ban.pop(order.condition_id, None)
            self.stats["fills"] += 1
            if order.remaining <= EPS:
                order.status = "filled"
                self.sink.order_update(order, closed_ts=now)
                self.broker.remove(order)
            else:
                self.sink.order_update(order)
            log.info("PAPER FILL %s %.0f @ %.2f (%s)", order.label, qty, price, order.id)

    def _fill_context(self, order: PaperOrder, now: float) -> tuple[float | None, float | None, float | None]:
        """Fair value, spot, and time-to-expiry at fill time (for calibration reports)."""
        market = self.hub.markets.get(order.condition_id)
        if market is None:
            return None, None, None
        tte_s = market.end_ts - now
        spot_row = self.hub.spot.get(market.asset)
        if spot_row is None or now - spot_row[0] > self.cfg.max_spot_age_s:
            return None, None, tte_s
        spot = spot_row[1]
        sigma = annualized_vol(
            self.hub.close_series(market.asset),
            lam=self.cfg.vol_lambda, min_obs=self.cfg.min_vol_candles,
        )
        if sigma is None:
            return None, spot, tte_s
        fair_yes = fair_yes_probability(market, spot, sigma, now)
        fair = fair_yes if order.label == "YES" else 1.0 - fair_yes
        return fair, spot, tte_s

    # ---- lifecycle -----------------------------------------------------------------

    def _cancel(self, order: PaperOrder, now: float, reason: str) -> None:
        order.status = "cancelled"
        order.cancel_reason = reason
        self.sink.order_update(order, closed_ts=now)
        self.broker.remove(order)
        if reason != "replace":
            self._cooldown[order.token_id] = now
        if (reason not in ZERO_FILL_EXCLUDE_CANCEL
                and order.filled <= EPS):
            cid = order.condition_id
            streak = self._zero_fill_count.get(cid, 0) + 1
            self._zero_fill_count[cid] = streak
            if streak >= self.cfg.zero_fill_cancel_limit:
                until = now + self.cfg.zero_fill_cooldown_s
                self._zero_fill_ban[cid] = until
                log.warning(
                    "zero-fill cooldown: %s after %d cancels (banned %.0f min)",
                    cid[:12], streak, self.cfg.zero_fill_cooldown_s / 60.0,
                )

    def cancel_all(self, now: float, reason: str) -> None:
        for order in list(self.broker.orders.values()):
            if order.status == "open":
                self._cancel(order, now, reason)

    def cancel_market_orders(self, condition_id: str, now: float, reason: str) -> None:
        for order in self.broker.orders_for_market(condition_id):
            if order.status == "open":
                self._cancel(order, now, reason)

    def settle_market(self, market: Market, outcome_yes: float, now: float) -> None:
        for order in self.broker.orders_for_market(market.condition_id):
            self._cancel(order, now, "market_closed")
        for st in self.positions.settle_market(market, outcome_yes):
            self.sink.settlement(now, st.condition_id, st.token_id, st.label,
                                 st.size, st.avg_price, st.payout, st.pnl)
            self.risk.on_settlement(st.pnl, now)
            self.stats["settlements"] += 1
            log.info("SETTLED %s %s %.0f @ %.2f -> payout %.2f pnl %+.2f USD",
                     market.asset, st.label, st.size, st.avg_price, st.payout, st.pnl)
        market.closed = True
        market.outcome = outcome_yes
        self.sink.market(market, now)

    def _persist_skip(self, sig: Signal, now: float, reason: str) -> None:
        self._persist_signal(Signal(
            ts=now, condition_id=sig.condition_id, token_id=sig.token_id, label=sig.label,
            fair=sig.fair, best_bid=sig.best_bid, best_ask=sig.best_ask,
            spread=sig.spread, edge=sig.edge, action="skip", reason=reason,
        ))

    def _persist_signal(self, sig: Signal) -> None:
        """Always persist 'enter'; throttle repeated identical skips to 1 per 5 min."""
        if sig.action == "enter":
            self.sink.signal(sig)
            return
        key = (sig.token_id, sig.reason)
        if sig.ts - self._skip_log.get(key, 0.0) >= 300.0:
            self.sink.signal(sig)
            self._skip_log[key] = sig.ts
