"""Polymarket Gamma API: market discovery and BTC/ETH terminal-price market parsing.

Only markets of the form "Will Bitcoin/Ethereum be above/below $X on/by <time>?"
are accepted. One-touch style markets ("hit", "reach", "dip to", "touch") and
extremum markets ("highest price", "all-time high", ...) are intentionally
skipped: their fair value is NOT the terminal digital-option price and
mispricing them is worse than not trading them. "Up or Down" markets are also
skipped (no parsable strike).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

from ..storage.models import Market

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CRYPTO_TAG_ID = "21"   # Polymarket "Crypto" tag
PAGE_SIZE = 100        # gamma caps limit at 100

_RX_BTC = re.compile(r"\b(bitcoin|btc)\b", re.I)
_RX_ETH = re.compile(r"\b(ethereum|eth)\b", re.I)
_RX_ABOVE = re.compile(r"\b(above|over|higher than|greater than|at least)\b", re.I)
_RX_BELOW = re.compile(r"\b(below|under|lower than|less than)\b", re.I)
_RX_TOUCH = re.compile(r"\b(hit|hits|reach|reaches|touch|touches|dip to|dips to)\b", re.I)
_RX_EXTREMUM = re.compile(
    r"\b(highest|lowest|peak|maximum|minimum|all[\s-]?time\s+high|ath|intraday)\b", re.I
)
_RX_PRICE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*([kKmM]?)")


def _parse_price(num: str, suffix: str) -> float:
    v = float(num.replace(",", ""))
    if suffix.lower() == "k":
        v *= 1_000
    elif suffix.lower() == "m":
        v *= 1_000_000
    return v


def parse_question(question: str) -> tuple[str, str, float] | None:
    """Return (asset, direction, strike) or None if the market is not tradable by this bot."""
    is_btc = bool(_RX_BTC.search(question))
    is_eth = bool(_RX_ETH.search(question))
    if is_btc == is_eth:  # neither, or ambiguous both
        return None
    asset = "BTC" if is_btc else "ETH"

    if _RX_TOUCH.search(question) or _RX_EXTREMUM.search(question):
        return None  # one-touch / extremum market: terminal model would misprice it

    above = bool(_RX_ABOVE.search(question))
    below = bool(_RX_BELOW.search(question))
    if above == below:
        return None
    direction = "above" if above else "below"

    prices = _RX_PRICE.findall(question)
    strikes = {_parse_price(n, s) for n, s in prices}
    if len(strikes) != 1:  # no strike, or a range ("between $X and $Y")
        return None
    return asset, direction, strikes.pop()


def _parse_end_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_gamma_market(m: dict) -> Market | None:
    condition_id = m.get("conditionId")
    question = m.get("question") or ""
    if not condition_id or not question:
        return None
    if m.get("enableOrderBook") is False:
        return None

    outcomes = [str(o).lower() for o in _json_list(m.get("outcomes"))]
    if outcomes != ["yes", "no"]:
        return None

    token_ids = [str(t) for t in _json_list(m.get("clobTokenIds"))]
    if len(token_ids) != 2:
        return None

    parsed = parse_question(question)
    if parsed is None:
        return None
    asset, direction, strike = parsed

    end_ts = _parse_end_ts(m.get("endDate"))
    if end_ts is None:
        return None

    outcome = _parse_outcome(m)
    return Market(
        condition_id=condition_id,
        question=question,
        slug=m.get("slug") or "",
        asset=asset,
        strike=strike,
        direction=direction,
        end_ts=end_ts,
        yes_token_id=token_ids[0],
        no_token_id=token_ids[1],
        active=bool(m.get("active", True)),
        closed=bool(m.get("closed", False)),
        outcome=outcome,
    )


def _parse_outcome(m: dict) -> float | None:
    """YES payout per share once the market is closed/resolved."""
    if not m.get("closed"):
        return None
    prices = _json_list(m.get("outcomePrices"))
    if len(prices) == 2:
        try:
            return float(prices[0])
        except (TypeError, ValueError):
            return None
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def fetch_active_crypto_markets(
    client: httpx.AsyncClient, now: float, window_days: float, max_pages: int = 60
) -> list[Market]:
    """List active, not-yet-closed BTC/ETH terminal-price markets ending within the window.

    Filters by the Crypto tag server-side; most results are short-lived
    "Up or Down" markets which the parser rejects, so we page until exhausted.
    """
    found: dict[str, Market] = {}
    for page in range(max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "tag_id": CRYPTO_TAG_ID,
            "limit": str(PAGE_SIZE),
            "offset": str(page * PAGE_SIZE),
            "order": "endDate",
            "ascending": "true",
            "end_date_min": _iso(now - 3600),
            "end_date_max": _iso(now + window_days * 86400),
        }
        r = await client.get(f"{GAMMA_BASE}/markets", params=params, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for raw in batch:
            mk = parse_gamma_market(raw)
            if mk is not None and not mk.closed and mk.end_ts > now:
                found[mk.condition_id] = mk
        if len(batch) < PAGE_SIZE:
            break
    return list(found.values())


async def fetch_markets_by_condition(
    client: httpx.AsyncClient, condition_ids: list[str]
) -> dict[str, dict]:
    """Fetch raw gamma market dicts keyed by condition id (used for resolution polling)."""
    out: dict[str, dict] = {}
    for i in range(0, len(condition_ids), 20):
        chunk = condition_ids[i : i + 20]
        params = [("condition_ids", cid) for cid in chunk]
        try:
            r = await client.get(f"{GAMMA_BASE}/markets", params=params, timeout=30)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("gamma resolution poll failed: %s", e)
            continue
        for raw in r.json():
            cid = raw.get("conditionId")
            if cid:
                out[cid] = raw
    return out
