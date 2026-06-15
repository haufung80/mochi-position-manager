"""A.4 — arb executor (pair sizing + open/close orchestration) + the retry scan.

These run through the SYNCHRONOUS entrypoints (`size_pair`, `_run_open`,
`_run_close`, `execute_leg`, `retry_worker._run_due_retries`) exactly like the
directional workers are tested — no async machinery.

Isolation is the load-bearing property: every test that fires a leg also asserts
that NO directional row (`Position` / `StrategyPosition` / `Order`) was written
and that `_LEDGER_LOCK` was never taken.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta

import pytest

from app import arb_executor
from app.db import session_scope
from app.models import (
    Alert, ArbLeg, ArbOrder, ArbPosition, Order, Position, StrategyPosition,
)
from app.schemas import OrderResult


def _now():
    return datetime.now(timezone.utc)


def _mk_arb(asset="BTC", status="opening", notional=1000.0):
    with session_scope() as db:
        arb = ArbPosition(idempotency_key=f"k-{_now().timestamp()}", asset=asset,
                          notional_target=notional, status=status)
        db.add(arb)
        db.flush()
        return arb.id


def _add_leg(arb_id, *, exchange, account="arb", product, symbol, side,
             target_qty, filled_qty=0.0, status="pending"):
    with session_scope() as db:
        leg = ArbLeg(arb_id=arb_id, exchange=exchange, account=account,
                     product=product, symbol=symbol, side=side,
                     target_qty=target_qty, filled_qty=filled_qty, status=status)
        db.add(leg)
        db.flush()
        return leg.id


class _LegSnap:
    """Detached snapshot of an ArbLeg's fields (read inside the session so no lazy
    refresh fires after the scope closes)."""
    def __init__(self, lg: ArbLeg):
        self.product = lg.product
        self.status = lg.status
        self.filled_qty = lg.filled_qty
        self.target_qty = lg.target_qty
        self.side = lg.side
        self.error_message = lg.error_message


def _legs(arb_id):
    with session_scope() as db:
        return {lg.product: _LegSnap(lg) for lg in
                db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()}


def _assert_no_directional_rows():
    with session_scope() as db:
        assert db.query(Position).count() == 0, "arb wrote a Position row"
        assert db.query(StrategyPosition).count() == 0, "arb wrote a StrategyPosition row"
        assert db.query(Order).count() == 0, "arb wrote a directional Order row"


# --- pair sizing ------------------------------------------------------------

def test_size_pair_notional_equal_base_qty_across_two_grids(arb_registry):
    """notional sizing clamps to the COARSER grid so both legs hold the SAME qty,
    even when the two venues report different step sizes."""
    arb_registry.state.prices["BTCUSDT"] = 50000.0      # bybit spot ref
    arb_registry.state.prices["BTC"] = 50000.0          # hl perp ref
    arb_registry.state.spot_step_sizes["BTCUSDT"] = 0.001   # finer spot grid
    arb_registry.state.step_sizes["BTC"] = 0.01             # coarser perp grid
    arb_id = _mk_arb(notional=1000.0)
    _add_leg(arb_id, exchange="bybit", product="spot", symbol="BTCUSDT",
             side="buy", target_qty=0.0)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.0)
    with session_scope() as db:
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        qty = arb_executor.size_pair(legs, size_mode="notional", notional=1000.0)
    # coarse grid 0.01; 1000/50000 = 0.02 -> snapped to 0.02 (a multiple of 0.01).
    assert qty == pytest.approx(0.02)
    # multiple of the COARSER grid, so BOTH legs can hold it exactly.
    assert round(qty / 0.01, 9) == round(qty / 0.01)


def test_size_pair_min_mode_clears_each_leg_min(arb_registry):
    """min mode picks a qty that clears BOTH legs' own min-notional; equal qty."""
    arb_registry.state.prices["BTCUSDT"] = 50000.0
    arb_registry.state.prices["BTC"] = 50000.0
    arb_registry.state.spot_min_notionals["BTCUSDT"] = 10.0   # HL/Bybit-ish floor
    arb_registry.state.min_notionals["BTC"] = 0.0
    arb_registry.state.spot_step_sizes["BTCUSDT"] = 0.001
    arb_registry.state.step_sizes["BTC"] = 0.001
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="bybit", product="spot", symbol="BTCUSDT",
             side="buy", target_qty=0.0)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.0)
    with session_scope() as db:
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        qty = arb_executor.size_pair(legs, size_mode="min", notional=None)
    # min for spot: 10/50000 = 0.0002 -> snapped UP to 0.001 (one step). >= min.
    assert qty == pytest.approx(0.001)
    assert qty * 50000.0 >= 10.0          # clears the spot min-notional


