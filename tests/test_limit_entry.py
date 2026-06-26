"""P1 — limit-entry placement + the fill-poller (limit_worker).

Covers: `entry` config parsing, the deterministic client-order-id, that a managed OPEN
on an `entry: limit` strategy rests a working limit (no ledger move) while a CLOSE stays
market, and the poller booking partial/full fills to the ledger (no double-count) and the
terminal cancel transition.
"""
from __future__ import annotations

import pytest

from app.db import session_scope
from app.executor import execute_order, make_client_order_id
from app.limit_worker import _poll_working_orders
from app.models import Alert, Order, Position, StrategyPosition
from app.routing import StrategyRoute, StrategyRouter, VenueRoute
from app.routes.webhook import _dispatch_venue


def _mk_alert(key: str, action: str, price: float, strategy: str) -> int:
    with session_scope() as db:
        a = Alert(idempotency_key=key, strategy_id=strategy, action=action,
                  raw_payload="{}", signal_price=price, source_ip="")
        db.add(a)
        db.flush()
        return a.id


_BYBIT = VenueRoute(exchange="bybit", symbol="SOLUSDT", enabled=True)


# --- config parsing ----------------------------------------------------------

def test_entry_config_parses(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        "strategies:\n"
        "  A: {base_asset: SOL, position_size: 100, entry: limit, venues: {bybit: true}}\n"
        "  B: {base_asset: SOL, position_size: 100, venues: {bybit: true}}\n"
        "  C: {base_asset: SOL, position_size: 100, entry: bogus, venues: {bybit: true}}\n"
    )
    r = StrategyRouter(p)
    assert r.get("A").entry == "limit"
    assert r.get("B").entry == "market"      # default
    assert r.get("C").entry == "market"      # unknown value -> safe default


def test_make_client_order_id_format():
    cid = make_client_order_id(123, "bybit", "SOLUSDT")
    assert cid.startswith("0x") and len(cid) == 34      # valid Bybit orderLinkId AND HL cloid
    assert make_client_order_id(123, "bybit", "SOLUSDT") == cid          # deterministic
    assert make_client_order_id(124, "bybit", "SOLUSDT") != cid          # varies per alert
    assert make_client_order_id(123, "hyperliquid", "SOL") != cid        # varies per venue


# --- placement: OPEN rests a limit; no ledger move ---------------------------

def test_limit_open_places_working_no_ledger(stub_exchange, silent_notifier):
    aid = _mk_alert("k-open", "buy", 69.8, "LIM")
    cloid = make_client_order_id(aid, "bybit", "SOLUSDT")
    with session_scope() as db:
        alert = db.get(Alert, aid)
        order = execute_order(db, alert, _BYBIT, quantity=2.0, order_type="limit",
                              limit_price=69.8, client_order_id=cloid)
        assert order.order_type == "limit" and order.status == "working"
        assert order.limit_price == 69.8 and order.exchange_order_id == cloid
        assert order.qty_base_filled == 0.0
    # a LIMIT was placed, not a market order
    assert any(c[0] == "limit" for c in stub_exchange.calls)
    assert not any(c[0] == "market" for c in stub_exchange.calls)
    # no ledger movement at placement time
    with session_scope() as db:
        assert db.query(Position).count() == 0
        assert db.query(StrategyPosition).count() == 0


# --- dispatch: managed OPEN -> limit; managed CLOSE -> market ----------------

def test_dispatch_limit_open_is_limit(stub_exchange, silent_notifier):
    stub_exchange.prices["SOLUSDT"] = 69.8
    aid = _mk_alert("d-open", "buy", 69.8, "LIMO")
    route = StrategyRoute("LIMO", "SOL", (_BYBIT,), sar=False, position_size=140.0, entry="limit")
    with session_scope() as db:
        _dispatch_venue(db, db.get(Alert, aid), route, _BYBIT, 0.0)
    with session_scope() as db:
        o = db.query(Order).one()
        assert o.order_type == "limit" and o.status == "working" and o.limit_price == 69.8
        assert db.query(Position).count() == 0           # nothing booked yet


