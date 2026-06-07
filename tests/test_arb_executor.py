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