@pytest.mark.parametrize("label, price, step", [
    ("BTC-like: high price, fine step", 50000.0, 0.001),
    ("HYPE-like: mid price", 59.0, 0.01),
    ("PURR-like: low price, whole units (the arb #1 failure)", 0.0879, 1.0),
    ("sub-$1 whole-unit coin", 0.5, 1.0),
    ("very-low price, fine step", 0.02, 0.1),
])
def test_size_pair_min_clears_floor_with_margin(arb_registry, label, price, step):
    """size_mode='min' must clear the $10 venue floor WITH MARGIN for ANY coin profile,
    never landing exactly on it — that's the arb #1 failure (114 PURR ≈ $10.02 → HL
    rejected on a downtick). This sweep guards future coin additions: drop the
    _MIN_NOTIONAL_BUFFER and the low-price/coarse-step rows fail here, not on the box."""
    arb_registry.state.prices["SPOT"] = price
    arb_registry.state.prices["PERP"] = price
    arb_registry.state.spot_min_notionals["SPOT"] = 10.0
    arb_registry.state.min_notionals["PERP"] = 10.0
    arb_registry.state.spot_step_sizes["SPOT"] = step
    arb_registry.state.step_sizes["PERP"] = step
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="SPOT",
             side="buy", target_qty=0.0)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="PERP",
             side="sell", target_qty=0.0)
    with session_scope() as db:
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        qty = arb_executor.size_pair(legs, size_mode="min", notional=None)
    assert qty * price >= 10.0 * 1.1, f"{label}: ${qty * price:.2f} too close to the $10 floor"


def test_snap_down_dust_tolerant():
    """_snap_down absorbs float dust at a step boundary (0.339999999999 -> 0.34) so it
    never drops a whole step, but still floors a genuine sub-step (0.335 -> 0.33)."""
    from app.arb_executor import _snap_down
    assert _snap_down(0.34, 0.01) == pytest.approx(0.34)
    assert _snap_down(0.33999999999999997, 0.01) == pytest.approx(0.34)   # dust -> snap up
    assert _snap_down(0.335, 0.01) == pytest.approx(0.33)                  # genuine -> floor
    assert _snap_down(1.0, 1.0) == pytest.approx(1.0)


def test_snap_nearest_rounds_to_closest_grid_point():
    """_snap_nearest picks the CLOSEST step multiple (residual <= step/2), so a hedge
    of a between-grid fill doesn't systematically under-shoot the way _snap_down does."""
    from app.arb_executor import _snap_nearest, _snap_down
    # The real BTC case: 0.00018987 net spot fill on a 0.00001 perp grid.
    assert _snap_nearest(0.0001898671, 0.00001) == pytest.approx(0.00019)   # up to nearest
    assert _snap_down(0.0001898671, 0.00001) == pytest.approx(0.00018)      # floor under-hedges
    assert _snap_nearest(0.0184, 0.001) == pytest.approx(0.018)             # rounds down when closer
    assert _snap_nearest(0.0185, 0.001) == pytest.approx(0.019)             # half rounds up
    assert _snap_nearest(0.0, 0.001) == 0.0


