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


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


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
        """SELECT COUNT(*) n, COALESCE(SUM(size),0) shares, COALESCE(SUM(price*size),0) vol
           FROM paper_fills WHERE mode=%s""", (mode,)).fetchone()
    s["fill_events"] = row["n"]
    s["filled_shares"] = row["shares"]
    s["fill_volume_usd"] = row["vol"]

    # unambiguous order/fill breakdown (see review: filled vs touched vs events)
    row = conn.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN filled > 0 THEN 1 ELSE 0 END) any_fill,
                  SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) fully_filled,
                  SUM(CASE WHEN filled > 0 AND status != 'filled' THEN 1 ELSE 0 END) partial_only,
                  SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) cancelled,
                  SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open,
                  MIN(created_ts) lo, MAX(created_ts) hi
           FROM paper_orders WHERE mode=%s""", (mode,)).fetchone()
    s["orders"] = row["total"]
    s["orders_any_fill"] = row["any_fill"] or 0
    s["orders_fully_filled"] = row["fully_filled"] or 0
    s["orders_partial_only"] = row["partial_only"] or 0
    s["orders_cancelled"] = row["cancelled"] or 0
    s["orders_open"] = row["open"] or 0
    span_min = ((row["hi"] - row["lo"]) / 60.0) if row["lo"] is not None and row["hi"] > row["lo"] else None
    s["orders_per_min"] = (s["orders"] / span_min) if span_min else None

    row = conn.execute(
        """SELECT SUM(CASE WHEN action='enter' THEN 1 ELSE 0 END) enters,
                  SUM(CASE WHEN action='skip' THEN 1 ELSE 0 END) skips
           FROM signals WHERE mode=%s""", (mode,)).fetchone()
    s["enter_signals"] = row["enters"] or 0
    s["skip_signals"] = row["skips"] or 0

    # order lifetime (created -> closed) for cancelled / filled orders
    row = conn.execute(
        """SELECT AVG(closed_ts - created_ts) avg_l,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY closed_ts - created_ts) med_l
           FROM paper_orders WHERE mode=%s AND closed_ts IS NOT NULL""", (mode,)).fetchone()
    s["avg_lifetime"] = row["avg_l"]
    s["med_lifetime"] = row["med_l"]

    # expected edge at entry vs realized edge per settled share
    row = conn.execute(
        """SELECT SUM(o.edge * f.size) / NULLIF(SUM(f.size), 0) exp_edge,
                  SUM(f.size * (s.payout - f.price)) / NULLIF(SUM(f.size), 0) real_edge
           FROM paper_fills f
           JOIN paper_orders o ON o.id = f.order_id
           JOIN paper_settlements s ON s.mode = f.mode AND s.token_id = f.token_id
           WHERE f.mode=%s""", (mode,)).fetchone()
    s["expected_edge"] = row["exp_edge"]
    s["realized_edge"] = row["real_edge"]

    # max drawdown over cumulative settlement pnl (time order)
    cum = peak = mdd = 0.0
    for r in conn.execute(
        "SELECT pnl FROM paper_settlements WHERE mode=%s ORDER BY ts", (mode,)
    ).fetchall():
        cum += r["pnl"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    s["max_drawdown"] = mdd

    upnl = unrealized_pnl(conn, mode)
    s["open_positions"] = upnl["open_positions"]
    s["total_exposure_usd"] = upnl["total_exposure_usd"]
    s["unrealized_pnl"] = upnl["unrealized_pnl"]
    return s


def daily_pnl(conn: psycopg.Connection, mode: str) -> list[tuple[str, float, int]]:
    cur = conn.execute(
        """SELECT to_char(to_timestamp(ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
                  SUM(pnl) AS pnl, COUNT(*) AS n
           FROM paper_settlements WHERE mode=%s GROUP BY day ORDER BY day""", (mode,))
    return [(r["day"], r["pnl"], r["n"]) for r in cur.fetchall()]


def edge_buckets(conn: psycopg.Connection, mode: str) -> list[dict]:
    """Per entry-edge bucket: orders, fills, settled tokens, win rate, realized PnL."""
    placed = {
        r["bucket"]: (r["placed"], r["touched"] or 0)
        for r in conn.execute(
            """SELECT floor(edge*100)::int AS bucket, COUNT(*) AS placed,
                      SUM(CASE WHEN filled > 0 THEN 1 ELSE 0 END) AS touched
               FROM paper_orders WHERE mode=%s GROUP BY bucket""", (mode,)).fetchall()
    }
    fills = {
        r["bucket"]: r
        for r in conn.execute(
            """SELECT floor(o.edge*100)::int AS bucket,
                      COUNT(*) AS fill_events, SUM(f.size) AS shares,
                      COUNT(DISTINCT s.token_id) AS settled_tokens,
                      SUM(f.size * (s.payout - f.price)) AS pnl
               FROM paper_fills f
               JOIN paper_orders o ON o.id = f.order_id
               LEFT JOIN paper_settlements s ON s.mode = f.mode AND s.token_id = f.token_id
               WHERE f.mode=%s GROUP BY bucket""", (mode,)).fetchall()
    }
    wins = {
        r["bucket"]: (r["wins"] or 0, r["n"])
        for r in conn.execute(
            """SELECT bucket, SUM(CASE WHEN tok_pnl > 0 THEN 1 ELSE 0 END) wins, COUNT(*) n
               FROM (
                   SELECT floor(o.edge*100)::int AS bucket, f.token_id,
                          SUM(f.size * (s.payout - f.price)) AS tok_pnl
                   FROM paper_fills f
                   JOIN paper_orders o ON o.id = f.order_id
                   JOIN paper_settlements s ON s.mode = f.mode AND s.token_id = f.token_id
                   WHERE f.mode=%s GROUP BY bucket, f.token_id
               ) x GROUP BY bucket""", (mode,)).fetchall()
    }
    out = []
    for bucket in sorted(set(placed) | set(fills)):
        p, touched = placed.get(bucket, (0, 0))
        f = fills.get(bucket)
        w = wins.get(bucket)
        out.append({
            "label": f"{bucket}-{bucket + 1}%",
            "orders": p,
            "orders_any_fill": touched,
            "fill_events": f["fill_events"] if f else 0,
            "settled": f["settled_tokens"] if f else 0,
            "win_rate": (w[0] / w[1] * 100) if w and w[1] else None,
            "pnl": f["pnl"] if f and f["pnl"] is not None else None,
        })
    return out


def reason_distribution(conn: psycopg.Connection, mode: str) -> list[tuple[str, int]]:
    cur = conn.execute(
        """SELECT reason, COUNT(*) n FROM signals
           WHERE mode=%s AND action='skip' GROUP BY reason ORDER BY n DESC""", (mode,))
    return [(r["reason"] or "?", r["n"]) for r in cur.fetchall()]


def cancel_reasons(conn: psycopg.Connection, mode: str) -> list[tuple[str, int]]:
    cur = conn.execute(
        """SELECT CASE
                  WHEN cancel_reason IS NULL THEN 'legacy_unknown'
                  ELSE cancel_reason
               END reason, COUNT(*) n
           FROM paper_orders WHERE mode=%s AND status='cancelled'
           GROUP BY reason ORDER BY n DESC""", (mode,))
    return [(r["reason"], r["n"]) for r in cur.fetchall()]


def unrealized_pnl(conn: psycopg.Connection, mode: str) -> dict:
    """Open positions from fills not yet settled, marked to latest book mid."""
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
           ),
           latest_mid AS (
               SELECT DISTINCT ON (token_id) token_id, mid
               FROM book_snapshots
               WHERE mid IS NOT NULL
               ORDER BY token_id, ts DESC
           )
           SELECT p.token_id, p.condition_id, p.label, p.size, p.avg_price,
                  m.mid AS mark_price,
                  p.size * p.avg_price AS cost_usd,
                  p.size * m.mid AS mark_usd,
                  p.size * (m.mid - p.avg_price) AS unrealized_pnl,
                  COALESCE(mk.question, p.condition_id) AS question
           FROM open_pos p
           LEFT JOIN latest_mid m ON m.token_id = p.token_id
           LEFT JOIN markets mk ON mk.condition_id = p.condition_id
           ORDER BY ABS(p.size * (m.mid - p.avg_price)) DESC NULLS LAST""",
        (mode, mode),
    ).fetchall()
    total_cost = sum(r["cost_usd"] or 0 for r in rows)
    total_upnl = sum(r["unrealized_pnl"] or 0 for r in rows if r["mark_price"] is not None)
    return {
        "open_positions": len(rows),
        "total_exposure_usd": total_cost,
        "unrealized_pnl": total_upnl,
        "positions": rows,
    }


def pnl_by_side(conn: psycopg.Connection, mode: str) -> list[dict]:
    cur = conn.execute(
        """SELECT label, COUNT(*) n, SUM(pnl) pnl,
                  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins
           FROM paper_settlements WHERE mode=%s GROUP BY label ORDER BY label""", (mode,))
    return cur.fetchall()


def pnl_by_market(conn: psycopg.Connection, mode: str, limit: int = 10) -> list[dict]:
    cur = conn.execute(
        """SELECT s.condition_id, COALESCE(MIN(m.question), '?') question,
                  COUNT(*) n, SUM(s.pnl) pnl
           FROM paper_settlements s LEFT JOIN markets m ON m.condition_id = s.condition_id
           WHERE s.mode=%s GROUP BY s.condition_id ORDER BY ABS(SUM(s.pnl)) DESC
           LIMIT %s""", (mode, limit))
    return cur.fetchall()


def churn_by_market(conn: psycopg.Connection, mode: str, limit: int = 10) -> list[dict]:
    cur = conn.execute(
        """SELECT o.condition_id, COALESCE(MIN(m.question), '?') question,
                  COUNT(*) orders,
                  SUM(CASE WHEN o.status='cancelled' THEN 1 ELSE 0 END) cancelled,
                  SUM(CASE WHEN o.cancel_reason='replace' THEN 1 ELSE 0 END) replaced,
                  SUM(CASE WHEN o.filled > 0 THEN 1 ELSE 0 END) any_fill
           FROM paper_orders o LEFT JOIN markets m ON m.condition_id = o.condition_id
           WHERE o.mode=%s GROUP BY o.condition_id ORDER BY COUNT(*) DESC
           LIMIT %s""", (mode, limit))
    return cur.fetchall()


def print_report(cfg: Settings) -> None:
    console = Console()
    conn = connect_sync(cfg.database_url)
    try:
        _print_collected(console, conn)
        for mode in ("paper", "backtest"):
            _print_mode(console, conn, mode)
        _print_comparison(console, conn)
        _print_arb(console, conn)
    finally:
        conn.close()


def _print_comparison(console: Console, conn: psycopg.Connection) -> None:
    paper = mode_summary(conn, "paper")
    back = mode_summary(conn, "backtest")
    if paper["orders"] == 0 and back["orders"] == 0:
        return
    t = Table(title="paper vs backtest (key metrics)")
    t.add_column("metric")
    t.add_column("paper", justify="right")
    t.add_column("backtest", justify="right")
    rows = [
        ("settlements", str(paper["settlements"]), str(back["settlements"])),
        ("win rate",
         f"{paper['win_rate']:.1f}%" if paper["settlements"] else "-",
         f"{back['win_rate']:.1f}%" if back["settlements"] else "-"),
        ("realized PnL", f"${paper['pnl']:+.2f}", f"${back['pnl']:+.2f}"),
        ("fill rate",
         f"{paper['orders_any_fill'] / paper['orders'] * 100:.1f}%" if paper["orders"] else "-",
         f"{back['orders_any_fill'] / back['orders'] * 100:.1f}%" if back["orders"] else "-"),
        ("orders / min",
         f"{paper['orders_per_min']:.2f}" if paper["orders_per_min"] else "-",
         f"{back['orders_per_min']:.2f}" if back["orders_per_min"] else "-"),
        ("open positions", str(paper.get("open_positions", 0)), str(back.get("open_positions", 0))),
        ("unrealized PnL",
         f"${paper.get('unrealized_pnl', 0):+.2f}" if paper.get("open_positions") else "-",
         f"${back.get('unrealized_pnl', 0):+.2f}" if back.get("open_positions") else "-"),
    ]
    for label, p, b in rows:
        t.add_row(label, p, b)
    console.print(t)
    if paper["settlements"] == 0 and paper.get("open_positions", 0) > 0:
        console.print(
            "[dim]paper has open positions but no settlements — run "
            "`python -m app.main settle-paper` or wait for Gamma resolution[/dim]"
        )


def _print_collected(console: Console, conn: psycopg.Connection) -> None:
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


def _print_mode(console: Console, conn: psycopg.Connection, mode: str) -> None:
    s = mode_summary(conn, mode)
    if s["orders"] == 0 and s["settlements"] == 0 and s["enter_signals"] == 0:
        console.print(f"[dim]{mode}: no activity recorded[/dim]")
        return

    t = Table(title=f"{mode} results")
    t.add_column("metric"); t.add_column("value", justify="right")
    t.add_row("signals (enter / skip)", f"{s['enter_signals']} / {s['skip_signals']}")
    t.add_row("orders placed", str(s["orders"]))
    t.add_row("orders with any fill", str(s["orders_any_fill"]))
    t.add_row("fully filled orders", str(s["orders_fully_filled"]))
    t.add_row("partial-only orders", str(s["orders_partial_only"]))
    t.add_row("orders cancelled / open", f"{s['orders_cancelled']} / {s['orders_open']}")
    t.add_row("fill events", str(s["fill_events"]))
    t.add_row("filled shares", f"{s['filled_shares']:.1f}")
    t.add_row("fill volume", f"${s['fill_volume_usd']:.2f}")
    if s["orders_per_min"] is not None:
        t.add_row("orders per minute", f"{s['orders_per_min']:.2f}")
    t.add_row("avg / median order lifetime",
              f"{_fmt_dur(s['avg_lifetime'])} / {_fmt_dur(s['med_lifetime'])}")
    t.add_row("settlements (W/L)", f"{s['settlements']} ({s['wins']}/{s['losses']})")
    t.add_row("win rate", f"{s['win_rate']:.1f}%")
    if s["expected_edge"] is not None:
        t.add_row("expected edge (entry, settled fills)", f"{s['expected_edge']*100:+.2f}%")
        t.add_row("realized edge (per settled share)", f"{s['realized_edge']*100:+.2f}%")
    t.add_row("realized PnL", f"${s['pnl']:+.2f}")
    if s.get("open_positions"):
        t.add_row("open positions", str(s["open_positions"]))
        t.add_row("total exposure", f"${s['total_exposure_usd']:.2f}")
        t.add_row("unrealized PnL (mid mark)", f"${s['unrealized_pnl']:+.2f}")
    t.add_row("max drawdown", f"${s['max_drawdown']:.2f}")
    console.print(t)

    upnl = unrealized_pnl(conn, mode)
    if upnl["positions"]:
        t = Table(title=f"{mode} open positions (unrealized)")
        t.add_column("market"); t.add_column("side")
        t.add_column("size", justify="right"); t.add_column("avg", justify="right")
        t.add_column("mid", justify="right"); t.add_column("exposure", justify="right")
        t.add_column("uPnL", justify="right")
        for r in upnl["positions"][:15]:
            t.add_row(
                r["question"][:50], r["label"],
                f"{r['size']:.1f}", f"{r['avg_price']:.3f}",
                f"{r['mark_price']:.3f}" if r["mark_price"] is not None else "-",
                f"${r['cost_usd']:.2f}",
                f"${r['unrealized_pnl']:+.2f}" if r["unrealized_pnl"] is not None else "-",
            )
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
        t = Table(title=f"{mode} by entry edge bucket")
        t.add_column("edge"); t.add_column("orders", justify="right")
        t.add_column("got fills", justify="right")
        t.add_column("fill events", justify="right")
        t.add_column("settled", justify="right")
        t.add_column("win rate", justify="right")
        t.add_column("realized PnL", justify="right")
        for b in buckets:
            t.add_row(
                b["label"], str(b["orders"]), str(b["orders_any_fill"]),
                str(b["fill_events"]), str(b["settled"]),
                f"{b['win_rate']:.0f}%" if b["win_rate"] is not None else "-",
                f"${b['pnl']:+.2f}" if b["pnl"] is not None else "-",
            )
        console.print(t)

    sides = pnl_by_side(conn, mode)
    if sides:
        t = Table(title=f"{mode} PnL by side")
        t.add_column("side"); t.add_column("settlements", justify="right")
        t.add_column("wins", justify="right"); t.add_column("pnl", justify="right")
        for r in sides:
            t.add_row(r["label"], str(r["n"]), str(r["wins"] or 0), f"${r['pnl']:+.2f}")
        console.print(t)

    markets = pnl_by_market(conn, mode)
    if markets:
        t = Table(title=f"{mode} PnL by market (top {len(markets)})")
        t.add_column("market"); t.add_column("settlements", justify="right")
        t.add_column("pnl", justify="right")
        for r in markets:
            t.add_row(r["question"][:60], str(r["n"]), f"${r['pnl']:+.2f}")
        console.print(t)

    cancels = cancel_reasons(conn, mode)
    if cancels:
        t = Table(title=f"{mode} cancel reasons")
        t.add_column("reason"); t.add_column("orders", justify="right")
        for reason, n in cancels:
            t.add_row(reason, str(n))
        console.print(t)
        if any(r == "legacy_unknown" for r, _ in cancels):
            console.print("[dim]legacy_unknown = cancelled before cancel_reason logging "
                          "(or missing update); new runs use explicit reasons[/dim]")

    churn = churn_by_market(conn, mode)
    if churn:
        t = Table(title=f"{mode} order churn by market (top {len(churn)})")
        t.add_column("market"); t.add_column("orders", justify="right")
        t.add_column("cancelled", justify="right"); t.add_column("replaced", justify="right")
        t.add_column("got fills", justify="right")
        for r in churn:
            t.add_row(r["question"][:60], str(r["orders"]),
                      str(r["cancelled"] or 0), str(r["replaced"] or 0),
                      str(r["any_fill"] or 0))
        console.print(t)

    reasons = reason_distribution(conn, mode)
    if reasons:
        t = Table(title=f"{mode} skip / reject reasons (throttled counts)")
        t.add_column("reason"); t.add_column("signals", justify="right")
        for reason, n in reasons:
            t.add_row(reason, str(n))
        console.print(t)


def _print_arb(console: Console, conn: psycopg.Connection) -> None:
    rows = conn.execute(
        """SELECT COALESCE(status, 'legacy') status, COUNT(*) n,
                  AVG(edge) avg_edge, MAX(edge) max_edge
           FROM arb_opportunities GROUP BY status ORDER BY n DESC""").fetchall()
    if not rows:
        return
    t = Table(title="YES/NO arb candidates by validation status")
    t.add_column("status"); t.add_column("count", justify="right")
    t.add_column("avg edge", justify="right"); t.add_column("max edge", justify="right")
    for r in rows:
        t.add_row(r["status"], str(r["n"]),
                  f"{r['avg_edge']*100:.2f}%", f"{r['max_edge']*100:.2f}%")
    console.print(t)
    console.print("[dim]legacy = recorded before validation fields existed; "
                  "only status='ok' rows are real candidates[/dim]")

    detail = conn.execute(
        """SELECT a.ts, COALESCE(m.question, a.condition_id) q, a.yes_ask, a.no_ask, a.edge,
                  a.yes_size, a.no_size, a.yes_book_ts, a.no_book_ts,
                  COALESCE(a.status, 'legacy') status
           FROM arb_opportunities a LEFT JOIN markets m ON m.condition_id = a.condition_id
           ORDER BY a.ts DESC LIMIT 15""").fetchall()
    t = Table(title="latest arb candidates")
    t.add_column("ts"); t.add_column("market"); t.add_column("yes/no ask", justify="right")
    t.add_column("edge", justify="right"); t.add_column("depth y/n", justify="right")
    t.add_column("book gap", justify="right"); t.add_column("status")
    for r in detail:
        gap = (abs(r["yes_book_ts"] - r["no_book_ts"])
               if r["yes_book_ts"] is not None and r["no_book_ts"] is not None else None)
        t.add_row(
            _fmt_ts(r["ts"]), r["q"][:45],
            f"{r['yes_ask']:.3f}/{r['no_ask']:.3f}", f"{r['edge']*100:.1f}%",
            f"{r['yes_size']:.0f}/{r['no_size']:.0f}",
            f"{gap:.2f}s" if gap is not None else "-", r["status"],
        )
    console.print(t)
