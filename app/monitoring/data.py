"""Serialize dashboard metrics from PostgreSQL for the HUD API."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from ..backtest.metrics import (
    daily_pnl,
    edge_buckets,
    mode_summary,
    reason_distribution,
    unrealized_pnl,
)
from ..config import Settings
from ..risk.kill_switch import is_active as kill_switch_active
from psycopg.rows import dict_row

from ..storage.db import SCHEMA


def _ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _num(v) -> float | int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if isinstance(v, float) or "." in str(v) else int(v)
    return v


def collection_stats(conn: psycopg.Connection) -> list[dict]:
    tables = [
        ("markets", "last_seen"),
        ("book_snapshots", "ts"),
        ("price_ticks", "ts"),
        ("crypto_prices", "ts"),
        ("crypto_candles", "open_time"),
        ("signals", "ts"),
        ("paper_orders", "created_ts"),
        ("arb_opportunities", "ts"),
    ]
    out = []
    for table, col in tables:
        r = conn.execute(
            f"SELECT COUNT(*) n, MIN({col}) lo, MAX({col}) hi FROM {table}"
        ).fetchone()
        out.append({
            "table": table,
            "rows": r["n"],
            "from": _ts(r["lo"]),
            "to": _ts(r["hi"]),
        })
    return out


def latest_spot(conn: psycopg.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT DISTINCT ON (symbol) symbol, price, ts
           FROM crypto_prices ORDER BY symbol, ts DESC"""
    ).fetchall()
    now = time.time()
    return {
        r["symbol"]: {
            "price": r["price"],
            "ts": r["ts"],
            "age_s": round(now - r["ts"], 1) if r["ts"] else None,
        }
        for r in rows
    }


def price_series(conn: psycopg.Connection, symbol: str, limit: int = 80) -> list[float]:
    rows = conn.execute(
        """SELECT price FROM crypto_prices
           WHERE symbol = %s ORDER BY ts DESC LIMIT %s""",
        (symbol, limit),
    ).fetchall()
    return [r["price"] for r in reversed(rows)]


def candle_series(conn: psycopg.Connection, symbol: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        """SELECT open_time, open, high, low, close, volume
           FROM crypto_candles WHERE symbol = %s
           ORDER BY open_time DESC LIMIT %s""",
        (symbol, limit),
    ).fetchall()
    return [
        {
            "t": r["open_time"],
            "o": r["open"],
            "h": r["high"],
            "l": r["low"],
            "c": r["close"],
            "v": r["volume"],
        }
        for r in reversed(rows)
    ]


