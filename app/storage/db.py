"""PostgreSQL storage: schema, connections (sync + async), and a buffered write Sink.

The Sink lets the synchronous strategy/paper core enqueue rows without caring
whether the process is the live asyncio app (flushed via an async connection)
or the offline backtester (flushed via a sync connection).

The database is selected with BOT_DATABASE_URL (.env), e.g.
    postgresql://postgres:postgres@localhost:5432/trading_bot         (local dev)
    postgresql://postgres:***@141.136.44.1:54322/trading_bot          (VPS)
"""
from __future__ import annotations

from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from .models import ArbOpportunity, Candle, Market, OrderBook, PaperOrder, Signal, TradeTick

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
    condition_id TEXT PRIMARY KEY,
    question TEXT, slug TEXT, asset TEXT,
    strike DOUBLE PRECISION, direction TEXT, end_ts DOUBLE PRECISION,
    yes_token_id TEXT, no_token_id TEXT,
    active INTEGER DEFAULT 1, closed INTEGER DEFAULT 0, outcome DOUBLE PRECISION,
    first_seen DOUBLE PRECISION, last_seen DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS book_snapshots(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts DOUBLE PRECISION, token_id TEXT,
    bids TEXT, asks TEXT,
    best_bid DOUBLE PRECISION, best_ask DOUBLE PRECISION,
    mid DOUBLE PRECISION, spread DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_book_token_ts ON book_snapshots(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_book_ts ON book_snapshots(ts);
CREATE TABLE IF NOT EXISTS price_ticks(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts DOUBLE PRECISION, token_id TEXT, event_type TEXT,
    price DOUBLE PRECISION, size DOUBLE PRECISION, side TEXT
);
CREATE INDEX IF NOT EXISTS idx_tick_ts ON price_ticks(ts);
CREATE INDEX IF NOT EXISTS idx_tick_token_ts ON price_ticks(token_id, ts);
CREATE TABLE IF NOT EXISTS crypto_prices(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts DOUBLE PRECISION, symbol TEXT, price DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_crypto_ts ON crypto_prices(ts);
CREATE TABLE IF NOT EXISTS crypto_candles(
    symbol TEXT, open_time DOUBLE PRECISION,
    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
    close DOUBLE PRECISION, volume DOUBLE PRECISION,
    PRIMARY KEY(symbol, open_time)
);
CREATE TABLE IF NOT EXISTS signals(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts DOUBLE PRECISION, condition_id TEXT, token_id TEXT, label TEXT,
    fair DOUBLE PRECISION, best_bid DOUBLE PRECISION, best_ask DOUBLE PRECISION,
    spread DOUBLE PRECISION, edge DOUBLE PRECISION,
    action TEXT, reason TEXT, mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE TABLE IF NOT EXISTS paper_orders(
    id TEXT PRIMARY KEY,
    created_ts DOUBLE PRECISION, closed_ts DOUBLE PRECISION,
    condition_id TEXT, token_id TEXT, side TEXT, label TEXT,
    price DOUBLE PRECISION, size DOUBLE PRECISION, filled DOUBLE PRECISION DEFAULT 0,
    status TEXT, fair DOUBLE PRECISION, edge DOUBLE PRECISION, mode TEXT
);
CREATE TABLE IF NOT EXISTS paper_fills(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id TEXT, ts DOUBLE PRECISION,
    condition_id TEXT, token_id TEXT,
    price DOUBLE PRECISION, size DOUBLE PRECISION, mode TEXT
);
CREATE TABLE IF NOT EXISTS paper_settlements(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts DOUBLE PRECISION, condition_id TEXT, token_id TEXT, label TEXT,
    size DOUBLE PRECISION, avg_price DOUBLE PRECISION,
    payout DOUBLE PRECISION, pnl DOUBLE PRECISION, mode TEXT
);
CREATE TABLE IF NOT EXISTS arb_opportunities(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts DOUBLE PRECISION, condition_id TEXT,
    yes_ask DOUBLE PRECISION, no_ask DOUBLE PRECISION,
    total DOUBLE PRECISION, edge DOUBLE PRECISION,
    yes_size DOUBLE PRECISION, no_size DOUBLE PRECISION
);
"""


def ensure_database(database_url: str) -> None:
    """Create the target database on first run if it does not exist yet."""
    try:
        psycopg.connect(database_url).close()
        return
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            raise
    dbname = urlsplit(database_url).path.lstrip("/")
    admin_url = database_url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')


def connect_sync(database_url: str) -> psycopg.Connection:
    ensure_database(database_url)
    conn = psycopg.connect(database_url, row_factory=dict_row)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


async def connect_async(database_url: str) -> psycopg.AsyncConnection:
    ensure_database(database_url)
    conn = await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row)
    await conn.execute(SCHEMA)
    await conn.commit()
    return conn


class Sink:
    """Buffer of (sql, params) rows, flushed periodically by the owning runtime."""

    def __init__(self, mode: str = "paper"):
        self.mode = mode
        self._rows: list[tuple[str, tuple]] = []

    def add(self, sql: str, params: tuple) -> None:
        self._rows.append((sql, params))

    def drain(self) -> list[tuple[str, tuple]]:
        rows, self._rows = self._rows, []
        return rows

    def __len__(self) -> int:
        return len(self._rows)

    # ---- helpers -----------------------------------------------------------

    def market(self, m: Market, now: float) -> None:
        self.add(
            """INSERT INTO markets(condition_id, question, slug, asset, strike, direction,
                   end_ts, yes_token_id, no_token_id, active, closed, outcome, first_seen, last_seen)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(condition_id) DO UPDATE SET
                   active=excluded.active, closed=excluded.closed, outcome=excluded.outcome,
                   end_ts=excluded.end_ts, last_seen=excluded.last_seen""",
            (m.condition_id, m.question, m.slug, m.asset, m.strike, m.direction,
             m.end_ts, m.yes_token_id, m.no_token_id, int(m.active), int(m.closed),
             m.outcome, now, now),
        )

    def book_snapshot(self, book: OrderBook) -> None:
        self.add(
            """INSERT INTO book_snapshots(ts, token_id, bids, asks, best_bid, best_ask, mid, spread)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (book.ts, book.token_id, book.bids_json(), book.asks_json(),
             book.best_bid, book.best_ask, book.mid, book.spread),
        )

    def tick(self, ts: float, token_id: str, event_type: str, price: float, size: float, side: str) -> None:
        self.add(
            "INSERT INTO price_ticks(ts, token_id, event_type, price, size, side) VALUES(%s,%s,%s,%s,%s,%s)",
            (ts, token_id, event_type, price, size, side),
        )

    def crypto_price(self, ts: float, symbol: str, price: float) -> None:
        self.add("INSERT INTO crypto_prices(ts, symbol, price) VALUES(%s,%s,%s)", (ts, symbol, price))

    def candle(self, c: Candle) -> None:
        self.add(
            """INSERT INTO crypto_candles(symbol, open_time, open, high, low, close, volume)
               VALUES(%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(symbol, open_time) DO UPDATE SET
                   open=excluded.open, high=excluded.high, low=excluded.low,
                   close=excluded.close, volume=excluded.volume""",
            (c.symbol, c.open_time, c.open, c.high, c.low, c.close, c.volume),
        )

    def signal(self, s: Signal) -> None:
        self.add(
            """INSERT INTO signals(ts, condition_id, token_id, label, fair, best_bid, best_ask,
                   spread, edge, action, reason, mode) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (s.ts, s.condition_id, s.token_id, s.label, s.fair, s.best_bid, s.best_ask,
             s.spread, s.edge, s.action, s.reason, self.mode),
        )

    def order_insert(self, o: PaperOrder) -> None:
        self.add(
            """INSERT INTO paper_orders(id, created_ts, condition_id, token_id, side, label,
                   price, size, filled, status, fair, edge, mode)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (o.id, o.created_ts, o.condition_id, o.token_id, o.side, o.label,
             o.price, o.size, o.filled, o.status, o.fair, o.edge, self.mode),
        )

    def order_update(self, o: PaperOrder, closed_ts: float | None = None) -> None:
        self.add(
            "UPDATE paper_orders SET status=%s, filled=%s, closed_ts=%s WHERE id=%s",
            (o.status, o.filled, closed_ts, o.id),
        )

    def fill(self, order: PaperOrder, ts: float, price: float, size: float) -> None:
        self.add(
            """INSERT INTO paper_fills(order_id, ts, condition_id, token_id, price, size, mode)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (order.id, ts, order.condition_id, order.token_id, price, size, self.mode),
        )

    def settlement(self, ts: float, condition_id: str, token_id: str, label: str,
                   size: float, avg_price: float, payout: float, pnl: float) -> None:
        self.add(
            """INSERT INTO paper_settlements(ts, condition_id, token_id, label, size,
                   avg_price, payout, pnl, mode) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ts, condition_id, token_id, label, size, avg_price, payout, pnl, self.mode),
        )

    def arb(self, a: ArbOpportunity) -> None:
        self.add(
            """INSERT INTO arb_opportunities(ts, condition_id, yes_ask, no_ask, total, edge,
                   yes_size, no_size) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (a.ts, a.condition_id, a.yes_ask, a.no_ask, a.total, a.edge, a.yes_size, a.no_size),
        )

    # ---- flushing ----------------------------------------------------------
    # Pipeline mode batches the statements into few network round-trips, which
    # matters when the database is remote (VPS).

    def flush_sync(self, conn: psycopg.Connection) -> int:
        rows = self.drain()
        if not rows:
            return 0
        with conn.pipeline():
            for sql, params in rows:
                conn.execute(sql, params)
        conn.commit()
        return len(rows)

    async def flush_async(self, conn: psycopg.AsyncConnection) -> int:
        rows = self.drain()
        if not rows:
            return 0
        async with conn.pipeline():
            for sql, params in rows:
                await conn.execute(sql, params)
        await conn.commit()
        return len(rows)
