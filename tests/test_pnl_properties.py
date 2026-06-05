"""Property-based tests (Hypothesis) for the PnL accounting.

The original unpriced-fill bugs silently violated a single accounting identity, so we
assert it directly over thousands of generated fill sequences instead of a handful of
hand-picked examples. The cost-basis identity holds after EVERY fill:

    realized - net * avg == cash_flow

where cash_flow sums +qty*price on sells (cash in) and -qty*price on buys (cash out).
Any sign error, weighted-average slip, or cross-zero-reversal bug breaks it.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.db import session_scope
from app.executor import _apply_fill_to_position, _fill_math
from app.models import Alert, Order, Position, StrategyPosition

# A fill = (is_buy, qty, price). Bounded so accumulated float error stays well under
# a cent while logic bugs (proportional to trade value) stand out clearly.
_fill = st.tuples(
    st.booleans(),
    st.floats(min_value=0.001, max_value=100, allow_nan=False, allow_infinity=False),
    st.floats(min_value=1.0, max_value=10_000, allow_nan=False, allow_infinity=False),
)
_fills = st.lists(_fill, min_size=0, max_size=20)


@given(_fills)
@settings(max_examples=400)
def test_fillmath_cost_basis_identity(fills):
    """realized - net*avg == cash_flow after every fill (pure _fill_math)."""
    net = avg = realized = cash = 0.0
    for is_buy, qty, price in fills:
        signed = qty if is_buy else -qty
        net, avg, rd = _fill_math(net, avg, signed, qty, price)
        realized += rd
        cash += (-qty * price) if is_buy else (qty * price)
        assert realized - net * avg == pytest.approx(cash, rel=1e-6, abs=1e-2)


@given(_fills)
@settings(max_examples=300)
def test_fillmath_net_running_sum_and_no_phantom_realized(fills):
    """net is exactly old_net+signed; opening/increasing never realizes; a live
    position always carries a positive entry."""
    net = avg = 0.0
    for is_buy, qty, price in fills:
        signed = qty if is_buy else -qty
        opening_or_increasing = (abs(net) < 1e-9) or ((net > 0) == (signed > 0))
        prev = net
        net, avg, rd = _fill_math(net, avg, signed, qty, price)
        assert net == pytest.approx(prev + signed, abs=1e-9)
        if opening_or_increasing:
            assert rd == 0.0
        if abs(net) > 1e-9:
            assert avg > 0


@given(_fills)
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_live_ledger_cost_basis_identity(fills):
    """The SAME identity through the real ledger path (_apply_fill_to_position) — not
    just the pure function — so a wiring/persistence regression is caught too."""
    with session_scope() as db:
        db.query(StrategyPosition).delete()
        db.query(Position).delete()
        db.query(Order).delete()
        db.query(Alert).delete()
    cash = 0.0
    for is_buy, qty, price in fills:
        side = "buy" if is_buy else "sell"
        with session_scope() as db:
            _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", side, qty, price)
        cash += (-qty * price) if is_buy else (qty * price)
    with session_scope() as db:
        sp = db.query(StrategyPosition).filter_by(strategy_id="S1").one_or_none()
        net = sp.net_qty_base if sp else 0.0
        avg = sp.avg_entry_price if sp else 0.0
        realized = sp.realized_pnl if sp else 0.0
    assert realized - net * avg == pytest.approx(cash, rel=1e-6, abs=1e-2)
