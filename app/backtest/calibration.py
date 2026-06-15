"""Fill calibration and adverse-selection analysis for the report command.

Recomputes fair at fill from recorded spot/candles when paper_fills.fair_at_fill
is missing (legacy rows). New fills persist fair/spot/tte at execution time.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ..config import Settings
from ..storage.models import Market
from ..strategy.fair_price import annualized_vol, fair_yes_probability

TTE_LABELS = ("<6h", "6-24h", "24-48h", ">48h")
SPOT_HORIZONS_MIN = (1, 5, 15, 30)


def _tte_bucket(tte_s: float | None) -> str:
    if tte_s is None:
        return "?"
    h = tte_s / 3600.0
    if h < 6:
        return "<6h"
    if h < 24:
        return "6-24h"
    if h < 48:
        return "24-48h"
    return ">48h"


def _spot_at(conn: psycopg.Connection, symbol: str, ts: float) -> float | None:
    row = conn.execute(
        "SELECT price FROM crypto_prices WHERE symbol=%s AND ts <= %s ORDER BY ts DESC LIMIT 1",
        (symbol, ts),
    ).fetchone()
    return row["price"] if row else None


def _closes_before(conn: psycopg.Connection, symbol: str, ts: float, limit: int = 1500) -> list[float]:
    rows = conn.execute(
        """SELECT close FROM crypto_candles
           WHERE symbol=%s AND open_time <= %s
           ORDER BY open_time DESC LIMIT %s""",
        (symbol, ts, limit),
    ).fetchall()
    return [r["close"] for r in reversed(rows)]


def _fair_for_side(market: Market, spot: float, sigma: float | None, ts: float, label: str) -> float | None:
    if sigma is None:
        return None
    fair_yes = fair_yes_probability(market, spot, sigma, ts)
    return fair_yes if label == "YES" else 1.0 - fair_yes


def _favorable_spot_pct(label: str, direction: str, spot0: float, spot1: float) -> float:
    """Signed spot return; positive means the move favored our token."""
    if spot0 <= 0:
        return 0.0
    raw = (spot1 - spot0) / spot0
    up = raw > 0
    if direction == "above":
        good = up if label == "YES" else not up
    else:
        good = (not up) if label == "YES" else up
    return raw if good else -abs(raw)


@dataclass
class EnrichedFill:
    fill_id: int
    token_id: str
    ts: float
    label: str
    price: float
    size: float
    entry_fair: float
    entry_edge: float
    fair_at_fill: float | None
    tte_s: float | None
    fill_edge: float | None          # fair_at_fill - price - cost
    fair_drift: float | None         # fair_at_fill - entry_fair
    settled: bool
    pnl: float | None
    spot_horizons: dict[int, float | None]  # minutes -> favorable spot pct
    tte_bucket: str
    order_age_s: float | None = None


def _load_markets(conn: psycopg.Connection, condition_ids: set[str]) -> dict[str, Market]:
    if not condition_ids:
        return {}
    rows = conn.execute(
        "SELECT * FROM markets WHERE condition_id = ANY(%s)",
        (list(condition_ids),),
    ).fetchall()
    out: dict[str, Market] = {}
    for r in rows:
        out[r["condition_id"]] = Market(
            condition_id=r["condition_id"], question=r["question"] or "", slug=r["slug"] or "",
            asset=r["asset"], strike=r["strike"], direction=r["direction"],
            end_ts=r["end_ts"], yes_token_id=r["yes_token_id"], no_token_id=r["no_token_id"],
            active=bool(r["active"]), closed=bool(r["closed"]), outcome=r["outcome"],
        )
    return out


def enrich_fills(conn: psycopg.Connection, cfg: Settings, mode: str) -> list[EnrichedFill]:
    rows = conn.execute(
        """SELECT f.id AS fill_id, f.ts, f.price, f.size, f.token_id, f.condition_id,
                  f.fair_at_fill, f.spot_at_fill, f.tte_s, f.order_age_s,
                  o.fair AS entry_fair, o.edge AS entry_edge, o.label,
                  s.pnl AS settlement_pnl,
                  m.asset, m.direction, m.end_ts
           FROM paper_fills f
           JOIN paper_orders o ON o.id = f.order_id
           LEFT JOIN paper_settlements s ON s.mode = f.mode AND s.token_id = f.token_id
           LEFT JOIN markets m ON m.condition_id = f.condition_id
           WHERE f.mode = %s
           ORDER BY f.ts""",
        (mode,),
    ).fetchall()
    if not rows:
        return []

    markets = _load_markets(conn, {r["condition_id"] for r in rows if r["condition_id"]})
    vol_cache: dict[tuple[str, int], float | None] = {}
    out: list[EnrichedFill] = []

    for r in rows:
        market = markets.get(r["condition_id"])
        ts = r["ts"]
        tte_s = r["tte_s"]
        if tte_s is None and market is not None:
            tte_s = market.end_ts - ts

        fair_at_fill = r["fair_at_fill"]
        if fair_at_fill is None and market is not None:
            spot = r["spot_at_fill"] or _spot_at(conn, market.asset, ts)
            if spot is not None:
                key = (market.asset, int(ts // 60))
                if key not in vol_cache:
                    vol_cache[key] = annualized_vol(
                        _closes_before(conn, market.asset, ts),
                        lam=cfg.vol_lambda, min_obs=cfg.min_vol_candles,
                    )
                fair_at_fill = _fair_for_side(market, spot, vol_cache[key], ts, r["label"])

        fill_edge = (fair_at_fill - r["price"] - cfg.cost) if fair_at_fill is not None else None
        fair_drift = (fair_at_fill - r["entry_fair"]) if fair_at_fill is not None else None

        spot_horizons: dict[int, float | None] = {}
        if market is not None:
            spot0 = r["spot_at_fill"] or _spot_at(conn, market.asset, ts)
            if spot0 is not None:
                for mins in SPOT_HORIZONS_MIN:
                    spot1 = _spot_at(conn, market.asset, ts + mins * 60)
                    spot_horizons[mins] = (
                        _favorable_spot_pct(r["label"], market.direction, spot0, spot1)
                        if spot1 is not None else None
                    )
            else:
                for mins in SPOT_HORIZONS_MIN:
                    spot_horizons[mins] = None

        out.append(EnrichedFill(
            fill_id=r["fill_id"],
            token_id=r["token_id"],
            ts=ts,
            label=r["label"],
            price=r["price"],
            size=r["size"],
            entry_fair=r["entry_fair"],
            entry_edge=r["entry_edge"],
            fair_at_fill=fair_at_fill,
            tte_s=tte_s,
            fill_edge=fill_edge,
            fair_drift=fair_drift,
            settled=r["settlement_pnl"] is not None,
            pnl=r["settlement_pnl"],
            spot_horizons=spot_horizons,
            tte_bucket=_tte_bucket(tte_s),
            order_age_s=r["order_age_s"],
        ))
    return out


def _avg(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def calibration_by_tte(fills: list[EnrichedFill]) -> list[dict]:
    """Settled tokens grouped by time-to-expiry at first fill (PnL counted once per token)."""
    # one row per settled token: first fill sets TTE bucket; pnl is token-level
    by_token: dict[str, EnrichedFill] = {}
    for f in fills:
        if not f.settled or f.pnl is None:
            continue
        prev = by_token.get(f.token_id)
        if prev is None or f.ts < prev.ts:
            by_token[f.token_id] = f

    buckets: dict[str, list[EnrichedFill]] = {b: [] for b in TTE_LABELS}
    for f in by_token.values():
        if f.tte_bucket in buckets:
            buckets[f.tte_bucket].append(f)

    out = []
    for label in TTE_LABELS:
        group = buckets[label]
        if not group:
            continue
        wins = sum(1 for f in group if f.pnl > 0)
        out.append({
            "tte": label,
            "tokens": len(group),
            "fills": sum(1 for f in fills if f.settled and f.token_id in {g.token_id for g in group}),
            "wins": wins,
            "win_rate": wins / len(group) * 100,
            "pnl": sum(f.pnl for f in group),
            "avg_entry_edge": _avg([f.entry_edge for f in group]),
            "avg_fill_edge": _avg([f.fill_edge for f in group if f.fill_edge is not None]),
            "avg_fair_drift": _avg([f.fair_drift for f in group if f.fair_drift is not None]),
        })
    return out


def fill_quality_summary(fills: list[EnrichedFill], cfg: Settings) -> dict:
    """Aggregate fill price vs fair and post-fill spot moves."""
    with_fair = [f for f in fills if f.fair_at_fill is not None]
    settled = [f for f in fills if f.settled]

    def adverse_rate(mins: int) -> float | None:
        vals = [f.spot_horizons.get(mins) for f in with_fair]
        known = [v for v in vals if v is not None]
        if not known:
            return None
        return sum(1 for v in known if v < 0) / len(known) * 100

    fav_keys = {1: "avg_fav_spot_1m", 5: "avg_fav_spot_5m", 15: "avg_fav_spot_15m", 30: "avg_fav_spot_30m"}
    adv_keys = {1: "adverse_1m_pct", 5: "adverse_5m_pct", 15: "adverse_15m_pct", 30: "adverse_30m_pct"}

    result = {
        "fills": len(fills),
        "with_fair": len(with_fair),
        "settled": len(settled),
        "avg_entry_edge": _avg([f.entry_edge for f in fills]),
        "avg_fill_edge": _avg([f.fill_edge for f in with_fair if f.fill_edge is not None]),
        "avg_fair_drift": _avg([f.fair_drift for f in with_fair if f.fair_drift is not None]),
        "avg_price_vs_fair": _avg([f.fair_at_fill - f.price for f in with_fair if f.fair_at_fill]),
        "cost": cfg.cost,
    }
    for mins in (1, 5, 15, 30):
        result[adv_keys[mins]] = adverse_rate(mins)
        result[fav_keys[mins]] = _avg([
            f.spot_horizons[mins] for f in with_fair if f.spot_horizons.get(mins) is not None
        ])
    return result