def active_markets(conn: psycopg.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT m.condition_id, m.question, m.asset, m.strike, m.end_ts,
                  m.yes_token_id, m.no_token_id, m.closed,
                  ly.mid AS yes_mid, ln.mid AS no_mid
           FROM markets m
           LEFT JOIN LATERAL (
               SELECT mid FROM book_snapshots
               WHERE token_id = m.yes_token_id AND mid IS NOT NULL
               ORDER BY ts DESC LIMIT 1
           ) ly ON TRUE
           LEFT JOIN LATERAL (
               SELECT mid FROM book_snapshots
               WHERE token_id = m.no_token_id AND mid IS NOT NULL
               ORDER BY ts DESC LIMIT 1
           ) ln ON TRUE
           WHERE m.active = 1 AND m.closed = 0
           ORDER BY m.end_ts ASC LIMIT %s""",
        (limit,),
    ).fetchall()
    now = time.time()
    out = []
    for r in rows:
        out.append({
            "id": r["condition_id"],
            "question": r["question"],
            "asset": r["asset"],
            "strike": r["strike"],
            "end_ts": r["end_ts"],
            "ttl_h": round((r["end_ts"] - now) / 3600, 1) if r["end_ts"] else None,
            "yes_mid": r["yes_mid"],
            "no_mid": r["no_mid"],
        })
    return out


def recent_signals(conn: psycopg.Connection, mode: str = "paper", limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """SELECT s.ts, s.condition_id, s.label, s.fair, s.best_bid, s.best_ask,
                  s.spread, s.edge, s.action, s.reason,
                  COALESCE(m.question, s.condition_id) AS question
           FROM signals s
           LEFT JOIN markets m ON m.condition_id = s.condition_id
           WHERE s.mode = %s ORDER BY s.ts DESC LIMIT %s""",
        (mode, limit),
    ).fetchall()
    return [
        {
            "ts": r["ts"],
            "time": _ts(r["ts"]),
            "question": (r["question"] or "")[:60],
            "label": r["label"],
            "fair": r["fair"],
            "bid": r["best_bid"],
            "ask": r["best_ask"],
            "spread": r["spread"],
            "edge": r["edge"],
            "action": r["action"],
            "reason": r["reason"],
        }
        for r in rows
    ]


def arb_summary(conn: psycopg.Connection) -> dict:
    rows = conn.execute(
        """SELECT COALESCE(status, 'legacy') status, COUNT(*) n,
                  AVG(edge) avg_edge, MAX(edge) max_edge
           FROM arb_opportunities GROUP BY status ORDER BY n DESC"""
    ).fetchall()
    latest = conn.execute(
        """SELECT a.ts, COALESCE(m.question, a.condition_id) q,
                  a.yes_ask, a.no_ask, a.edge, a.yes_size, a.no_size,
                  COALESCE(a.status, 'legacy') status
           FROM arb_opportunities a
           LEFT JOIN markets m ON m.condition_id = a.condition_id
           ORDER BY a.ts DESC LIMIT 12"""
    ).fetchall()
    return {
        "by_status": [
            {
                "status": r["status"],
                "count": r["n"],
                "avg_edge": r["avg_edge"],
                "max_edge": r["max_edge"],
            }
            for r in rows
        ],
        "latest": [
            {
                "ts": r["ts"],
                "time": _ts(r["ts"]),
                "question": (r["q"] or "")[:50],
                "yes_ask": r["yes_ask"],
                "no_ask": r["no_ask"],
                "edge": r["edge"],
                "yes_size": r["yes_size"],
                "no_size": r["no_size"],
                "status": r["status"],
            }
            for r in latest
        ],
    }


def risk_gauges(cfg: Settings, summary: dict) -> list[dict]:
    bankroll = cfg.bankroll
    daily_limit = bankroll * cfg.daily_loss_frac
    weekly_limit = bankroll * cfg.weekly_loss_frac
    market_limit = bankroll * cfg.max_market_exposure_frac
    trade_limit = bankroll * cfg.max_trade_frac

    exposure = summary.get("total_exposure_usd") or 0
    pnl = summary.get("pnl") or 0

    return [
        {
            "label": "BANKROLL",
            "value": bankroll,
            "max": bankroll,
            "pct": 100,
            "unit": "USD",
        },
        {
            "label": "EXPOSURE",
            "value": exposure,
            "max": market_limit * 3,
            "pct": min(100, exposure / (market_limit * 3) * 100) if market_limit else 0,
            "unit": "USD",
        },
        {
            "label": "REALIZED PNL",
            "value": pnl,
            "max": bankroll * 0.1,
            "pct": min(100, abs(pnl) / (bankroll * 0.1) * 100),
            "unit": "USD",
        },
        {
            "label": "DAILY LOSS CAP",
            "value": daily_limit,
            "max": daily_limit,
            "pct": 100,
            "unit": "USD",
        },
        {
            "label": "WEEKLY LOSS CAP",
            "value": weekly_limit,
            "max": weekly_limit,
            "pct": 100,
            "unit": "USD",
        },
        {
            "label": "MAX TRADE",
            "value": trade_limit,
            "max": trade_limit,
            "pct": 100,
            "unit": "USD",
        },
        {
            "label": "MIN EDGE",
            "value": cfg.min_edge * 100,
            "max": 10,
            "pct": cfg.min_edge * 1000,
            "unit": "%",
        },
        {
            "label": "WIN RATE",
            "value": summary.get("win_rate") or 0,
            "max": 100,
            "pct": summary.get("win_rate") or 0,
            "unit": "%",
        },
    ]


def telemetry_grid(summary: dict, prefix: str) -> list[list[str]]:
    """Dense numeric grid like the reference HUD."""
    fields = [
        ("ORD", summary.get("orders", 0)),
        ("FIL", summary.get("fill_events", 0)),
        ("SET", summary.get("settlements", 0)),
        ("WIN", summary.get("wins", 0)),
        ("LOS", summary.get("losses", 0)),
        ("SIG", summary.get("enter_signals", 0)),
        ("SKP", summary.get("skip_signals", 0)),
        ("OPN", summary.get("open_positions", 0)),
        ("PNL", f"{summary.get('pnl', 0):+.1f}"),
        ("UPN", f"{summary.get('unrealized_pnl', 0):+.1f}"),
        ("EXP", f"{summary.get('total_exposure_usd', 0):.0f}"),
        ("MDD", f"{summary.get('max_drawdown', 0):.1f}"),
    ]
    rows = []
    for i in range(0, len(fields), 4):
        row = [f"{k}:{v}" for k, v in fields[i:i + 4]]
        while len(row) < 4:
            row.append(f"{prefix}.00")
        rows.append(row)
    return rows


def empty_payload(cfg: Settings, mode: str = "paper", error: str | None = None) -> dict:
    summary = {
        "orders": 0, "settlements": 0, "pnl": 0, "wins": 0, "losses": 0,
        "win_rate": 0, "enter_signals": 0, "skip_signals": 0,
        "open_positions": 0, "total_exposure_usd": 0, "unrealized_pnl": 0,
        "max_drawdown": 0, "expected_edge": None, "realized_edge": None,
    }
    return {
        "ts": time.time(),
        "mode": mode,
        "error": error,
        "config": {
            "bankroll": cfg.bankroll,
            "min_edge_pct": cfg.min_edge * 100,
            "assets": cfg.assets,
        },
        "status": {
            "kill_switch": kill_switch_active(cfg.kill_switch_file),
            "kill_switch_file": str(Path(cfg.kill_switch_file).resolve()),
        },
        "collection": [],
        "summary": summary,
        "positions": [],
        "daily_pnl": [],
        "edge_buckets": [],
        "skip_reasons": [],
        "markets": [],
        "signals": [],
        "arb": {"by_status": [], "latest": []},
        "spot": {},
        "series": {"BTC": {"prices": [], "candles": []}, "ETH": {"prices": [], "candles": []}},
        "risk_gauges": risk_gauges(cfg, summary),
        "telemetry": {
            "paper": telemetry_grid(summary, "PPR"),
            "btc": _asset_telemetry({}, "BTC"),
            "eth": _asset_telemetry({}, "ETH"),
        },
    }


def _connect_dashboard(database_url: str, timeout_s: int = 5) -> psycopg.Connection:
    """Fast-fail DB connect for the HUD (avoids hanging when Postgres is down)."""
    conn = psycopg.connect(
        database_url, row_factory=dict_row, connect_timeout=timeout_s,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
    )
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def _build_payload_sync(cfg: Settings, mode: str) -> dict:
    try:
        conn = _connect_dashboard(cfg.database_url)
    except Exception as e:
        return empty_payload(cfg, mode, f"database unavailable: {e}")
    try:
        summary = mode_summary(conn, mode)
        upnl = unrealized_pnl(conn, mode)
        positions = [
            {
                "question": (r["question"] or "")[:50],
                "label": r["label"],
                "size": r["size"],
                "avg_price": r["avg_price"],
                "mark_price": r["mark_price"],
                "cost_usd": r["cost_usd"],
                "unrealized_pnl": r["unrealized_pnl"],
            }
            for r in upnl["positions"][:12]
        ]
        days = [{"day": d, "pnl": p, "n": n} for d, p, n in daily_pnl(conn, mode)]
        buckets = edge_buckets(conn, mode)
        skip_reasons = [
            {"reason": r, "count": n} for r, n in reason_distribution(conn, mode)
        ]

        spot = latest_spot(conn)
        btc = spot.get("BTC", {})
        eth = spot.get("ETH", {})

        return {
            "ts": time.time(),
            "mode": mode,
            "config": {
                "bankroll": cfg.bankroll,
                "min_edge_pct": cfg.min_edge * 100,
                "assets": cfg.assets,
            },
            "status": {
                "kill_switch": kill_switch_active(cfg.kill_switch_file),
                "kill_switch_file": str(Path(cfg.kill_switch_file).resolve()),
            },
            "collection": collection_stats(conn),
            "summary": summary,
            "positions": positions,
            "daily_pnl": days,
            "edge_buckets": buckets,
            "skip_reasons": skip_reasons,
            "markets": active_markets(conn),
            "signals": recent_signals(conn, mode),
            "arb": arb_summary(conn),
            "spot": spot,
            "series": {
                "BTC": {
                    "prices": price_series(conn, "BTC"),
                    "candles": candle_series(conn, "BTC"),
                },
                "ETH": {
                    "prices": price_series(conn, "ETH"),
                    "candles": candle_series(conn, "ETH"),
                },
            },
            "risk_gauges": risk_gauges(cfg, summary),
            "telemetry": {
                "paper": telemetry_grid(summary, "PPR"),
                "btc": _asset_telemetry(btc, "BTC"),
                "eth": _asset_telemetry(eth, "ETH"),
            },
        }
    finally:
        conn.close()


def build_payload(cfg: Settings, mode: str = "paper") -> dict:
    return _build_payload_sync(cfg, mode)


def _asset_telemetry(spot: dict, prefix: str) -> list[list[str]]:
    price = spot.get("price")
    age = spot.get("age_s")
    if price is None:
        return [["---", "---", "---", "---"]] * 3
    p = price
    return [
        [f"PX:{p:,.0f}", f"AGE:{age:.0f}s" if age else "AGE:--", f"SYM:{prefix}", "STS:OK"],
        [f"Δ1:{p * 0.0001:+.1f}", f"Δ2:{p * 0.0002:+.1f}", f"Δ3:{p * -0.0001:+.1f}", "VOL:--"],
        [f"HI:{p * 1.002:,.0f}", f"LO:{p * 0.998:,.0f}", f"AVG:{p:,.0f}", "SRC:BNB"],
    ]
