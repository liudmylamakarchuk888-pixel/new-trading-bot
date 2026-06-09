"""Performance metrics and the `report` command output (rich tables)."""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from rich.console import Console
from rich.table import Table

from ..config import Settings
from ..storage.db import connect_sync


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def mode_summary(conn: psycopg.Connection, mode: str) -> dict:
    s = {}
    row = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl,
                  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,
                  SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) losses
           FROM paper_settlements WHERE mode=%s""", (mode,)).fetchone()
    s["settlements"] = row["n"]
    s["pnl"] = row["pnl"]
    s["wins"] = row["wins"] or 0
    s["losses"] = row["losses"] or 0
    s["win_rate"] = (s["wins"] / s["settlements"] * 100) if s["settlements"] else 0.0

    row = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(price*size),0) vol
           FROM paper_fills WHERE mode=%s""", (mode,)).fetchone()
    s["fills"] = row["n"]
    s["fill_volume_usd"] = row["vol"]

    row = conn.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) filled,
                  SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) cancelled,
                  SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open
           FROM paper_orders WHERE mode=%s""", (mode,)).fetchone()
    s["orders"] = row["total"]
    s["orders_filled"] = row["filled"] or 0
    s["orders_cancelled"] = row["cancelled"] or 0
    s["orders_open"] = row["open"] or 0

    row = conn.execute(
        "SELECT COUNT(*) n FROM signals WHERE mode=%s AND action='enter'", (mode,)).fetchone()
    s["enter_signals"] = row["n"]

    # max drawdown over cumulative settlement pnl (time order)
    cum = peak = mdd = 0.0
    for r in conn.execute(
        "SELECT pnl FROM paper_settlements WHERE mode=%s ORDER BY ts", (mode,)
    ).fetchall():
        cum += r["pnl"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    s["max_drawdown"] = mdd
    return s


def daily_pnl(conn: psycopg.Connection, mode: str) -> list[tuple[str, float, int]]:
    cur = conn.execute(
        """SELECT to_char(to_timestamp(ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD') day,
                  SUM(pnl) pnl, COUNT(*) n
           FROM paper_settlements WHERE mode=%s GROUP BY day ORDER BY day""", (mode,))
    return [(r["day"], r["pnl"], r["n"]) for r in cur.fetchall()]


def edge_buckets(conn: psycopg.Connection, mode: str) -> list[tuple[str, int, int]]:
    """Distribution of entry edge across orders (placed vs filled)."""
    cur = conn.execute(
        """SELECT floor(edge*100)::int bucket,
                  COUNT(*) placed,
                  SUM(CASE WHEN filled > 0 THEN 1 ELSE 0 END) touched
           FROM paper_orders WHERE mode=%s GROUP BY bucket ORDER BY bucket""", (mode,))
    return [(f"{r['bucket']}-{r['bucket'] + 1}%", r["placed"], r["touched"] or 0)
            for r in cur.fetchall()]


def print_report(cfg: Settings) -> None:
    console = Console()
    conn = connect_sync(cfg.database_url)
    try:
        # data collection stats
        t = Table(title="Collected data", show_lines=False)
        t.add_column("table"); t.add_column("rows", justify="right")
        t.add_column("from"); t.add_column("to")
        for table, ts_col in [("markets", "last_seen"), ("book_snapshots", "ts"),
                              ("price_ticks", "ts"), ("crypto_prices", "ts"),
                              ("crypto_candles", "open_time"), ("arb_opportunities", "ts")]:
            r = conn.execute(
                f"SELECT COUNT(*) n, MIN({ts_col}) lo, MAX({ts_col}) hi FROM {table}").fetchone()
            t.add_row(table, str(r["n"]), _fmt_ts(r["lo"]), _fmt_ts(r["hi"]))
        console.print(t)

        for mode in ("paper", "backtest"):
            s = mode_summary(conn, mode)
            if s["orders"] == 0 and s["settlements"] == 0:
                console.print(f"[dim]{mode}: no activity recorded[/dim]")
                continue
            t = Table(title=f"{mode} results")
            t.add_column("metric"); t.add_column("value", justify="right")
            t.add_row("enter signals", str(s["enter_signals"]))
            t.add_row("orders placed", str(s["orders"]))
            t.add_row("orders filled / cancelled / open",
                      f"{s['orders_filled']} / {s['orders_cancelled']} / {s['orders_open']}")
            t.add_row("fills", str(s["fills"]))
            t.add_row("fill volume", f"${s['fill_volume_usd']:.2f}")
            t.add_row("settlements (W/L)", f"{s['settlements']} ({s['wins']}/{s['losses']})")
            t.add_row("win rate", f"{s['win_rate']:.1f}%")
            t.add_row("realized PnL", f"${s['pnl']:+.2f}")
            t.add_row("max drawdown", f"${s['max_drawdown']:.2f}")
            console.print(t)

            days = daily_pnl(conn, mode)
            if days:
                t = Table(title=f"{mode} daily PnL")
                t.add_column("day"); t.add_column("pnl", justify="right")
                t.add_column("settlements", justify="right")
                for day, pnl, n in days:
                    t.add_row(day, f"${pnl:+.2f}", str(n))
                console.print(t)

            buckets = edge_buckets(conn, mode)
            if buckets:
                t = Table(title=f"{mode} entry edge distribution")
                t.add_column("edge"); t.add_column("orders", justify="right")
                t.add_column("got fills", justify="right")
                for label, placed, touched in buckets:
                    t.add_row(label, str(placed), str(touched))
                console.print(t)

        # arbitrage opportunities summary
        r = conn.execute(
            """SELECT COUNT(*) n, COALESCE(AVG(edge),0) avg_edge, COALESCE(MAX(edge),0) max_edge
               FROM arb_opportunities""").fetchone()
        if r["n"]:
            console.print(
                f"YES/NO arb opportunities recorded: {r['n']} "
                f"(avg edge {r['avg_edge']*100:.2f}%, max {r['max_edge']*100:.2f}%)")
    finally:
        conn.close()