def test_size_pair_rejects_when_notional_below_a_leg_min(arb_registry):
    """If the sized qty can't clear ONE leg's min-notional, REJECT (don't shrink)."""
    arb_registry.state.prices["BTCUSDT"] = 50000.0
    arb_registry.state.prices["BTC"] = 50000.0
    arb_registry.state.spot_min_notionals["BTCUSDT"] = 5000.0   # high floor
    arb_registry.state.spot_step_sizes["BTCUSDT"] = 0.001
    arb_registry.state.step_sizes["BTC"] = 0.001
    arb_id = _mk_arb(notional=100.0)
    _add_leg(arb_id, exchange="bybit", product="spot", symbol="BTCUSDT",
             side="buy", target_qty=0.0)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.0)
    with session_scope() as db:
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        with pytest.raises(arb_executor.SizingError):
            arb_executor.size_pair(legs, size_mode="notional", notional=100.0)


# --- _run_open: both legs fill, isolation -----------------------------------

def test_run_open_fills_both_legs_no_directional_rows(arb_registry):
    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="P1", filled_qty_base=0.02, avg_price=50000.0)
    arb_registry.state.spot_result = OrderResult(
        success=True, exchange_order_id="S1", filled_qty_base=0.02, avg_price=50000.0)
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02)

    arb_executor._run_open(arb_id)

    legs = _legs(arb_id)
    assert legs["spot"].status == "success" and legs["spot"].filled_qty == 0.02
    assert legs["perp"].status == "success" and legs["perp"].filled_qty == 0.02
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        assert arb.status == "open" and arb.opened_at is not None
        # one ArbOrder per leg, on the 'arb' account.
        orders = db.query(ArbOrder).all()
        assert len(orders) == 2
        assert all(o.account == "arb" and o.status == "success" for o in orders)
    _assert_no_directional_rows()


def test_run_open_records_ref_price_for_slippage(arb_registry):
    """Each filled leg records the mid at order time (ref_price), so execution slippage
    (avg_fill vs ref) is reportable on the arb page."""
    arb_registry.state.prices["UBTC/USDC"] = 49990.0
    arb_registry.state.prices["BTC"] = 50000.0
    arb_registry.state.spot_result = OrderResult(
        success=True, exchange_order_id="S1", filled_qty_base=0.02, avg_price=49995.0)
    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="P1", filled_qty_base=0.02, avg_price=50000.0)
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02)
    arb_executor._run_open(arb_id)
    with session_scope() as db:                      # read ref_price off the persisted legs
        legs = {lg.product: lg for lg in db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id)}
        assert legs["spot"].status == "success" and legs["spot"].ref_price == pytest.approx(49990.0)
        assert legs["perp"].status == "success" and legs["perp"].ref_price == pytest.approx(50000.0)


def test_run_open_fires_spot_before_perp(arb_registry):
    """Thinner leg (spot) fires FIRST to minimize the naked window."""
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02)
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02)
    arb_executor._run_open(arb_id)
    order_calls = [c for c in arb_registry.state.calls
                   if c[0] in ("spot_market", "market")]
    assert order_calls[0][0] == "spot_market", "spot leg must fire first"


# --- partial-fill re-hedge --------------------------------------------------

def test_partial_fill_rehedges_leg2_to_leg1_actual_fill(arb_registry):
    """leg-1 (spot) target 0.5 but fills 0.4 -> leg-2 (perp) re-derived to 0.4."""
    arb_registry.state.spot_result = OrderResult(
        success=True, exchange_order_id="S", filled_qty_base=0.4, avg_price=50000.0)
    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="P", filled_qty_base=0.4, avg_price=50000.0)
    arb_registry.state.step_sizes["BTC"] = 0.001
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.5)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.5)

    arb_executor._run_open(arb_id)

    legs = _legs(arb_id)
    assert legs["spot"].filled_qty == 0.4
    # leg-2's target was re-derived from leg-1's ACTUAL fill (0.4), not 0.5.
    assert legs["perp"].target_qty == pytest.approx(0.4)
    # the perp market_order was called with the re-derived 0.4.
    perp_calls = [c for c in arb_registry.state.calls if c[0] == "market"]
    assert perp_calls[-1][3] == pytest.approx(0.4)
    assert legs["perp"].status == "success"
    with session_scope() as db:
        assert db.get(ArbPosition, arb_id).status == "open"


