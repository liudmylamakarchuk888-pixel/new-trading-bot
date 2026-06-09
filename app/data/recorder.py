"""In-memory market data state (DataHub) + persistence glue (Recorder).

DataHub is feed-agnostic: live WS feeds and the backtest replayer both push
updates into it, and the strategy engine reads from it. Recorder routes the
same updates into the SQLite Sink (Phase 1 data collection).
"""
from __future__ import annotations

import logging
from collections import deque

from ..config import Settings
from ..storage.db import Sink
from ..storage.models import Candle, Market, OrderBook, OrderBookState, TradeTick

log = logging.getLogger(__name__)


class DataHub:
    def __init__(self, max_candles: int = 3000):
        self.markets: dict[str, Market] = {}
        self.token_market: dict[str, str] = {}
        self.books: dict[str, OrderBookState] = {}
        self.spot: dict[str, tuple[float, float]] = {}          # symbol -> (ts, price)
        self.closes: dict[str, deque[tuple[float, float]]] = {} # symbol -> (open_time, close)
        self._max_candles = max_candles

    # markets ---------------------------------------------------------------

    def update_market(self, m: Market) -> None:
        self.markets[m.condition_id] = m
        self.token_market[m.yes_token_id] = m.condition_id
        self.token_market[m.no_token_id] = m.condition_id

    def remove_market(self, condition_id: str) -> None:
        m = self.markets.pop(condition_id, None)
        if m:
            for tok in m.tokens:
                self.token_market.pop(tok, None)
                self.books.pop(tok, None)

    def tracked_tokens(self) -> set[str]:
        out: set[str] = set()
        for m in self.markets.values():
            if not m.closed:
                out.update(m.tokens)
        return out

    # books -------------------------------------------------------------------

    def book_state(self, token_id: str) -> OrderBookState:
        st = self.books.get(token_id)
        if st is None:
            st = OrderBookState(token_id)
            self.books[token_id] = st
        return st

    def get_book(self, token_id: str) -> OrderBook | None:
        st = self.books.get(token_id)
        if st is None or not st.has_data:
            return None
        return st.to_book()

    def set_book(self, book: OrderBook) -> None:
        self.book_state(book.token_id).apply_snapshot(book.bids, book.asks, book.ts)

    # crypto -------------------------------------------------------------------

    def set_spot(self, symbol: str, ts: float, price: float) -> None:
        self.spot[symbol] = (ts, price)

    def add_candle(self, c: Candle) -> None:
        dq = self.closes.get(c.symbol)
        if dq is None:
            dq = deque(maxlen=self._max_candles)
            self.closes[c.symbol] = dq
        if dq and dq[-1][0] == c.open_time:
            dq[-1] = (c.open_time, c.close)
        else:
            dq.append((c.open_time, c.close))

    def close_series(self, symbol: str) -> list[float]:
        return [c for _, c in self.closes.get(symbol, ())]


class Recorder:
    """Applies feed events to the DataHub and enqueues them into the Sink."""

    def __init__(self, cfg: Settings, hub: DataHub, sink: Sink):
        self.cfg = cfg
        self.hub = hub
        self.sink = sink
        self._last_snapshot_ts: dict[str, float] = {}
        self._last_spot_persist: dict[str, float] = {}
        self.counters = {"books": 0, "ticks": 0, "trades": 0, "crypto": 0}

    # Polymarket WS events ----------------------------------------------------

    def on_ws_event(self, ev: dict) -> TradeTick | OrderBook | None:
        """Returns the normalized object so the caller can forward it to the engine."""
        etype = ev["type"]
        token = ev.get("token_id", "")
        if not token or token not in self.hub.token_market:
            return None
        if etype == "book":
            st = self.hub.book_state(token)
            st.apply_snapshot(ev["bids"], ev["asks"], ev["ts"])
            book = st.to_book()
            self.sink.book_snapshot(book)
            self._last_snapshot_ts[token] = ev["ts"]
            self.counters["books"] += 1
            return book
        if etype == "price_change":
            st = self.hub.book_state(token)
            st.apply_change(ev["side"], ev["price"], ev["size"], ev["ts"])
            self.sink.tick(ev["ts"], token, "price_change", ev["price"], ev["size"], ev["side"])
            self.counters["ticks"] += 1
            last = self._last_snapshot_ts.get(token, 0.0)
            if ev["ts"] - last >= self.cfg.book_snapshot_interval_s and st.has_data:
                book = st.to_book()
                self.sink.book_snapshot(book)
                self._last_snapshot_ts[token] = ev["ts"]
                return book
            return None
        if etype == "trade":
            tick = TradeTick(token_id=token, ts=ev["ts"], price=ev["price"],
                             size=ev["size"], side=ev["side"])
            self.sink.tick(tick.ts, token, "last_trade_price", tick.price, tick.size, tick.side)
            self.counters["trades"] += 1
            return tick
        return None

    # crypto feed callbacks -----------------------------------------------------

    def on_price(self, symbol: str, ts: float, price: float) -> None:
        self.hub.set_spot(symbol, ts, price)
        # persist at most one row per second per symbol
        if ts - self._last_spot_persist.get(symbol, 0.0) >= 1.0:
            self.sink.crypto_price(ts, symbol, price)
            self._last_spot_persist[symbol] = ts
            self.counters["crypto"] += 1

    def on_candle(self, c: Candle) -> None:
        self.hub.add_candle(c)
        self.sink.candle(c)

    # market discovery -----------------------------------------------------------

    def on_market(self, m: Market, now: float) -> None:
        self.hub.update_market(m)
        self.sink.market(m, now)
