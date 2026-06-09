"""CLI entrypoint.

  python -m app.main collect    # Phase 1: record markets / orderbooks / crypto prices
  python -m app.main backtest   # Phase 2: replay recorded data through the strategy
  python -m app.main paper      # Phase 3: live paper trading (no real orders, ever)
  python -m app.main report     # PnL / win-rate / edge / arb report
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from .backtest.backtester import Backtester
from .backtest.metrics import print_report
from .config import Settings, load_settings
from .data import clob_rest, gamma_client
from .data.clob_websocket import MarketWebSocket
from .data.crypto_feed import CryptoFeed
from .data.recorder import DataHub, Recorder
from .paper.paper_engine import StrategyEngine
from .risk.risk_engine import RiskEngine
from .storage.db import Sink, connect_async, connect_sync
from .storage.models import Market, OrderBook, TradeTick

log = logging.getLogger("app")


def _setup_logging() -> None:
    try:
        from rich.logging import RichHandler
        handler: logging.Handler = RichHandler(rich_tracebacks=False, show_path=False)
        fmt = "%(message)s"
    except ImportError:
        handler = logging.StreamHandler()
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[handler])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class LiveRunner:
    """Shared runtime for `collect` (record only) and `paper` (record + trade)."""

    def __init__(self, cfg: Settings, paper: bool):
        self.cfg = cfg
        self.paper = paper
        self.hub = DataHub()
        self.sink = Sink(mode="paper")
        self.recorder = Recorder(cfg, self.hub, self.sink)
        self.ws = MarketWebSocket(self._on_ws_event)
        self.feed = CryptoFeed(cfg.assets, self.recorder.on_price, self.recorder.on_candle)
        self.engine: StrategyEngine | None = None
        if paper:
            risk = RiskEngine(cfg)
            self.engine = StrategyEngine(cfg, self.hub, self.sink, risk, mode="paper")
        self._http: httpx.AsyncClient | None = None
        self._db = None

    # ---- websocket event routing (sync callback) -------------------------------

    def _on_ws_event(self, ev: dict) -> None:
        obj = self.recorder.on_ws_event(ev)
        if self.engine is None or obj is None:
            return
        now = time.time()
        if isinstance(obj, TradeTick):
            self.engine.on_trade(obj, now)
        elif isinstance(obj, OrderBook):
            self.engine.on_book(obj, now)

    # ---- market discovery / resolution -------------------------------------------

    async def _refresh_markets(self) -> None:
        now = time.time()
        markets = await gamma_client.fetch_active_crypto_markets(
            self._http, now, self.cfg.market_window_days)
        fresh_ids = {m.condition_id for m in markets}
        for m in markets:
            known = self.hub.markets.get(m.condition_id)
            if known is None:
                log.info("tracking market: %s (ends %s)", m.question[:70],
                         datetime.fromtimestamp(m.end_ts, tz=timezone.utc).strftime("%m-%d %H:%M"))
                await self._seed_books(m)
            self.recorder.on_market(m, now)

        # poll resolutions for tracked markets that expired or vanished from the listing
        stale = [
            cid for cid, m in self.hub.markets.items()
            if not m.closed and (m.end_ts < now or cid not in fresh_ids)
        ]
        if stale:
            raw = await gamma_client.fetch_markets_by_condition(self._http, stale)
            for cid, rm in raw.items():
                m = self.hub.markets.get(cid)
                if m is None or not rm.get("closed"):
                    continue
                outcome = gamma_client._parse_outcome(rm)
                if outcome is None:
                    continue
                log.info("market resolved: %s -> YES=%.2f", m.question[:70], outcome)
                if self.engine is not None:
                    self.engine.settle_market(m, outcome, now)
                else:
                    m.closed = True
                    m.outcome = outcome
                    self.sink.market(m, now)
                self.hub.remove_market(cid)

        self.ws.set_assets(self.hub.tracked_tokens())

    async def _seed_books(self, m: Market) -> None:
        """REST snapshot so the book exists before the first WS event arrives."""
        for token in m.tokens:
            book = await clob_rest.get_order_book(self._http, token)
            if book is not None:
                self.hub.set_book(book)
                self.sink.book_snapshot(book)

    # ---- periodic tasks ----------------------------------------------------------

    async def _market_loop(self) -> None:
        while True:
            try:
                await self._refresh_markets()
            except Exception:
                log.exception("market refresh failed")
            await asyncio.sleep(self.cfg.market_refresh_s)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.flush_interval_s)
            try:
                await self.sink.flush_async(self._db)
            except Exception:
                log.exception("db flush failed")

    async def _eval_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.eval_interval_s)
            try:
                self.engine.evaluate(time.time())
            except Exception:
                log.exception("strategy evaluation failed")

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            c = self.recorder.counters
            spots = ", ".join(
                f"{sym} {price:,.0f}" for sym, (_, price) in sorted(self.hub.spot.items()))
            line = (f"markets={len(self.hub.markets)} books={c['books']} "
                    f"ticks={c['ticks']} trades={c['trades']} | {spots or 'no spot yet'}")
            if self.engine is not None:
                s = self.engine.stats
                now = time.time()
                line += (f" | orders={s['orders']} fills={s['fills']} "
                         f"settled={s['settlements']} todayPnL=${self.engine.risk.today_pnl(now):+.2f}")
                if self.engine.risk.halted_reason:
                    line += f" [HALTED: {self.engine.risk.halted_reason}]"
            log.info(line)

    # ---- entry ----------------------------------------------------------------------

    async def run(self) -> None:
        mode = "PAPER TRADING (no real orders)" if self.paper else "DATA COLLECTION"
        log.info("starting %s | db=%s | bankroll=$%.0f min_edge=%.1f%%",
                 mode, _redact_db_url(self.cfg.database_url),
                 self.cfg.bankroll, self.cfg.min_edge * 100)
        self._db = await connect_async(self.cfg.database_url)
        self._http = httpx.AsyncClient()
        try:
            if self.engine is not None:
                await self._bootstrap_risk()
            await self.feed.bootstrap(self._http)
            await self._refresh_markets()
            if not self.hub.markets:
                log.warning("no BTC/ETH terminal-price markets found right now; "
                            "the bot keeps polling every %.0fs", self.cfg.market_refresh_s)
            tasks = [
                asyncio.create_task(self.ws.run(), name="clob-ws"),
                asyncio.create_task(self.feed.run(), name="crypto-feed"),
                asyncio.create_task(self._market_loop(), name="markets"),
                asyncio.create_task(self._flush_loop(), name="flush"),
                asyncio.create_task(self._status_loop(), name="status"),
            ]
            if self.engine is not None:
                tasks.append(asyncio.create_task(self._eval_loop(), name="eval"))
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception():
                    raise t.exception()
        finally:
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    t.cancel()
            await self.sink.flush_async(self._db)
            await self._db.close()
            await self._http.aclose()

    async def _bootstrap_risk(self) -> None:
        """Restore daily/weekly loss + consecutive-loss state from prior paper runs."""
        cur = await self._db.execute(
            "SELECT ts, pnl FROM paper_settlements WHERE mode='paper' AND ts >= %s ORDER BY ts",
            (time.time() - 14 * 86400,))
        rows = await cur.fetchall()
        self.engine.risk.bootstrap([(r["ts"], r["pnl"]) for r in rows])
        if rows:
            log.info("restored risk state from %d prior settlements "
                     "(consecutive losses: %d)", len(rows), self.engine.risk.consecutive_losses)


def _redact_db_url(url: str) -> str:
    """Hide the password when logging the connection string."""
    import re
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def _parse_when(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s)  # raw epoch
    except ValueError:
        pass
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="polymarket-bot",
                                     description="Polymarket BTC/ETH value bot (paper only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="record markets, orderbooks and crypto prices")
    sub.add_parser("paper", help="live paper trading (records data too)")
    bt = sub.add_parser("backtest", help="replay recorded data through the strategy")
    bt.add_argument("--from", dest="t0", default=None,
                    help="start (ISO datetime or epoch), default: first recorded snapshot")
    bt.add_argument("--to", dest="t1", default=None,
                    help="end (ISO datetime or epoch), default: last recorded snapshot")
    sub.add_parser("report", help="print PnL / win-rate / edge / arb report")
    args = parser.parse_args()

    cfg = load_settings()
    if args.command in ("collect", "paper"):
        runner = LiveRunner(cfg, paper=args.command == "paper")
        try:
            asyncio.run(runner.run())
        except KeyboardInterrupt:
            log.info("stopped by user")
    elif args.command == "backtest":
        connect_sync(cfg.database_url).close()  # ensure schema exists
        result = Backtester(cfg, _parse_when(args.t0), _parse_when(args.t1)).run()
        log.info("backtest done: %s", result)
        print_report(cfg)
    elif args.command == "report":
        print_report(cfg)


if __name__ == "__main__":
    main()
