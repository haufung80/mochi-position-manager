"""Portfolio manager: managed sizing decisions for sar=false strategies.

Uses the recording `stub_exchange` (provides get_price/get_step_size) and seeds
the StrategyPosition ledger via `_apply_fill_to_position`.
"""
from __future__ import annotations
import pytest

from app import portfolio
from app.portfolio import Decision, compute_managed_qty
from app.db import session_scope
from app.executor import _apply_fill_to_position
from app.routing import StrategyRouter


# ---------- compute_managed_qty ----------

def test_compute_managed_qty_rounds_down_to_step():
    assert compute_managed_qty(1000, 50000, 0.001) == pytest.approx(0.02)
    # 1075/50000 = 0.0215 -> floor to 0.021 on a 0.001 grid
    assert compute_managed_qty(1075, 50000, 0.001) == pytest.approx(0.021)
    # Decimal-exact even on the nasty 0.1 grid: 1000/0.5 = 2000 units
    assert compute_managed_qty(1000, 0.5, 0.1) == pytest.approx(2000.0)
    # budget below one step -> 0
    assert compute_managed_qty(10, 50000, 0.001) == 0.0
    # guards
    assert compute_managed_qty(1000, 0, 0.001) == 0.0
    assert compute_managed_qty(0, 50000, 0.001) == 0.0
    assert compute_managed_qty(1000, 50000, 0) == 0.0


# ---------- decide ----------

def _route(strategies_yaml, sid):
    return StrategyRouter(strategies_yaml).get(sid)


def _venue(route, exchange="bybit"):
    return next(v for v in route.venues if v.exchange == exchange)


def test_decide_sar_is_alert_driven(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_SAR")
    with session_scope() as db:
        s = portfolio.decide(db, route, _venue(route), "buy", alert_quantity=0.05)
    assert s.decision is Decision.ALERT_DRIVEN
    assert s.qty == 0.05


def test_decide_flat_managed_opens_sized(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")     # position_size 1000, XRP
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 0.5                 # XRP @ $0.50
    stub_exchange.step_sizes[v.symbol] = 0.1
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.OPEN
    assert s.paper is False
    assert s.qty == pytest.approx(2000.0)               # 1000 / 0.5


def test_decide_flat_no_size_is_paper(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_BTC")         # no position_size
    v = _venue(route)
    stub_exchange.step_sizes[v.symbol] = 0.001
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.OPEN
    assert s.paper is True
    assert s.qty == 0.001                               # one min unit


def test_decide_double_down_long_rejected(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    with session_scope() as db:
        _apply_fill_to_position(db, route.strategy_id, v.exchange, v.symbol, "buy", 100.0, 0.5)
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)   # long + buy
    assert s.decision is Decision.REJECT
    assert "double-down" in s.reason


def test_decide_long_then_sell_closes(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    with session_scope() as db:
        _apply_fill_to_position(db, route.strategy_id, v.exchange, v.symbol, "buy", 100.0, 0.5)
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "sell", alert_quantity=None)  # long + sell
    assert s.decision is Decision.CLOSE
    assert s.side == "sell"
    assert s.qty == pytest.approx(100.0)               # abs(net)


def test_decide_short_then_buy_closes(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    with session_scope() as db:
        _apply_fill_to_position(db, route.strategy_id, v.exchange, v.symbol, "sell", 50.0, 0.5)
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)   # short + buy
    assert s.decision is Decision.CLOSE
    assert s.qty == pytest.approx(50.0)


def test_decide_short_then_sell_rejected(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    with session_scope() as db:
        _apply_fill_to_position(db, route.strategy_id, v.exchange, v.symbol, "sell", 50.0, 0.5)
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "sell", alert_quantity=None)  # short + sell
    assert s.decision is Decision.REJECT


def test_decide_unsized_when_budget_below_step(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")     # position_size 1000
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 50000.0
    stub_exchange.step_sizes[v.symbol] = 1.0            # 1 whole unit step
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.REJECT                # 1000/50000 = 0.02 < 1 step


def test_decide_price_unavailable_rejects(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 0.0               # price lookup "failed"
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.REJECT
    assert "price" in s.reason


def test_decide_close_does_not_need_price(strategies_yaml, stub_exchange):
    """A close is sized from the ledger net, so a price outage can't block it."""
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 0.0               # price unavailable
    with session_scope() as db:
        _apply_fill_to_position(db, route.strategy_id, v.exchange, v.symbol, "buy", 7.0, 0.5)
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "sell", alert_quantity=None)
    assert s.decision is Decision.CLOSE
    assert s.qty == pytest.approx(7.0)
