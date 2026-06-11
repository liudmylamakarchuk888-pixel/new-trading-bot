"""Offline smoke test: parser, fair price, edge, paper fills, risk, and a synthetic backtest.

Needs a reachable PostgreSQL server; uses a dedicated `trading_bot_smoke`
database (created automatically) so it never pollutes the real bot data.
"""
import os
import time

os.environ["BOT_DATABASE_URL"] = os.environ.get(
    "BOT_SMOKE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/trading_bot_smoke")

import logging

from app.config import load_settings
from app.data.gamma_client import parse_gamma_market, parse_question
from app.paper.position_manager import PositionManager
from app.paper.settlement import outcome_from_spot, resolve_outcome
from app.data.recorder import DataHub
from app.paper.paper_engine import StrategyEngine
from app.risk.risk_engine import RiskEngine
from app.storage.db import Sink, connect_sync
from app.storage.models import Candle, Market, OrderBook, TradeTick
from app.strategy.edge_detector import evaluate_market, maker_price
from app.strategy.fair_price import annualized_vol, fair_yes_probability, prob_above

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# --- question parser ---------------------------------------------------------
check("parse above", parse_question("Will Bitcoin be above $110,000 on June 12?") == ("BTC", "above", 110000.0))
check("parse below", parse_question("Ethereum below $2,500 on June 11?") == ("ETH", "below", 2500.0))
check("parse $110K suffix", parse_question("Bitcoin above $110K by Friday?") == ("BTC", "above", 110000.0))
check("skip touch market", parse_question("Will Bitcoin hit $150,000 in June?") is None)
check("skip up-or-down", parse_question("Bitcoin Up or Down on June 10?") is None)
check("skip range", parse_question("Will BTC be between $100,000 and $110,000?") is None)
check("skip non-crypto", parse_question("Will it rain in NYC above 2 inches?") is None)

# --- fair price ---------------------------------------------------------------
p_itm = prob_above(110000, 100000, 0.5, 3600)
p_otm = prob_above(100000, 110000, 0.5, 3600)
p_atm = prob_above(100000, 100000, 0.5, 365 * 24 * 3600)
check("deep ITM ~1", p_itm > 0.99)
check("deep OTM ~0", p_otm < 0.01)
check("ATM ~0.5", 0.35 < p_atm < 0.55)
check("expired -> indicator", prob_above(101, 100, 0.5, 0) == 1.0)

closes = [100000.0]
import random
random.seed(7)
for _ in range(300):
    closes.append(closes[-1] * (1 + random.gauss(0, 0.0005)))
vol = annualized_vol(closes, min_obs=60)
check("vol estimated", vol is not None and 0.05 < vol < 2.0)
check("vol insufficient history -> None", annualized_vol(closes[:30], min_obs=60) is None)

# --- edge detector --------------------------------------------------------------
cfg = load_settings()
now = time.time()
mkt = Market("0xc1", "Will Bitcoin be above $100,000 today?", "slug", "BTC",
             100000.0, "above", now + 3600, "YT", "NT")
yes_book = OrderBook("YT", now, bids=[(0.50, 100)], asks=[(0.52, 100)])
no_book = OrderBook("NT", now, bids=[(0.46, 100)], asks=[(0.48, 100)])

sigs = evaluate_market(mkt, 0.56, yes_book, no_book, now, cfg)
yes_sig = next(s for s in sigs if s.label == "YES")
no_sig = next(s for s in sigs if s.label == "NO")
check("YES edge 3% -> enter", yes_sig.action == "enter" and abs(yes_sig.edge - 0.03) < 1e-9)
check("NO low edge -> skip", no_sig.action == "skip" and no_sig.reason == "low_edge")

sigs = evaluate_market(mkt, 0.52, yes_book, no_book, now, cfg)
check("0% edge -> no trade", all(s.action == "skip" for s in sigs))

wide = OrderBook("YT", now, bids=[(0.40, 100)], asks=[(0.52, 100)])
sigs = evaluate_market(mkt, 0.60, wide, no_book, now, cfg)
check("wide spread blocked", next(s for s in sigs if s.label == "YES").reason == "wide_spread")

