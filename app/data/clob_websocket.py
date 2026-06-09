"""Polymarket CLOB market-channel WebSocket client (public, no auth).

Streams `book` snapshots, `price_change` deltas and `last_trade_price` events
for subscribed token ids. The subscription set can change at runtime (markets
expire / new ones appear); the client reconnects with the new set.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import websockets

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 10.0


def _to_ts(raw) -> float:
    """Polymarket WS timestamps are epoch milliseconds (as strings)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return time.time()
    if v > 1e12:
        v /= 1000.0
    return v


def _levels(raw: list | None) -> list[tuple[float, float]]:
    out = []
    for lvl in raw or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((p, s))
    return out


class MarketWebSocket:
    """Reconnecting market-channel consumer.

    on_event receives normalized dicts:
      {"type": "book", "token_id", "ts", "bids", "asks"}
      {"type": "price_change", "token_id", "ts", "side", "price", "size"}
      {"type": "trade", "token_id", "ts", "price", "size", "side"}
    """

    def __init__(self, on_event: Callable[[dict], None]):
        self._on_event = on_event
        self._assets: frozenset[str] = frozenset()
        self._resubscribe = asyncio.Event()
        self.connected = False

    def set_assets(self, token_ids: set[str]) -> None:
        new = frozenset(token_ids)
        if new != self._assets:
            self._assets = new
            self._resubscribe.set()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            if not self._assets:
                self._resubscribe.clear()
                await self._resubscribe.wait()
                continue
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning("market WS disconnected: %s (retry in %.0fs)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        assets = list(self._assets)
        self._resubscribe.clear()
        async with websockets.connect(WS_URL, max_size=2**24) as ws:
            await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
            self.connected = True
            log.info("market WS subscribed to %d tokens", len(assets))
            ping_task = asyncio.create_task(self._pinger(ws))
            resub_task = asyncio.create_task(self._resubscribe.wait())
            try:
                while True:
                    recv_task = asyncio.create_task(ws.recv())
                    done, _ = await asyncio.wait(
                        {recv_task, resub_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if resub_task in done:
                        recv_task.cancel()
                        log.info("subscription set changed; reconnecting WS")
                        return
                    msg = recv_task.result()
                    self._handle_message(msg)
            finally:
                self.connected = False
                ping_task.cancel()
                resub_task.cancel()

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send("PING")

    def _handle_message(self, msg) -> None:
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", errors="replace")
        if not msg or msg == "PONG":
            return
        try:
            payload = json.loads(msg)
        except json.JSONDecodeError:
            return
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if isinstance(ev, dict):
                try:
                    self._dispatch(ev)
                except Exception:
                    log.exception("error handling WS event: %r", ev)

    def _dispatch(self, ev: dict) -> None:
        etype = ev.get("event_type")
        if etype == "book":
            self._on_event({
                "type": "book",
                "token_id": str(ev.get("asset_id", "")),
                "ts": _to_ts(ev.get("timestamp")),
                "bids": _levels(ev.get("bids") or ev.get("buys")),
                "asks": _levels(ev.get("asks") or ev.get("sells")),
            })
        elif etype == "price_change":
            ts = _to_ts(ev.get("timestamp"))
            changes = ev.get("changes") or ev.get("price_changes")
            items = changes if isinstance(changes, list) else [ev]
            for ch in items:
                if not isinstance(ch, dict):
                    continue
                token = str(ch.get("asset_id") or ev.get("asset_id") or "")
                try:
                    price = float(ch["price"])
                    size = float(ch.get("size", 0))
                except (KeyError, TypeError, ValueError):
                    continue
                self._on_event({
                    "type": "price_change",
                    "token_id": token,
                    "ts": ts,
                    "side": str(ch.get("side", "")).upper(),
                    "price": price,
                    "size": size,
                })
        elif etype == "last_trade_price":
            try:
                price = float(ev["price"])
            except (KeyError, TypeError, ValueError):
                return
            try:
                size = float(ev.get("size", 0))
            except (TypeError, ValueError):
                size = 0.0
            self._on_event({
                "type": "trade",
                "token_id": str(ev.get("asset_id", "")),
                "ts": _to_ts(ev.get("timestamp")),
                "price": price,
                "size": size,
                "side": str(ev.get("side", "")).upper(),
            })
        # tick_size_change and unknown events are ignored
