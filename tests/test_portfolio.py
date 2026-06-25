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
from app.routing import StrategyRoute, StrategyRouter, VenueRoute


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


def _set_risk(*, per_order_max_notional=None, kill_switch=None):
    """Set the global risk gate for a decide test."""
    from app.risk import update_risk_settings
    with session_scope() as db:
        update_risk_settings(db, per_order_max_notional=per_order_max_notional,
                             kill_switch=kill_switch)


def test_decide_sar_is_alert_driven(strategies_yaml, stub_exchange):
    _set_risk(per_order_max_notional=0.0)               # isolate from the risk gate
    route = _route(strategies_yaml, "TEST_SAR")
    with session_scope() as db:
        s = portfolio.decide(db, route, _venue(route), "buy", alert_quantity=0.05)
    assert s.decision is Decision.ALERT_DRIVEN
    assert s.qty == 0.05


def test_decide_flat_managed_opens_sized(strategies_yaml, stub_exchange):
    _set_risk(per_order_max_notional=0.0)               # isolate from the risk gate
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


# ---------- exchange minimum order value ----------

def test_min_qty_rounds_up_to_meet_notional():
    from app.portfolio import _min_qty
    assert _min_qty(10, 100000, 0.00001) == pytest.approx(0.0001)   # $10 / 100k -> 0.0001 BTC
    assert _min_qty(0, 100000, 0.001) == 0.0


def test_decide_paper_meets_min_notional(strategies_yaml, stub_exchange):
    """Paper bumps the one-step order up to the exchange min notional (a lone HL
    BTC step is ~$1, below the $10 minimum)."""
    route = _route(strategies_yaml, "TEST_BTC")        # managed, no position_size -> paper
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 100000.0
    stub_exchange.step_sizes[v.symbol] = 0.00001
    stub_exchange.min_notionals[v.symbol] = 10.0
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.OPEN and s.paper
    assert s.qty == pytest.approx(0.0001)              # 10 / 100000, rounded up to step
    assert s.qty * 100000 >= 10.0


def test_decide_managed_below_min_notional_rejected(stub_exchange):
    """position_size below the exchange minimum order value -> reject (don't place
    an order the exchange will bounce)."""
    route = StrategyRoute(strategy_id="SMALL", base_asset="BTC",
                          venues=(VenueRoute("bybit", "BTCUSDT", True),),
                          position_size=8.0)
    v = route.venues[0]
    stub_exchange.prices["BTCUSDT"] = 100000.0
    stub_exchange.step_sizes["BTCUSDT"] = 0.00001
    stub_exchange.min_notionals["BTCUSDT"] = 10.0
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.REJECT
    assert "minimum" in s.reason


# ---------- pre-trade risk gate (RiskSettings) ----------

def test_risk_settings_defaults_and_update():
    """A fresh deploy's singleton defaults to the $500 per-order cap + kill-switch off
    (the MODEL default; the test fixture zeroes the live row), and update patches it."""
    from app.models import RiskSettings
    from app.risk import get_risk_settings, update_risk_settings
    assert RiskSettings.__table__.c.per_order_max_notional.default.arg == pytest.approx(500.0)
    assert RiskSettings.__table__.c.kill_switch.default.arg is False
    with session_scope() as db:
        update_risk_settings(db, per_order_max_notional=250.0, kill_switch=True)
    with session_scope() as db:
        rs = get_risk_settings(db)
        assert rs.per_order_max_notional == pytest.approx(250.0) and rs.kill_switch is True


def test_decide_kill_switch_rejects_all(strategies_yaml, stub_exchange):
    _set_risk(kill_switch=True)
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 0.5
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.REJECT and "kill-switch" in s.reason.lower()


def test_decide_per_order_cap_rejects_oversized_managed_open(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_MANAGED")     # position_size 1000
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 0.5                 # 1000/0.5 -> 2000 qty = $1000 notional
    stub_exchange.step_sizes[v.symbol] = 0.1
    _set_risk(per_order_max_notional=500.0)
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.REJECT and "per-order cap" in s.reason
    _set_risk(per_order_max_notional=2000.0)             # raise the cap -> now opens
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=None)
    assert s.decision is Decision.OPEN and s.qty == pytest.approx(2000.0)


def test_decide_per_order_cap_rejects_oversized_sar(strategies_yaml, stub_exchange):
    route = _route(strategies_yaml, "TEST_SAR")
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 50000.0            # 0.05 * 50000 = $2500 > $500
    _set_risk(per_order_max_notional=500.0)
    with session_scope() as db:
        s = portfolio.decide(db, route, v, "buy", alert_quantity=0.05)
    assert s.decision is Decision.REJECT and "per-order cap" in s.reason


def test_decide_close_is_exempt_from_per_order_cap(strategies_yaml, stub_exchange):
    """A CLOSE (opposite signal) is NEVER blocked by the cap — you must be able to exit a
    position larger than the per-order cap."""
    route = _route(strategies_yaml, "TEST_MANAGED")
    v = _venue(route)
    stub_exchange.prices[v.symbol] = 0.5
    with session_scope() as db:                          # seed a long 2000 ($1000) position
        _apply_fill_to_position(db, route.strategy_id, v.exchange, v.symbol, "buy", 2000.0, 0.5)
    _set_risk(per_order_max_notional=500.0)
    with session_scope() as db:                          # opposite signal -> close to flat
        s = portfolio.decide(db, route, v, "sell", alert_quantity=None)
    assert s.decision is Decision.CLOSE and s.qty == pytest.approx(2000.0)
