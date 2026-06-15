"""Backtester: replays recorded orderbook/trade/spot events through the exact
same StrategyEngine used in live paper trading.

Fidelity notes:
  - books come from recorded snapshots (includes spread + depth),
  - fills require trades printing through our bid or the book crossing it
    (slippage-free maker fills are impossible by construction),
  - partial fills are capped by printed trade size / visible ask size,
  - settlement uses the recorded resolution when available, otherwise it is
    derived from the recorded spot price at expiry vs the strike.
"""
from __future__ import annotations

import heapq
import logging
import time

from ..config import Settings
from ..data.recorder import DataHub
from ..paper.paper_engine import StrategyEngine
from ..paper.settlement import resolve_outcome
from ..risk.risk_engine import RiskEngine
from ..storage.db import Sink, connect_sync
from ..storage.models import Candle, Market, OrderBook, TradeTick

log = logging.getLogger(__name__)

MODE = "backtest"


def _load_markets(conn, t0: float, t1: float) -> list[Market]:
    rows = conn.execute(
        "SELECT * FROM markets WHERE end_ts >= %s AND first_seen <= %s", (t0, t1)
    ).fetchall()
    out = []
    for r in rows:
        out.append(Market(
            condition_id=r["condition_id"], question=r["question"] or "", slug=r["slug"] or "",
            asset=r["asset"], strike=r["strike"], direction=r["direction"],
            end_ts=r["end_ts"], yes_token_id=r["yes_token_id"], no_token_id=r["no_token_id"],
            active=bool(r["active"]), closed=False,  # replay starts with the market open
            outcome=r["outcome"],
        ))
    return out


def _event_streams(conn, t0: float, t1: float):
    """Yield (ts, kind, payload) from each table, merged in time order.

    Server-side (named) cursors stream the rows instead of loading every
    table into memory at once.
    """
    def stream(name: str, sql: str, params: tuple):
        with conn.cursor(name=name) as cur:
            cur.itersize = 5000
            cur.execute(sql, params)
            yield from cur

    def books():
        for r in stream("bt_books",
                        "SELECT ts, token_id, bids, asks FROM book_snapshots "
                        "WHERE ts BETWEEN %s AND %s ORDER BY ts", (t0, t1)):
            yield (r["ts"], 1, ("book", r))

    def trades():
        for r in stream("bt_trades",
                        "SELECT ts, token_id, price, size, side FROM price_ticks "
                        "WHERE event_type='last_trade_price' AND ts BETWEEN %s AND %s "
                        "ORDER BY ts", (t0, t1)):
            yield (r["ts"], 2, ("trade", r))

    def spots():
        for r in stream("bt_spots",
                        "SELECT ts, symbol, price FROM crypto_prices "
                        "WHERE ts BETWEEN %s AND %s ORDER BY ts", (t0, t1)):
            yield (r["ts"], 0, ("spot", r))

    def candles():
        for r in stream("bt_candles",
                        "SELECT symbol, open_time, open, high, low, close, volume "
                        "FROM crypto_candles WHERE open_time BETWEEN %s AND %s "
                        "ORDER BY open_time", (t0 - 60, t1)):
            # candle becomes known when it closes
            yield (r["open_time"] + 60.0, 0, ("candle", r))

    return heapq.merge(books(), trades(), spots(), candles(), key=lambda e: (e[0], e[1]))


