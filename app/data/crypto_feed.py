"""BTC/ETH spot price feed: Binance WebSocket (trades + 1m klines) with Coinbase REST fallback.

Bootstraps recent 1m candles over REST so the volatility estimator works immediately.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import httpx
import websockets

from ..storage.models import Candle

log = logging.getLogger(__name__)

BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/stream"
COINBASE_REST = "https://api.coinbase.com/v2/prices/{pair}/spot"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{pair}/candles"

SYMBOL_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
COINBASE_PAIR = {"BTC": "BTC-USD", "ETH": "ETH-USD"}


class CryptoFeed:
    """Pushes (symbol, ts, price) and closed 1m Candle objects to callbacks."""

    def __init__(
        self,
        assets: list[str],
        on_price: Callable[[str, float, float], None],
        on_candle: Callable[[Candle], None],
    ):
        self.assets = [a for a in assets if a in SYMBOL_MAP]
        self.on_price = on_price
        self.on_candle = on_candle
        self.connected = False

    # ---- bootstrap -----------------------------------------------------------

    async def bootstrap(self, client: httpx.AsyncClient, candles: int = 600) -> None:
        for asset in self.assets:
            rows = await self._fetch_binance_klines(client, asset, candles)
            if not rows:
                rows = await self._fetch_coinbase_candles(client, asset, candles)
            for c in rows:
                self.on_candle(c)
            if rows:
                last = rows[-1]
                self.on_price(asset, last.open_time + 60.0, last.close)
            log.info("bootstrapped %d 1m candles for %s", len(rows), asset)

    async def _fetch_binance_klines(
        self, client: httpx.AsyncClient, asset: str, limit: int
    ) -> list[Candle]:
        try:
            r = await client.get(
                f"{BINANCE_REST}/api/v3/klines",
                params={"symbol": SYMBOL_MAP[asset], "interval": "1m", "limit": min(limit, 1000)},
                timeout=20,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("binance klines failed for %s: %s", asset, e)
            return []
        out = []
        for k in r.json():
            out.append(Candle(
                symbol=asset, open_time=k[0] / 1000.0,
                open=float(k[1]), high=float(k[2]), low=float(k[3]),
                close=float(k[4]), volume=float(k[5]),
            ))
        return out

    async def _fetch_coinbase_candles(
        self, client: httpx.AsyncClient, asset: str, limit: int
    ) -> list[Candle]:
        try:
            r = await client.get(
                COINBASE_CANDLES.format(pair=COINBASE_PAIR[asset]),
                params={"granularity": 60},
                timeout=20,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("coinbase candles failed for %s: %s", asset, e)
            return []
        rows = []
        for k in r.json()[:limit]:  # [time, low, high, open, close, volume], newest first
            rows.append(Candle(
                symbol=asset, open_time=float(k[0]),
                open=float(k[3]), high=float(k[2]), low=float(k[1]),
                close=float(k[4]), volume=float(k[5]),
            ))
        rows.sort(key=lambda c: c.open_time)
        return rows

    # ---- live stream ----------------------------------------------------------

    async def run(self) -> None:
        binance_failures = 0
        while True:
            try:
                await self._binance_session()
                binance_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                binance_failures += 1
                log.warning("binance WS error (%d): %s", binance_failures, e)
            if binance_failures >= 5:
                log.warning("binance unreachable; falling back to Coinbase polling for 5 min")
                try:
                    await self._coinbase_poll(duration_s=300.0)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("coinbase fallback error: %s", e)
                binance_failures = 0
            await asyncio.sleep(min(2.0 * binance_failures + 1.0, 15.0))

    async def _binance_session(self) -> None:
        streams = []
        for asset in self.assets:
            s = SYMBOL_MAP[asset].lower()
            streams.append(f"{s}@miniTicker")
            streams.append(f"{s}@kline_1m")
        url = f"{BINANCE_WS}?streams={'/'.join(streams)}"
        rev = {v: k for k, v in SYMBOL_MAP.items()}
        async with websockets.connect(url, max_size=2**22, ping_interval=20) as ws:
            self.connected = True
            log.info("binance WS connected (%s)", ", ".join(self.assets))
            async for msg in ws:
                try:
                    data = json.loads(msg).get("data") or {}
                except json.JSONDecodeError:
                    continue
                etype = data.get("e")
                if etype == "24hrMiniTicker":
                    asset = rev.get(data.get("s", ""))
                    if asset:
                        self.on_price(asset, data.get("E", 0) / 1000.0 or time.time(), float(data["c"]))
                elif etype == "kline":
                    k = data.get("k") or {}
                    asset = rev.get(data.get("s", ""))
                    if asset and k.get("x"):  # closed candle only
                        self.on_candle(Candle(
                            symbol=asset, open_time=k["t"] / 1000.0,
                            open=float(k["o"]), high=float(k["h"]), low=float(k["l"]),
                            close=float(k["c"]), volume=float(k["v"]),
                        ))

    async def _coinbase_poll(self, duration_s: float, interval_s: float = 3.0) -> None:
        """Degraded mode: poll spot prices and synthesize 1m candles from polls."""
        deadline = time.time() + duration_s
        builders: dict[str, Candle | None] = {a: None for a in self.assets}
        async with httpx.AsyncClient() as client:
            self.connected = True
            while time.time() < deadline:
                now = time.time()
                for asset in self.assets:
                    try:
                        r = await client.get(COINBASE_REST.format(pair=COINBASE_PAIR[asset]), timeout=10)
                        r.raise_for_status()
                        price = float(r.json()["data"]["amount"])
                    except (httpx.HTTPError, KeyError, TypeError, ValueError):
                        continue
                    self.on_price(asset, now, price)
                    minute = now - (now % 60.0)
                    b = builders[asset]
                    if b is None or b.open_time != minute:
                        if b is not None:
                            self.on_candle(b)
                        builders[asset] = Candle(asset, minute, price, price, price, price, 0.0)
                    else:
                        b.high = max(b.high, price)
                        b.low = min(b.low, price)
                        b.close = price
                await asyncio.sleep(interval_s)
        self.connected = False
