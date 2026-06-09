"""Polymarket CLOB public REST endpoints (no auth): book snapshots, prices, history."""
from __future__ import annotations

import logging
import time

import httpx

from ..storage.models import OrderBook

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"


def _levels(raw: list | None) -> list[tuple[float, float]]:
    out = []
    for lvl in raw or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if s > 0:
            out.append((p, s))
    return out


async def get_order_book(client: httpx.AsyncClient, token_id: str) -> OrderBook | None:
    try:
        r = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=15)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.debug("clob /book failed for %s: %s", token_id, e)
        return None
    j = r.json()
    bids = sorted(_levels(j.get("bids")), key=lambda x: -x[0])
    asks = sorted(_levels(j.get("asks")), key=lambda x: x[0])
    ts = time.time()
    raw_ts = j.get("timestamp")
    if raw_ts:
        try:
            ts = float(raw_ts) / 1000.0
        except (TypeError, ValueError):
            pass
    return OrderBook(token_id=token_id, ts=ts, bids=bids, asks=asks)


async def get_midpoint(client: httpx.AsyncClient, token_id: str) -> float | None:
    try:
        r = await client.get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id}, timeout=15)
        r.raise_for_status()
        return float(r.json()["mid"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


async def get_prices_history(
    client: httpx.AsyncClient, token_id: str, start_ts: float, end_ts: float, fidelity_min: int = 1
) -> list[tuple[float, float]]:
    """Midpoint history [(ts, price)] - auxiliary data only (no depth, not used for fills)."""
    try:
        r = await client.get(
            f"{CLOB_BASE}/prices-history",
            params={
                "market": token_id,
                "startTs": int(start_ts),
                "endTs": int(end_ts),
                "fidelity": fidelity_min,
            },
            timeout=30,
        )
        r.raise_for_status()
        hist = r.json().get("history", [])
        return [(float(h["t"]), float(h["p"])) for h in hist]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
        log.debug("prices-history failed for %s: %s", token_id, e)
        return []
