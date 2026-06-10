"""Shared dataclasses used by data collection, strategy, paper trading and backtest."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    asset: str            # "BTC" | "ETH"
    strike: float
    direction: str        # "above" | "below"  (terminal price markets only)
    end_ts: float         # epoch seconds
    yes_token_id: str
    no_token_id: str
    active: bool = True
    closed: bool = False
    outcome: float | None = None   # YES payout per share: 1.0 / 0.0 (sometimes fractional)

    @property
    def tokens(self) -> tuple[str, str]:
        return self.yes_token_id, self.no_token_id


@dataclass
class Candle:
    symbol: str           # "BTC" | "ETH"
    open_time: float      # epoch seconds (start of minute)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class TradeTick:
    token_id: str
    ts: float
    price: float
    size: float
    side: str             # aggressor side reported by the feed ("BUY"/"SELL"), may be ""


@dataclass
class OrderBook:
    token_id: str
    ts: float
    bids: list[tuple[float, float]]   # (price, size) sorted desc by price
    asks: list[tuple[float, float]]   # (price, size) sorted asc by price

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba - bb

    @property
    def crossed(self) -> bool:
        """bid >= ask is an impossible resting state on a real CLOB; it means a
        corrupted/merged book (e.g. complement-token levels on the wrong side).
        Any price derived from a crossed book must not be trusted."""
        bb, ba = self.best_bid, self.best_ask
        return bb is not None and ba is not None and bb >= ba - 1e-9

    def ask_size_at_or_below(self, price: float) -> float:
        return sum(s for p, s in self.asks if p <= price + 1e-9)

    def bids_json(self) -> str:
        return json.dumps(self.bids)

    def asks_json(self) -> str:
        return json.dumps(self.asks)

    @staticmethod
    def from_json(token_id: str, ts: float, bids_json: str, asks_json: str) -> "OrderBook":
        bids = [(float(p), float(s)) for p, s in json.loads(bids_json)]
        asks = [(float(p), float(s)) for p, s in json.loads(asks_json)]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return OrderBook(token_id=token_id, ts=ts, bids=bids, asks=asks)


class OrderBookState:
    """Incrementally maintained orderbook for one token (WS snapshots + deltas)."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        self.ts: float = 0.0
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}

    def apply_snapshot(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], ts: float) -> None:
        self._bids = {p: s for p, s in bids if s > 0}
        self._asks = {p: s for p, s in asks if s > 0}
        self.ts = ts

    def apply_change(self, side: str, price: float, size: float, ts: float) -> None:
        levels = self._bids if side.upper() == "BUY" else self._asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.ts = ts

    @property
    def has_data(self) -> bool:
        return bool(self._bids or self._asks)

    def to_book(self, depth: int = 20) -> OrderBook:
        bids = sorted(self._bids.items(), key=lambda x: -x[0])[:depth]
        asks = sorted(self._asks.items(), key=lambda x: x[0])[:depth]
        return OrderBook(token_id=self.token_id, ts=self.ts, bids=bids, asks=asks)


@dataclass
class Signal:
    ts: float
    condition_id: str
    token_id: str
    label: str            # "YES" | "NO"
    fair: float
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    edge: float | None
    action: str           # "enter" | "skip"
    reason: str           # skip reason or "" for enter


@dataclass
class PaperOrder:
    id: str
    created_ts: float
    condition_id: str
    token_id: str
    label: str            # "YES" | "NO"
    side: str             # always "BUY" in this MVP (maker buy of YES or NO token)
    price: float
    size: float           # shares
    filled: float = 0.0
    status: str = "open"  # open | filled | cancelled
    fair: float = 0.0
    edge: float = 0.0
    cancel_reason: str | None = None  # replace | edge_dropped | stale_book | market_expiry | market_closed | kill_switch | manual_shutdown | backtest_end

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)


@dataclass
class Position:
    condition_id: str
    token_id: str
    label: str
    size: float = 0.0
    avg_price: float = 0.0

    @property
    def cost_usd(self) -> float:
        return self.size * self.avg_price


@dataclass
class ArbOpportunity:
    ts: float
    condition_id: str
    yes_token_id: str
    no_token_id: str
    yes_ask: float
    no_ask: float
    total: float
    edge: float
    yes_size: float
    no_size: float
    yes_book_ts: float
    no_book_ts: float
    status: str = "ok"   # 'ok' or rejection reason (stale_book, book_ts_gap, ...)

    @property
    def book_ts_gap(self) -> float:
        return abs(self.yes_book_ts - self.no_book_ts)
