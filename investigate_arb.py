"""One-off audit of recorded arb_opportunities outliers (review item: 36% edge).

For each recorded opportunity, pull the market, and the nearest recorded book
snapshot of each leg around the opportunity timestamp, to see whether the edge
was real or an artifact of stale/empty books.
"""
import json

from app.config import load_settings
from app.storage.db import connect_sync

conn = connect_sync(load_settings().database_url)

arbs = conn.execute("SELECT * FROM arb_opportunities ORDER BY ts").fetchall()
print(f"{len(arbs)} arb rows recorded\n")

for a in arbs:
    m = conn.execute("SELECT * FROM markets WHERE condition_id=%s",
                     (a["condition_id"],)).fetchone()
    print("=" * 100)
    print(f"ts={a['ts']:.0f} edge={a['edge']*100:.2f}% yes_ask={a['yes_ask']} "
          f"no_ask={a['no_ask']} total={a['total']} yes_size={a['yes_size']} "
          f"no_size={a['no_size']} status={a.get('status')}")
    if m is None:
        print("  !! market not found")
        continue
    print(f"  market: {m['question']!r} end_ts={m['end_ts']:.0f} "
          f"(opportunity {(m['end_ts'] - a['ts'])/60:.1f} min before expiry) "
          f"active={m['active']} closed={m['closed']} outcome={m['outcome']}")
    for label, tok in (("YES", m["yes_token_id"]), ("NO", m["no_token_id"])):
        r = conn.execute(
            """SELECT ts, best_bid, best_ask, bids, asks FROM book_snapshots
               WHERE token_id=%s AND ts <= %s ORDER BY ts DESC LIMIT 1""",
            (tok, a["ts"])).fetchone()
        if r is None:
            print(f"  {label}: no snapshot at/before opportunity ts")
            continue
        age = a["ts"] - r["ts"]
        asks = json.loads(r["asks"])[:3]
        bids = json.loads(r["bids"])[:3]
        print(f"  {label}: snapshot age={age:.1f}s best_bid={r['best_bid']} "
              f"best_ask={r['best_ask']} top_bids={bids} top_asks={asks}")
        nxt = conn.execute(
            """SELECT ts, best_bid, best_ask FROM book_snapshots
               WHERE token_id=%s AND ts > %s ORDER BY ts LIMIT 1""",
            (tok, a["ts"])).fetchone()
        if nxt:
            print(f"        next snapshot +{nxt['ts'] - a['ts']:.1f}s "
                  f"best_bid={nxt['best_bid']} best_ask={nxt['best_ask']}")

conn.close()