def test_hedge_snaps_to_nearest_grid_point_not_down(arb_registry):
    """A spot fill landing BETWEEN perp grid points hedges to the NEAREST point, not
    floored down. Reproduces the live BTC skew: 0.0001898671 net spot on a 0.00001
    perp grid -> short 0.00019 (residual −1.3e-7), NOT 0.00018 (residual +9.9e-6)."""
    arb_registry.state.spot_result = OrderResult(
        success=True, exchange_order_id="S", filled_qty_base=0.0001898671, avg_price=66292.0)
    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="P", filled_qty_base=0.00019, avg_price=66202.0)
    arb_registry.state.step_sizes["BTC"] = 0.00001
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.00019)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.00019)

    arb_executor._run_open(arb_id)

    legs = _legs(arb_id)
    assert legs["perp"].target_qty == pytest.approx(0.00019)   # nearest, not 0.00018
    perp_calls = [c for c in arb_registry.state.calls if c[0] == "market"]
    assert perp_calls[-1][3] == pytest.approx(0.00019)
    # residual is now within half a perp step (was ~a full step under snap-down).
    skew = legs["spot"].filled_qty - legs["perp"].filled_qty
    assert abs(skew) <= 0.00001 / 2


def test_unhedgeable_partial_fill_marks_error_not_open(arb_registry):
    """leg-1 fills a dust amount that can't clear leg-2's grid -> error + skew
    visible, NEVER silently open."""
    arb_registry.state.spot_result = OrderResult(
        success=True, exchange_order_id="S", filled_qty_base=0.0005, avg_price=50000.0)
    arb_registry.state.step_sizes["BTC"] = 0.001   # 0.0005 snaps to 0
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.5)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.5)

    arb_executor._run_open(arb_id)

    legs = _legs(arb_id)
    assert legs["perp"].status == "error"
    # the perp leg was NOT fired (no naked hedge).
    assert not [c for c in arb_registry.state.calls if c[0] == "market"]
    with session_scope() as db:
        assert db.get(ArbPosition, arb_id).status == "error"


# --- leg-1 failure: no naked hedge ------------------------------------------

def test_leg1_failure_does_not_place_naked_hedge(arb_registry):
    arb_registry.state.spot_result = OrderResult(success=False, error_message="boom")
    arb_id = _mk_arb()
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02)

    arb_executor._run_open(arb_id)

    legs = _legs(arb_id)
    assert legs["spot"].status == "retrying"     # first failure -> retrying
    assert legs["perp"].status == "error"        # hedge withheld
    assert not [c for c in arb_registry.state.calls if c[0] == "market"]
    with session_scope() as db:
        assert db.get(ArbPosition, arb_id).status == "error"
    _assert_no_directional_rows()


# --- failure -> retrying -> dead ladder -------------------------------------

def test_leg_failure_walks_retrying_then_dead(arb_registry, silent_notifier):
    arb_registry.state.next_result = OrderResult(success=False, error_message="nope")
    arb_id = _mk_arb()
    leg_id = _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
                      side="sell", target_qty=0.02)

    # Attempt 1: first failure -> retrying, next_retry_at set.
    with session_scope() as db:
        leg = db.get(ArbLeg, leg_id)
        order = arb_executor.execute_leg(db, leg)
        oid = order.id
    with session_scope() as db:
        o = db.get(ArbOrder, oid)
        assert o.status == "retrying" and o.attempts == 1 and o.next_retry_at is not None

    # Walk attempts up to the cap -> dead.
    from app.config import get_settings
    cap = get_settings().retry_max_attempts
    for _ in range(cap):
        with session_scope() as db:
            leg = db.get(ArbLeg, leg_id)
            order = db.get(ArbOrder, oid)
            arb_executor.execute_leg(db, leg, existing_order=order)
    with session_scope() as db:
        o = db.get(ArbOrder, oid)
        leg = db.get(ArbLeg, leg_id)
        assert o.status == "dead" and leg.status == "dead"
    _assert_no_directional_rows()