def test_dispatch_limit_close_is_market(stub_exchange, silent_notifier):
    stub_exchange.prices["SOLUSDT"] = 69.8
    # seed a long position so a sell is a CLOSE
    with session_scope() as db:
        db.add(StrategyPosition(strategy_id="LIMC", exchange="bybit", symbol="SOLUSDT",
                                net_qty_base=2.0, net_qty_usd=140.0, last_price=70.0,
                                avg_entry_price=70.0, realized_pnl=0.0))
    aid = _mk_alert("d-close", "sell", 69.8, "LIMC")
    route = StrategyRoute("LIMC", "SOL", (_BYBIT,), sar=False, position_size=140.0, entry="limit")
    with session_scope() as db:
        _dispatch_venue(db, db.get(Alert, aid), route, _BYBIT, 0.0)
    with session_scope() as db:
        o = db.query(Order).one()
        assert o.order_type == "market" and o.status == "success"   # CLOSE stays market


# --- the poller: book partial -> full, no double-count, cancel --------------

def _place_working(strategy: str, key: str, qty: float = 2.0, price: float = 69.8) -> str:
    aid = _mk_alert(key, "buy", price, strategy)
    cloid = make_client_order_id(aid, "bybit", "SOLUSDT")
    with session_scope() as db:
        execute_order(db, db.get(Alert, aid), _BYBIT, quantity=qty, order_type="limit",
                      limit_price=price, client_order_id=cloid)
    return cloid


def _net(symbol="SOLUSDT", exchange="bybit") -> float:
    with session_scope() as db:
        pos = db.query(Position).filter_by(exchange=exchange, symbol=symbol).one_or_none()
        return pos.net_qty_base if pos else 0.0


def test_poller_books_partial_then_full(stub_exchange, silent_notifier):
    cloid = _place_working("LIM", "p1")
    # partial fill
    stub_exchange.limit_orders[cloid].update(filled=1.0, avg=69.8, state="partially_filled")
    _poll_working_orders()
    assert _net() == pytest.approx(1.0)
    with session_scope() as db:
        o = db.query(Order).one()
        assert o.status == "working" and o.qty_base_filled == pytest.approx(1.0)
    # full fill -> books the remaining delta, order completes
    stub_exchange.limit_orders[cloid].update(filled=2.0, avg=69.8, state="filled")
    _poll_working_orders()
    assert _net() == pytest.approx(2.0)
    with session_scope() as db:
        o = db.query(Order).one()
        assert o.status == "success" and o.qty_base_filled == pytest.approx(2.0)
        assert o.qty_base == pytest.approx(2.0) and o.fee_source == "exchange"


def test_poller_no_double_count(stub_exchange, silent_notifier):
    cloid = _place_working("LIM", "p2")
    stub_exchange.limit_orders[cloid].update(filled=1.0, avg=69.8, state="partially_filled")
    _poll_working_orders()
    _poll_working_orders()        # same cumulative -> second pass books nothing new
    assert _net() == pytest.approx(1.0)


def test_poller_cancel_after_partial_keeps_fill(stub_exchange, silent_notifier):
    cloid = _place_working("LIM", "p3")
    stub_exchange.limit_orders[cloid].update(filled=1.0, avg=69.8, state="partially_filled")
    _poll_working_orders()
    # cancelled with the partial still on the book
    stub_exchange.limit_orders[cloid].update(state="cancelled")
    _poll_working_orders()
    assert _net() == pytest.approx(1.0)         # the partial fill is retained
    with session_scope() as db:
        assert db.query(Order).one().status == "cancelled"


def test_poller_cancel_unfilled_no_position(stub_exchange, silent_notifier):
    cloid = _place_working("LIM", "p4")
    stub_exchange.limit_orders[cloid].update(state="cancelled")    # never filled
    _poll_working_orders()
    assert _net() == 0.0
    with session_scope() as db:
        assert db.query(Order).one().status == "cancelled"