sigs = evaluate_market(mkt, 0.56, yes_book, no_book, mkt.end_ts - 60, cfg)
check("expiry cutoff blocked", all(s.reason == "expiry_cutoff" for s in sigs))

mp = maker_price(0.56, 0.50, 0.52, cfg)
check("maker price improves bid, stays under ask", mp == 0.51)
check("maker never crosses cap", maker_price(0.52, 0.50, 0.52, cfg) == 0.48)

# --- settlement helpers (no DB) -------------------------------------------------
m_settle = Market("0xset", "Will Bitcoin be above $100,000 today?", "slug", "BTC",
                  100000.0, "above", now + 3600, "YS", "NS")
check("outcome above strike", outcome_from_spot(m_settle, 100500.0) == 1.0)
check("outcome below strike", outcome_from_spot(m_settle, 99500.0) == 0.0)
check("resolve prefers recorded outcome", resolve_outcome(
    Market("0x", "", "", "BTC", 100000.0, "above", now, "YS", "NS", outcome=0.0),
    100500.0) == 0.0)

# --- zero-fill market cooldown (no DB) -------------------------------------------
cfg_zf = load_settings()
cfg_zf.zero_fill_cancel_limit = 2
cfg_zf.zero_fill_cooldown_s = 600.0
cfg_zf.order_cooldown_s = 0.0
cfg_zf.market_cooldown_s = 0.0
hub_zf = DataHub()
eng_zf = StrategyEngine(cfg_zf, hub_zf, Sink(mode="backtest"), RiskEngine(cfg_zf), mode="backtest")
m_zf = Market("0xzf", "Will Bitcoin be above $100,000 today?", "slug", "BTC",
              100000.0, "above", now + 3600, "YZF", "NZF")
hub_zf.update_market(m_zf)
hub_zf.set_spot("BTC", now, 100500.0)
for i, c in enumerate(closes):
    hub_zf.add_candle(Candle("BTC", now - (len(closes) - i) * 60, c, c, c, c, 1.0))
hub_zf.set_book(OrderBook("YZF", now, bids=[(0.50, 100)], asks=[(0.52, 100)]))
hub_zf.set_book(OrderBook("NZF", now, bids=[(0.46, 100)], asks=[(0.48, 100)]))
eng_zf.evaluate(now)
ord_zf = eng_zf.broker.order_for_token("YZF")
check("zero-fill test order placed", ord_zf is not None)
if ord_zf:
    eng_zf._cancel(ord_zf, now + 1, "edge_dropped")
    eng_zf.evaluate(now + 2)
    ord_zf = eng_zf.broker.order_for_token("YZF")
    check("second order after one cancel", ord_zf is not None)
    if ord_zf:
        eng_zf._cancel(ord_zf, now + 3, "edge_dropped")
        check("zero-fill ban set", eng_zf._zero_fill_ban.get("0xzf", 0) > now)
        eng_zf.evaluate(now + 4)
        check("banned market gets no new order", eng_zf.broker.order_for_token("YZF") is None)

hub_zf2 = DataHub()
eng_zf2 = StrategyEngine(cfg_zf, hub_zf2, Sink(mode="backtest"), RiskEngine(cfg_zf), mode="backtest")
hub_zf2.update_market(m_zf)
hub_zf2.set_spot("BTC", now, 100500.0)
for i, c in enumerate(closes):
    hub_zf2.add_candle(Candle("BTC", now - (len(closes) - i) * 60, c, c, c, c, 1.0))
hub_zf2.set_book(OrderBook("YZF", now, bids=[(0.50, 100)], asks=[(0.52, 100)]))
eng_zf2.evaluate(now)
ord_rep = eng_zf2.broker.order_for_token("YZF")
if ord_rep:
    eng_zf2._cancel(ord_rep, now + 1, "replace")
    check("replace does not increment zero-fill streak",
          eng_zf2._zero_fill_count.get("0xzf", 0) == 0)