# --- execute_leg resolves the leg's OWN account -----------------------------

def test_execute_leg_uses_leg_account_not_default(arb_registry):
    arb_id = _mk_arb()
    leg_id = _add_leg(arb_id, exchange="hyperliquid", account="arb",
                      product="perp", symbol="BTC", side="sell", target_qty=0.02)
    with session_scope() as db:
        leg = db.get(ArbLeg, leg_id)
        arb_executor.execute_leg(db, leg)
    # the market call recorded the (name, account) it ran on -> ('hyperliquid','arb')
    market = [c for c in arb_registry.state.calls if c[0] == "market"][-1]
    assert market[5] == "hyperliquid" and market[6] == "arb"


# --- _run_close: perp close + spot sell clamped to balance ------------------

def test_run_close_perp_closes_and_spot_sells_clamped(arb_registry):
    arb_registry.state.spot_balances["BTC"] = 0.015   # less than held 0.02
    arb_id = _mk_arb(status="closing")
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02, filled_qty=0.02, status="success")
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02, filled_qty=0.02, status="success")

    arb_executor._run_close(arb_id)

    # perp closed via close_position; spot sold via spot_market_order SELL.
    assert any(c[0] == "close" and c[1] == "BTC" for c in arb_registry.state.calls)
    spot_sells = [c for c in arb_registry.state.calls
                  if c[0] == "spot_market" and c[2] == "sell"]
    assert spot_sells, "spot leg was not sold on close"
    # clamped to the live free balance (0.015), not the held 0.02.
    assert spot_sells[-1][3] == pytest.approx(0.015)
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        assert arb.status == "closed" and arb.closed_at is not None
    _assert_no_directional_rows()


def test_run_close_perp_failure_marks_arb_error(arb_registry, silent_notifier):
    """A failed perp close leaves the arb `error` (not silently `closed`)."""
    arb_id = _mk_arb(status="closing")
    leg_id = _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
                      side="sell", target_qty=0.02, filled_qty=0.02, status="success")
    # make close_position fail on the perp leg's fake instance.
    perp_fake = arb_registry.get("hyperliquid", "arb")
    perp_fake.close_position = lambda symbol: OrderResult(
        success=False, error_message="exchange down")

    arb_executor._run_close(arb_id)

    legs = _legs(arb_id)
    assert legs["perp"].status == "error"
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        assert arb.status == "error" and "exchange down" in arb.error_message


def test_run_close_spot_already_flat_is_noop(arb_registry):
    """If the live free balance is 0, the spot SELL is skipped and the leg is
    still marked closed (nothing to flatten)."""
    arb_registry.state.spot_balances["BTC"] = 0.0
    arb_id = _mk_arb(status="closing")
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02, filled_qty=0.02, status="success")
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02, filled_qty=0.02, status="success")

    arb_executor._run_close(arb_id)

    # no spot sell was fired (balance 0).
    assert not [c for c in arb_registry.state.calls
                if c[0] == "spot_market" and c[2] == "sell"]
    legs = _legs(arb_id)
    assert legs["spot"].status == "closed"
    with session_scope() as db:
        assert db.get(ArbPosition, arb_id).status == "closed"


# --- retry scan: arb retry runs WITHOUT _LEDGER_LOCK, on the right account ---

