"""Runtime calibration gate: block entries in historically unprofitable setups.

Buckets are keyed by (asset, side, tte_bucket, edge_bucket). A setup is allowed
only when it has at least min_bucket_settlements settled tokens AND positive
realized PnL AND adverse-move rate below max_adverse_move_pct.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from ..config import Settings
from ..backtest.calibration import _tte_bucket, enrich_fills

log = logging.getLogger(__name__)


def edge_bucket(edge: float | None) -> str:
    if edge is None:
        return "?"
    b = int(edge * 100)
    return f"{b}-{b + 1}%"


def setup_key(asset: str, label: str, tte_s: float | None, edge: float | None) -> str:
    return f"{asset}_{label}_{_tte_bucket(tte_s)}_{edge_bucket(edge)}"


@dataclass
class BucketStats:
    key: str
    settlements: int
    pnl: float
    win_rate: float
    adverse_rate: float | None


class CalibrationGate:
    """Loads historical bucket stats once; refreshed on bootstrap."""

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._buckets: dict[str, BucketStats] = {}

    def load(self, conn: psycopg.Connection, mode: str = "paper") -> None:
        self._buckets.clear()
        if not self.cfg.require_calibrated_bucket:
            return

        rows = conn.execute(
            """WITH first_fill AS (
                   SELECT DISTINCT ON (f.token_id)
                          f.token_id, f.tte_s, o.edge AS entry_edge, o.label,
                          m.asset
                   FROM paper_fills f
                   JOIN paper_orders o ON o.id = f.order_id
                   JOIN markets m ON m.condition_id = f.condition_id
                   WHERE f.mode = %s
                   ORDER BY f.token_id, f.ts
               )
               SELECT ff.asset, ff.label, ff.entry_edge, ff.tte_s,
                      s.pnl
               FROM paper_settlements s
               JOIN first_fill ff ON ff.token_id = s.token_id
               WHERE s.mode = %s""",
            (mode, mode),
        ).fetchall()

        agg: dict[str, dict] = {}
        for r in rows:
            key = setup_key(r["asset"], r["label"], r["tte_s"], r["entry_edge"])
            if key not in agg:
                agg[key] = {"settlements": 0, "pnl": 0.0, "wins": 0}
            agg[key]["settlements"] += 1
            agg[key]["pnl"] += r["pnl"]
            if r["pnl"] > 0:
                agg[key]["wins"] += 1

        adverse_by_key: dict[str, list[bool]] = {}
        for f in enrich_fills(conn, self.cfg, mode):
            market_row = conn.execute(
                "SELECT asset FROM markets WHERE condition_id = "
                "(SELECT condition_id FROM paper_fills WHERE id = %s)",
                (f.fill_id,),
            ).fetchone()
            asset = market_row["asset"] if market_row else "?"
            key = setup_key(asset, f.label, f.tte_s, f.entry_edge)
            adv = f.spot_horizons.get(5)
            if adv is not None:
                adverse_by_key.setdefault(key, []).append(adv < 0)

        for key, stats in agg.items():
            adv_list = adverse_by_key.get(key, [])
            adverse_rate = (sum(adv_list) / len(adv_list) * 100) if adv_list else None
            n = stats["settlements"]
            self._buckets[key] = BucketStats(
                key=key,
                settlements=n,
                pnl=stats["pnl"],
                win_rate=stats["wins"] / n * 100 if n else 0,
                adverse_rate=adverse_rate,
            )
        log.info("calibration gate loaded %d buckets", len(self._buckets))

    def check(self, asset: str, label: str, tte_s: float, edge: float | None) -> tuple[bool, str]:
        if not self.cfg.require_calibrated_bucket:
            return True, ""
        key = setup_key(asset, label, tte_s, edge)
        stats = self._buckets.get(key)
        if stats is None:
            return False, "calibration:unknown_bucket"
        if stats.settlements < self.cfg.min_bucket_settlements:
            return False, "calibration:insufficient_data"
        if stats.pnl <= 0:
            return False, "calibration:negative_pnl"
        if (stats.adverse_rate is not None
                and stats.adverse_rate > self.cfg.max_adverse_move_pct):
            return False, "calibration:high_adverse"
        return True, ""