# --- paper engine end-to-end (synthetic, needs PostgreSQL) --------------------
conn = connect_sync(cfg.database_url)
for _table in ("signals", "paper_orders", "paper_fills", "paper_settlements"):
    conn.execute(f"DELETE FROM {_table} WHERE mode='backtest'")
conn.commit()
hub = DataHub()
sink = Sink(mode="backtest")
risk = RiskEngine(cfg)
engine = StrategyEngine(cfg, hub, sink, risk, mode="backtest")

hub.update_market(mkt)
hub.set_spot("BTC", now, 100500.0)
for i, c in enumerate(closes):
    hub.add_candle(Candle("BTC", now - (len(closes) - i) * 60, c, c, c, c, 1.0))
hub.set_book(yes_book)
hub.set_book(no_book)

engine.evaluate(now)
order = engine.broker.order_for_token("YT")
check("order placed when edge exists", order is not None)
if order:
    check("maker price below ask", order.price < 0.52)
    exp_size = round(cfg.bankroll * cfg.max_trade_frac / order.price, 2)
    check("size = bankroll * 1%", abs(order.size - exp_size) < 0.01)

    # trade prints at our price -> conservative queue: NOT filled
    engine.on_trade(TradeTick("YT", now + 1, order.price, 50, "SELL"), now + 1)
    check("trade AT our price does not fill (queue)", order.filled == 0)
    # trade through our price -> filled, capped at min(remaining, trade size)
    engine.on_trade(TradeTick("YT", now + 2, order.price - 0.01, 30, "SELL"), now + 2)
    expected_fill = min(order.size, 30)
    check("trade through price fills (capped)", abs(order.filled - expected_fill) < 1e-9)
    check("position opened", engine.positions.has_position("YT"))

    # no averaging: with a position, evaluate must not place another YT order
    engine.broker.remove(order)
    engine.evaluate(now + 3)
    check("no averaging down", engine.broker.order_for_token("YT") is None)

    # settle YES=1
    pos = engine.positions.positions["YT"]
    engine.settle_market(mkt, 1.0, now + 10)
    check("settled with profit", risk.today_pnl(now + 10) > 0)

# --- risk engine -----------------------------------------------------------------
r2 = RiskEngine(cfg)
ok, _ = r2.check_entry(now, 0.0, r2.trade_size_usd())
check("entry allowed initially", ok)
ok, reason = r2.check_entry(now, cfg.bankroll * cfg.max_market_exposure_frac, r2.trade_size_usd())
check("market exposure cap", not ok and reason == "market_exposure_limit")
r2.on_settlement(-cfg.bankroll * cfg.daily_loss_frac - 1, now)
ok, reason = r2.check_entry(now, 0, 1)
check("daily loss halt", not ok and reason == "daily_loss_limit")
r3 = RiskEngine(cfg)
for _ in range(3):
    r3.on_settlement(-0.5, now)
ok, reason = r3.check_entry(now, 0, 1)
check("3 consecutive losses halt", not ok and reason == "consecutive_losses")

# --- test 5: parser false positives (must all be rejected) -------------------------
# These are hit/reach/touch, up-or-down, range and extremum markets: the bot's
# terminal price model cannot price them -> reason = unsupported_market_type (None).
for q in [
    "Will Bitcoin hit $110k by June 11?",
    "Will Bitcoin reach $110k?",
    "Bitcoin Up or Down on June 11?",
    "Bitcoin between $100k and $110k?",
    "Bitcoin highest price above $110k?",
    "Bitcoin touches $110k?",
]:
    check(f"reject unsupported_market_type: {q!r}", parse_question(q) is None)