def test_arb_retry_scan_runs_without_ledger_lock(arb_registry, silent_notifier):
    """A due ArbOrder replays through the retry worker, takes NO _LEDGER_LOCK, and
    writes no Position/StrategyPosition. The account is re-resolved from the leg."""
    from app import retry_worker
    from app import executor as exec_mod

    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="OK", filled_qty_base=0.02, avg_price=50000.0)
    arb_id = _mk_arb()
    leg_id = _add_leg(arb_id, exchange="hyperliquid", account="arb",
                      product="perp", symbol="BTC", side="sell", target_qty=0.02)
    # A due retrying ArbOrder.
    with session_scope() as db:
        order = ArbOrder(arb_leg_id=leg_id, exchange="hyperliquid", account="arb",
                         product="perp", symbol="BTC", side="sell", qty_base=0.02,
                         status="retrying", attempts=1,
                         next_retry_at=_now() - timedelta(seconds=1))
        db.add(order)
        db.flush()
        oid = order.id

    # Assert the ledger lock is NOT acquired during the arb scan.
    real_lock = exec_mod._LEDGER_LOCK

    class _TattleLock:
        acquired = False
        def __enter__(self):
            _TattleLock.acquired = True
            return real_lock.__enter__()
        def __exit__(self, *a):
            return real_lock.__exit__(*a)

    exec_mod._LEDGER_LOCK = _TattleLock()
    try:
        retry_worker._run_due_arb_retries()
    finally:
        exec_mod._LEDGER_LOCK = real_lock

    assert _TattleLock.acquired is False, "arb retry took the directional _LEDGER_LOCK"
    with session_scope() as db:
        o = db.get(ArbOrder, oid)
        leg = db.get(ArbLeg, leg_id)
        assert o.status == "success" and leg.status == "success"
    # re-resolved on the leg's own (hyperliquid, arb).
    market = [c for c in arb_registry.state.calls if c[0] == "market"][-1]
    assert market[5] == "hyperliquid" and market[6] == "arb"
    _assert_no_directional_rows()


def test_arb_retry_orphan_order_marked_dead(arb_registry):
    from app import retry_worker
    with session_scope() as db:
        order = ArbOrder(arb_leg_id=999999, exchange="bybit", account="arb",
                         product="perp", symbol="BTCUSDT", side="sell", qty_base=0.02,
                         status="retrying", attempts=1,
                         next_retry_at=_now() - timedelta(seconds=1))
        db.add(order)
        db.flush()
        oid = order.id
    retry_worker._run_due_arb_retries()
    with session_scope() as db:
        assert db.get(ArbOrder, oid).status == "dead"


# --- shared-writer smoke: a due ArbOrder + a due Order in ONE pass ----------

def test_shared_writer_replays_both_order_and_arb_order(arb_registry, silent_notifier):
    """One `_run_due_retries` pass must replay BOTH a directional Order and an arb
    ArbOrder, neither corrupting the other."""
    from app import retry_worker

    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="OK", filled_qty_base=0.001, avg_price=50000.0)

    # A due directional Order (with its Alert).
    with session_scope() as db:
        alert = Alert(idempotency_key="dir-1", strategy_id="S", action="buy",
                      raw_payload="{}")
        db.add(alert)
        db.flush()
        order = Order(alert_id=alert.id, exchange="bybit", symbol="BTCUSDT",
                      side="buy", qty_usd=0.0, qty_base=0.001, status="retrying",
                      attempts=1, next_retry_at=_now() - timedelta(seconds=1))
        db.add(order)
        db.flush()
        dir_oid = order.id

    # A due arb ArbOrder.
    arb_id = _mk_arb()
    leg_id = _add_leg(arb_id, exchange="hyperliquid", account="arb", product="perp",
                      symbol="BTC", side="sell", target_qty=0.001)
    with session_scope() as db:
        ao = ArbOrder(arb_leg_id=leg_id, exchange="hyperliquid", account="arb",
                      product="perp", symbol="BTC", side="sell", qty_base=0.001,
                      status="retrying", attempts=1,
                      next_retry_at=_now() - timedelta(seconds=1))
        db.add(ao)
        db.flush()
        arb_oid = ao.id

    retry_worker._run_due_retries()   # the combined pass

    with session_scope() as db:
        assert db.get(Order, dir_oid).status == "success"
        assert db.get(ArbOrder, arb_oid).status == "success"
        # the directional Order replay updated the directional ledger...
        assert db.query(Position).count() == 1
        assert db.query(StrategyPosition).count() == 1
        # ...and the arb leg recorded its fill.
        assert db.get(ArbLeg, leg_id).status == "success"


# --- position-level Telegram alerts (A.x) -----------------------------------

