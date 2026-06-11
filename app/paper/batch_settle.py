"""Batch-settle open paper/backtest positions from DB using outcome or spot fallback."""
from __future__ import annotations

import logging
import time

from datetime import datetime, timezone

from ..config import Settings
from ..storage.db import connect_sync
from ..storage.models import Market
from .settlement import resolve_outcome

log = logging.getLogger(__name__)


def _diagnose(conn, mode: str, grace: float, now: float) -> dict:
    """Break down why settle-paper may find nothing to settle."""
    row = conn.execute(
        """SELECT COUNT(DISTINCT f.token_id) AS open_tokens,
                  COUNT(DISTINCT f.condition_id) AS open_markets
           FROM paper_fills f
           WHERE f.mode = %s
             AND f.token_id NOT IN (
                 SELECT token_id FROM paper_settlements WHERE mode = %s)""",
        (mode, mode),
    ).fetchone()
    row2 = conn.execute(
        """WITH open_pos AS (
               SELECT DISTINCT f.condition_id
               FROM paper_fills f
               WHERE f.mode = %s
                 AND f.token_id NOT IN (
                     SELECT token_id FROM paper_settlements WHERE mode = %s)
           )
           SELECT
               COUNT(*) AS open_market_rows,
               SUM(CASE WHEN m.condition_id IS NULL THEN 1 ELSE 0 END) AS missing_market_row,
               SUM(CASE WHEN m.end_ts IS NULL AND m.condition_id IS NOT NULL THEN 1 ELSE 0 END) AS null_end_ts,
               SUM(CASE WHEN m.end_ts IS NOT NULL AND m.end_ts + %s <= %s THEN 1 ELSE 0 END) AS expired,
               SUM(CASE WHEN m.end_ts IS NOT NULL AND m.end_ts + %s > %s THEN 1 ELSE 0 END) AS not_expired,
               MIN(m.end_ts) AS min_end_ts,
               MAX(m.end_ts) AS max_end_ts
           FROM open_pos p
           LEFT JOIN markets m ON m.condition_id = p.condition_id""",
        (mode, mode, grace, now, grace, now),
    ).fetchone()
    return {
        "open_tokens": row["open_tokens"] or 0,
        "open_markets": row["open_markets"] or 0,
        "markets_missing_row": row2["missing_market_row"] or 0,
        "markets_null_end_ts": row2["null_end_ts"] or 0,
        "markets_expired": row2["expired"] or 0,
        "markets_not_expired": row2["not_expired"] or 0,
        "min_end_ts": row2["min_end_ts"],
        "max_end_ts": row2["max_end_ts"],
    }


def _spot_at_expiry(conn, asset: str, end_ts: float) -> float | None:
    row = conn.execute(
        "SELECT price FROM crypto_prices WHERE symbol=%s AND ts <= %s "
        "ORDER BY ts DESC LIMIT 1",
        (asset, end_ts),
    ).fetchone()
    return row["price"] if row else None


def settle_open_positions(
    cfg: Settings,
    mode: str = "paper",
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Insert settlements for expired markets that still have open fill positions."""
    conn = connect_sync(cfg.database_url)
    now = time.time()
    grace = 0.0 if force else cfg.paper_settle_grace_s
    try:
        rows = conn.execute(
            """WITH open_pos AS (
                   SELECT f.token_id, f.condition_id, o.label,
                          SUM(f.size) AS size,
                          SUM(f.price * f.size) / NULLIF(SUM(f.size), 0) AS avg_price
                   FROM paper_fills f
                   JOIN paper_orders o ON o.id = f.order_id
                   WHERE f.mode = %s
                     AND f.token_id NOT IN (
                         SELECT token_id FROM paper_settlements WHERE mode = %s)
                   GROUP BY f.token_id, f.condition_id, o.label
               )
               SELECT p.token_id, p.condition_id, p.label, p.size, p.avg_price,
                      m.question, m.asset, m.strike, m.direction, m.end_ts,
                      m.outcome, m.yes_token_id, m.no_token_id
               FROM open_pos p
               JOIN markets m ON m.condition_id = p.condition_id
               WHERE m.end_ts + %s <= %s
               ORDER BY m.end_ts, p.token_id""",
            (mode, mode, grace, now),
        ).fetchall()
        if not rows:
            diag = _diagnose(conn, mode, grace, now)
            if diag["open_tokens"]:
                min_end = diag["min_end_ts"]
                max_end = diag["max_end_ts"]
                log.info(
                    "settle-paper: nothing eligible — %d open tokens across %d markets "
                    "(%d expired, %d not expired yet, %d missing markets row); "
                    "end_ts range %s .. %s (now=%s, grace=%.0fs)",
                    diag["open_tokens"], diag["open_markets"],
                    diag["markets_expired"], diag["markets_not_expired"],
                    diag["markets_missing_row"],
                    datetime.fromtimestamp(min_end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    if min_end else "-",
                    datetime.fromtimestamp(max_end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    if max_end else "-",
                    datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    grace,
                )
            return {"markets": 0, "positions": 0, "pnl": 0.0, "skipped": 0, **diag}

        by_market: dict[str, list] = {}
        meta: dict[str, dict] = {}
        for r in rows:
            cid = r["condition_id"]
            by_market.setdefault(cid, []).append(r)
            meta[cid] = r

        markets_settled = positions_settled = skipped = 0
        total_pnl = 0.0
        for cid, positions in by_market.items():
            mrow = meta[cid]
            market = Market(
                condition_id=cid,
                question=mrow["question"] or "",
                slug="",
                asset=mrow["asset"],
                strike=mrow["strike"],
                direction=mrow["direction"],
                end_ts=mrow["end_ts"],
                yes_token_id=mrow["yes_token_id"],
                no_token_id=mrow["no_token_id"],
                outcome=mrow["outcome"],
            )
            spot = _spot_at_expiry(conn, market.asset, market.end_ts)
            outcome = resolve_outcome(market, spot)
            if outcome is None:
                skipped += 1
                log.warning(
                    "skip settle %s: no outcome and no spot at expiry (asset=%s end=%.0f)",
                    (market.question or cid)[:60], market.asset, market.end_ts,
                )
                continue

            market_pnl = 0.0
            for pos in positions:
                payout = outcome if pos["token_id"] == market.yes_token_id else 1.0 - outcome
                pnl = pos["size"] * (payout - pos["avg_price"])
                market_pnl += pnl
                if not dry_run:
                    conn.execute(
                        """INSERT INTO paper_settlements
                           (ts, condition_id, token_id, label, size, avg_price, payout, pnl, mode)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (now, cid, pos["token_id"], pos["label"], pos["size"],
                         pos["avg_price"], payout, pnl, mode),
                    )
                positions_settled += 1

            if not dry_run:
                conn.execute(
                    "UPDATE markets SET closed=1, outcome=%s, last_seen=%s WHERE condition_id=%s",
                    (outcome, now, cid),
                )
            markets_settled += 1
            total_pnl += market_pnl
            src = "recorded" if mrow["outcome"] is not None else f"spot={spot:,.0f}"
            log.info(
                "batch settle %s -> YES=%.2f (%s) pnl=%+.2f (%d positions)",
                (market.question or cid)[:60], outcome, src, market_pnl, len(positions),
            )

        if not dry_run:
            conn.commit()
        return {
            "markets": markets_settled,
            "positions": positions_settled,
            "pnl": total_pnl,
            "skipped": skipped,
            "dry_run": dry_run,
        }
    finally:
        conn.close()