# --- test 6: YES/NO token mapping ----------------------------------------------------
raw_gamma = {
    "conditionId": "0xmap",
    "question": "Will Bitcoin be above $110,000 on June 12?",
    "slug": "btc-110k",
    "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["TOK_YES", "TOK_NO"]',
    "endDate": "2026-06-12T12:00:00Z",
    "active": True,
    "closed": False,
}
gm = parse_gamma_market(raw_gamma)
check("gamma market parsed", gm is not None)
check("YES token = outcomes[0] token", gm is not None and gm.yes_token_id == "TOK_YES")
check("NO token = outcomes[1] token", gm is not None and gm.no_token_id == "TOK_NO")
check("reversed outcome order rejected",
      parse_gamma_market(dict(raw_gamma, outcomes='["No", "Yes"]')) is None)

m6 = Market("0xmap", "Will Bitcoin be above $110,000 on June 12?", "btc-110k", "BTC",
            110000.0, "above", now + 3600, "TOK_YES", "TOK_NO")
yb6 = OrderBook("TOK_YES", now, bids=[(0.50, 100)], asks=[(0.52, 100)])
nb6 = OrderBook("TOK_NO", now, bids=[(0.46, 100)], asks=[(0.48, 100)])
sigs6 = evaluate_market(m6, 0.56, yb6, nb6, now, cfg)
ys = next(s for s in sigs6 if s.label == "YES")
ns = next(s for s in sigs6 if s.label == "NO")
check("YES signal bound to YES token", ys.token_id == "TOK_YES")
check("YES signal quotes YES book", ys.best_bid == 0.50 and ys.best_ask == 0.52)
check("NO signal bound to NO token", ns.token_id == "TOK_NO")
check("NO signal quotes NO book", ns.best_bid == 0.46 and ns.best_ask == 0.48)
check("fair_yes on YES side", abs(ys.fair - 0.56) < 1e-9)
check("fair_no = 1 - fair_yes", abs(ns.fair - 0.44) < 1e-9)
check("YES edge measured vs yes_ask", abs(ys.edge - (0.56 - 0.52 - cfg.cost)) < 1e-9)
check("NO edge measured vs no_ask", abs(ns.edge - (0.44 - 0.48 - cfg.cost)) < 1e-9)

pm6 = PositionManager()
pm6.on_fill("0xmap", "TOK_YES", "YES", 0.50, 10)
pm6.on_fill("0xmap", "TOK_NO", "NO", 0.40, 10)
st6 = {s.token_id: s for s in pm6.settle_market(m6, 1.0)}
check("YES settles to outcome (payout 1)", st6["TOK_YES"].payout == 1.0
      and abs(st6["TOK_YES"].pnl - 5.0) < 1e-9)
check("NO settles to 1-outcome (payout 0)", st6["TOK_NO"].payout == 0.0
      and abs(st6["TOK_NO"].pnl - (-4.0)) < 1e-9)


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


pe_log = logging.getLogger("app.paper.paper_engine")
cap = _LogCapture()
pe_log.addHandler(cap)
pe_log.setLevel(logging.INFO)

hub6 = DataHub()
sink6 = Sink(mode="backtest")
eng6 = StrategyEngine(cfg, hub6, sink6, RiskEngine(cfg), mode="backtest")
m6b = Market("0xmaplog", "Will Bitcoin be above $100,000 today?", "slug", "BTC",
             100000.0, "above", now + 3600, "TOK_YES2", "TOK_NO2")
hub6.update_market(m6b)
hub6.set_spot("BTC", now, 100500.0)
for i, c in enumerate(closes):
    hub6.add_candle(Candle("BTC", now - (len(closes) - i) * 60, c, c, c, c, 1.0))
hub6.set_book(OrderBook("TOK_YES2", now, bids=[(0.50, 100)], asks=[(0.52, 100)]))
hub6.set_book(OrderBook("TOK_NO2", now, bids=[(0.46, 100)], asks=[(0.48, 100)]))
eng6.evaluate(now)
pe_log.removeHandler(cap)

order_log = next((m for m in cap.msgs if "PAPER ORDER" in m), "")
check("order placed for mapping log test", order_log != "")
for fieldname in ["market_question=", "condition_id=0xmaplog", "yes_token_id=TOK_YES2",
                  "no_token_id=TOK_NO2", "yes_best_bid=0.5", "yes_best_ask=0.52",
                  "no_best_bid=0.46", "no_best_ask=0.48", "fair_yes=", "fair_no=",
                  "selected_side=YES"]:
    check(f"mapping log has {fieldname}", fieldname in order_log)