class _RecNotifier:
    """A recording notifier: real arb_* method signatures so a wrong call is
    caught, per-INSTANCE `calls` so nothing leaks across tests. Mirrors how
    `silent_notifier` installs itself onto `notifier._notifier`."""
    enabled = True

    def __init__(self):
        self.calls: list[tuple] = []

    def arb_opened(self, arb_id, asset, qty, legs):
        self.calls.append(("arb_opened", arb_id, asset, qty, legs))

    def arb_error(self, arb_id, asset, reason, skew=None):
        self.calls.append(("arb_error", arb_id, asset, reason, skew))

    def arb_closed(self, arb_id, asset, net=None):
        self.calls.append(("arb_closed", arb_id, asset, net))

    # the existing per-leg failure alerts the executor still fires — accept &
    # record them so this notifier is a drop-in for the whole arb flow.
    def order_failed(self, *a, **kw):
        self.calls.append(("order_failed", a, kw))

    def order_dead(self, *a, **kw):
        self.calls.append(("order_dead", a, kw))

    def _names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rec_notifier(monkeypatch):
    from app import notifier as notif_mod
    rec = _RecNotifier()
    monkeypatch.setattr(notif_mod, "_notifier", rec)
    return rec


def test_open_neutral_fires_arb_opened_alert(arb_registry, rec_notifier):
    """Both legs fill + pair is neutral -> informational `arb_opened` (NOT urgent,
    NOT order_dead)."""
    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="P1", filled_qty_base=0.02, avg_price=50000.0)
    arb_registry.state.spot_result = OrderResult(
        success=True, exchange_order_id="S1", filled_qty_base=0.02, avg_price=50000.0)
    arb_id = _mk_arb(asset="BTC")
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02)

    arb_executor._run_open(arb_id)

    assert "arb_opened" in rec_notifier._names()
    assert "arb_error" not in rec_notifier._names()
    assert "order_dead" not in rec_notifier._names()   # not an error finalize
    opened = next(c for c in rec_notifier.calls if c[0] == "arb_opened")
    assert opened[1] == arb_id and opened[2] == "BTC"
    assert opened[3] == pytest.approx(0.02)            # qty per leg


def test_finalize_error_fires_urgent_arb_error_alert(arb_registry, rec_notifier):
    """Leg-1 fails -> hedge withheld -> finalize `error` (non-neutral): the URGENT
    `arb_error` alert fires alongside the existing dead-letter, and no `arb_opened`."""
    arb_registry.state.spot_result = OrderResult(success=False, error_message="boom")
    arb_id = _mk_arb(asset="ETH")
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UETH/USDC",
             side="buy", target_qty=0.5)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="ETH",
             side="sell", target_qty=0.5)

    arb_executor._run_open(arb_id)

    names = rec_notifier._names()
    assert "arb_error" in names
    assert "arb_opened" not in names
    err = next(c for c in rec_notifier.calls if c[0] == "arb_error")
    assert err[1] == arb_id and err[2] == "ETH"
    assert "did not fill" in err[3]                    # the leg-failure reason
    # skew is the signed imbalance: spot didn't fill (0) vs perp not placed (0) -> 0.
    assert err[4] == pytest.approx(0.0)
    with session_scope() as db:
        assert db.get(ArbPosition, arb_id).status == "error"


def test_finalize_error_alert_is_urgent_on_real_notifier(arb_registry, monkeypatch):
    """The arb_error path must reach `send(..., urgent=True)` on the real notifier
    (it's the leg-risk alert) — assert the urgent flag at the send() boundary."""
    from app.notifier import TelegramNotifier
    sent: list[tuple] = []
    notifier = TelegramNotifier(token="t", chat_id="c")
    monkeypatch.setattr(notifier, "send",
                        lambda text, **kw: sent.append((text, kw)))
    from app import notifier as notif_mod
    monkeypatch.setattr(notif_mod, "_notifier", notifier)

    arb_registry.state.spot_result = OrderResult(success=False, error_message="boom")
    arb_id = _mk_arb(asset="BTC")
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02)

    arb_executor._run_open(arb_id)

    arb_msgs = [s for s in sent if "Arb NOT NEUTRAL" in s[0]]
    assert arb_msgs, "arb_error alert was not sent"
    assert all(kw.get("urgent") is True for _, kw in arb_msgs)