class Backtester:
    def __init__(self, cfg: Settings, t0: float | None = None, t1: float | None = None):
        self.cfg = cfg
        self.t0 = t0
        self.t1 = t1

    def run(self) -> dict:
        # separate read/write connections: the writer commits mid-replay, which
        # would otherwise close the reader's server-side cursors
        conn = connect_sync(self.cfg.database_url)
        wconn = connect_sync(self.cfg.database_url)
        try:
            return self._run(conn, wconn)
        finally:
            conn.close()
            wconn.close()

    def _run(self, conn, wconn) -> dict:
        # determine replay window from recorded data if not given
        row = conn.execute("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM book_snapshots").fetchone()
        if row["lo"] is None:
            raise SystemExit(
                "No recorded orderbook data. Run `python -m app.main collect` first.")
        t0 = self.t0 if self.t0 is not None else row["lo"]
        t1 = self.t1 if self.t1 is not None else row["hi"]
        if t1 <= t0:
            raise SystemExit(f"Empty backtest window: {t0} .. {t1}")

        # wipe previous backtest output
        for table in ("signals", "paper_orders", "paper_fills", "paper_settlements", "paper_exits"):
            wconn.execute(f"DELETE FROM {table} WHERE mode=%s", (MODE,))
        wconn.commit()

        hub = DataHub()
        sink = Sink(mode=MODE)
        risk = RiskEngine(self.cfg)
        engine = StrategyEngine(self.cfg, hub, sink, risk, mode=MODE)

        markets = _load_markets(conn, t0, t1)
        for m in markets:
            hub.update_market(m)
        pending = sorted(markets, key=lambda m: m.end_ts)  # settlement queue
        log.info("backtest window %.0fs, %d markets, replaying events...", t1 - t0, len(pending))

        # seed candles known before the window so vol estimates exist from the start
        cur = conn.execute(
            "SELECT symbol, open_time, open, high, low, close, volume FROM crypto_candles "
            "WHERE open_time < %s ORDER BY open_time DESC LIMIT %s",
            (t0, 1500 * max(1, len(self.cfg.assets))))
        for r in sorted(cur.fetchall(), key=lambda r: r["open_time"]):
            hub.add_candle(Candle(r["symbol"], r["open_time"], r["open"], r["high"],
                                  r["low"], r["close"], r["volume"]))

        wall = time.time()
        n_events = 0
        next_eval = t0
        last_spot: dict[str, float] = {}

        for ts, _, (kind, r) in _event_streams(conn, t0, t1):
            n_events += 1
            now = ts

            # settle expired markets before processing the event
            while pending and pending[0].end_ts <= now:
                m = pending.pop(0)
                self._settle(engine, hub, m, last_spot, now)

            if kind == "spot":
                hub.set_spot(r["symbol"], ts, r["price"])
                last_spot[r["symbol"]] = r["price"]
            elif kind == "candle":
                hub.add_candle(Candle(r["symbol"], r["open_time"], r["open"], r["high"],
                                      r["low"], r["close"], r["volume"]))
            elif kind == "book":
                book = OrderBook.from_json(r["token_id"], ts, r["bids"], r["asks"])
                hub.set_book(book)
                engine.on_book(book, now)
            elif kind == "trade":
                tick = TradeTick(token_id=r["token_id"], ts=ts, price=r["price"],
                                 size=r["size"], side=r["side"] or "")
                engine.on_trade(tick, now)

            if now >= next_eval:
                engine.evaluate(now)
                next_eval = now + self.cfg.eval_interval_s

            if len(sink) >= 2000:
                sink.flush_sync(wconn)

        # settle whatever expired within (or right at the end of) the window
        for m in list(pending):
            if m.end_ts <= t1:
                self._settle(engine, hub, m, last_spot, m.end_ts)
        engine.cancel_all(t1, "backtest_end")
        sink.flush_sync(wconn)

        log.info("replayed %d events in %.1fs (orders=%d fills=%d settlements=%d)",
                 n_events, time.time() - wall, engine.stats["orders"],
                 engine.stats["fills"], engine.stats["settlements"])
        return {"t0": t0, "t1": t1, "events": n_events, **engine.stats}

    def _settle(self, engine: StrategyEngine, hub: DataHub, m: Market,
                last_spot: dict[str, float], now: float) -> None:
        spot = last_spot.get(m.asset)
        outcome = resolve_outcome(m, spot)
        if outcome is None:
            engine.cancel_market_orders(m.condition_id, now, "market_closed")
            hub.remove_market(m.condition_id)
            return
        engine.settle_market(m, outcome, now)
        hub.remove_market(m.condition_id)