# --- test 7: duplicate order prevention -----------------------------------------------
hub7 = DataHub()
sink7 = Sink(mode="backtest")
eng7 = StrategyEngine(cfg, hub7, sink7, RiskEngine(cfg), mode="backtest")
m7 = Market("0xdup", "Will Bitcoin be above $100,000 today?", "slug", "BTC",
            100000.0, "above", now + 3600, "YT7", "NT7")
hub7.update_market(m7)
hub7.set_spot("BTC", now, 100500.0)
for i, c in enumerate(closes):
    hub7.add_candle(Candle("BTC", now - (len(closes) - i) * 60, c, c, c, c, 1.0))
hub7.set_book(OrderBook("YT7", now, bids=[(0.50, 100)], asks=[(0.52, 100)]))
hub7.set_book(OrderBook("NT7", now, bids=[(0.46, 100)], asks=[(0.48, 100)]))

# edge stays constant -> repeated evaluation must keep exactly 1 open order
eng7.evaluate(now)
first = eng7.broker.order_for_token("YT7")
check("first order created", first is not None)
eng7.evaluate(now + 5)
eng7.evaluate(now + 10)
check("still exactly 1 open order", len(eng7.broker.orders) == 1)
check("same order kept (no duplicate)",
      eng7.broker.order_for_token("YT7") is first)
check("only 1 order counted", eng7.stats["orders"] == 1)

# book moves -> requote replaces the order, still exactly 1 open
hub7.set_book(OrderBook("YT7", now + 15, bids=[(0.49, 100)], asks=[(0.52, 100)]))
eng7.evaluate(now + 15)
check("requote keeps single open order", len(eng7.broker.orders) == 1)

# fill -> position exists -> further entries rejected (no averaging)
order7 = eng7.broker.order_for_token("YT7")
eng7.on_trade(TradeTick("YT7", now + 16, order7.price - 0.01, order7.size, "SELL"), now + 16)
check("position opened after fill", eng7.positions.has_position("YT7"))
eng7.evaluate(now + 20)
check("no new order with open position", eng7.broker.order_for_token("YT7") is None)

# exposure limit: pre-existing exposure in the same market blocks a new entry
m7b = Market("0xexp", "Will Bitcoin be above $100,000 today?", "slug", "BTC",
             100000.0, "above", now + 3600, "YT7B", "NT7B")
hub7.update_market(m7b)
hub7.set_book(OrderBook("YT7B", now + 25, bids=[(0.50, 100)], asks=[(0.52, 100)]))
hub7.set_book(OrderBook("NT7B", now + 25, bids=[(0.46, 100)], asks=[(0.48, 100)]))
near_cap = cfg.bankroll * cfg.max_market_exposure_frac - cfg.bankroll * cfg.max_trade_frac / 2
eng7.positions.on_fill("0xexp", "NT7B", "NO", 0.5, near_cap / 0.5)
eng7.evaluate(now + 25)
check("exposure limit blocks entry", eng7.broker.order_for_token("YT7B") is None)

n7 = sink7.flush_sync(conn)
check("test7 sink flushed", n7 > 0)
for reason, cid in [("existing_open_order", "0xdup"),
                    ("existing_position", "0xdup"),
                    ("risk:market_exposure_limit", "0xexp")]:
    row = conn.execute(
        "SELECT COUNT(*) n FROM signals WHERE condition_id=%s AND action='skip' AND reason=%s",
        (cid, reason)).fetchone()
    check(f"skip reason persisted: {reason}", row["n"] >= 1)

# --- sink flush round-trip -----------------------------------------------------------
n = sink.flush_sync(conn)
check("sink flushed rows", n > 0)
row = conn.execute("SELECT COUNT(*) n FROM paper_settlements WHERE mode='backtest'").fetchone()
check("settlement persisted", row["n"] >= 1)
conn.close()

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