def test_close_completion_fires_arb_closed_alert(arb_registry, rec_notifier):
    """A clean close -> `arb_closed` (NOT urgent, NOT order_dead/arb_error)."""
    arb_registry.state.spot_balances["BTC"] = 0.02
    arb_id = _mk_arb(asset="BTC", status="closing")
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02, filled_qty=0.02, status="success")
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02, filled_qty=0.02, status="success")

    arb_executor._run_close(arb_id)

    names = rec_notifier._names()
    assert "arb_closed" in names
    assert "arb_error" not in names and "order_dead" not in names
    closed = next(c for c in rec_notifier.calls if c[0] == "arb_closed")
    assert closed[1] == arb_id and closed[2] == "BTC"


def test_close_alert_reports_realized_net_when_available(arb_registry, rec_notifier):
    """When legs carry funding/commission, the close alert surfaces the readily-
    available net = Σ funding − Σ commission."""
    arb_registry.state.spot_balances["BTC"] = 0.02
    arb_id = _mk_arb(asset="BTC", status="closing")
    with session_scope() as db:
        # perp leg earned funding, both legs paid a commission.
        db.add(ArbLeg(arb_id=arb_id, exchange="hyperliquid", account="arb",
                      product="spot", symbol="UBTC/USDC", side="buy",
                      target_qty=0.02, filled_qty=0.02, commission=1.0,
                      status="success"))
        db.add(ArbLeg(arb_id=arb_id, exchange="hyperliquid", account="arb",
                      product="perp", symbol="BTC", side="sell",
                      target_qty=0.02, filled_qty=0.02, funding=5.0,
                      commission=1.0, status="success"))

    arb_executor._run_close(arb_id)

    closed = next(c for c in rec_notifier.calls if c[0] == "arb_closed")
    assert closed[3] == pytest.approx(5.0 - 2.0)       # funding 5 − fees (1+1)


def test_close_failure_fires_urgent_arb_error_not_closed(arb_registry, rec_notifier):
    """A failed perp close -> arb `error` -> `arb_error` (urgent), never `arb_closed`."""
    arb_id = _mk_arb(asset="BTC", status="closing")
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02, filled_qty=0.02, status="success")
    perp_fake = arb_registry.get("hyperliquid", "arb")
    perp_fake.close_position = lambda symbol: OrderResult(
        success=False, error_message="exchange down")

    arb_executor._run_close(arb_id)

    names = rec_notifier._names()
    assert "arb_error" in names
    assert "arb_closed" not in names
    err = next(c for c in rec_notifier.calls if c[0] == "arb_error")
    assert err[1] == arb_id and "exchange down" in err[3]


def test_arb_alert_failure_never_breaks_finalize(arb_registry, monkeypatch):
    """A throwing notifier must NOT roll back the recorded open (best-effort)."""
    from app import notifier as notif_mod

    class _BoomNotifier:
        enabled = True
        def __getattr__(self, name):
            def _boom(*a, **kw):
                raise RuntimeError("telegram exploded")
            return _boom

    monkeypatch.setattr(notif_mod, "_notifier", _BoomNotifier())
    arb_registry.state.next_result = OrderResult(
        success=True, exchange_order_id="P1", filled_qty_base=0.02, avg_price=50000.0)
    arb_registry.state.spot_result = OrderResult(
        success=True, exchange_order_id="S1", filled_qty_base=0.02, avg_price=50000.0)
    arb_id = _mk_arb(asset="BTC")
    _add_leg(arb_id, exchange="hyperliquid", product="spot", symbol="UBTC/USDC",
             side="buy", target_qty=0.02)
    _add_leg(arb_id, exchange="hyperliquid", product="perp", symbol="BTC",
             side="sell", target_qty=0.02)

    arb_executor._run_open(arb_id)   # must not raise

    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        # the open was still committed despite the notifier exploding.
        assert arb.status == "open" and arb.opened_at is not None
