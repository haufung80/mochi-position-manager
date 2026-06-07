"""A.6 — regression isolation (LOAD-BEARING).

Proves the arb book is STRUCTURALLY invisible to the directional reporting + the
reconcile audit, under a DETERMINISTIC multi-account stub so the directional
numbers are byte-stable across runs:

  1. ``_execution_quality`` fees + slippage are EQUAL with vs without arb rows
     (``ArbOrder``/``ArbLeg`` present) — the real bleed vector, since
     ``_execution_quality`` queries ``Order`` with NO Alert join (an arb row in
     ``orders`` would inflate it). Arb fills live in ``arb_orders``, so they can't.
  2. ``Σ FundingEvent`` (the directional headline funding) AND the ``_equity_curve``
     points are EQUAL with arb ``ArbFundingEvent`` rows present — proving the
     directional funding/equity queries are blind to the arb funding table.
  3. ``reconcile.audit_pnl`` does NOT read any ``arb_*`` table (it queries only
     ``StrategyPosition`` / ``Order`` / ``Alert``).

If any of these regress, an arb row has leaked into a directional query — exactly
the catastrophe the separate-tables design exists to prevent.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.executor import _apply_fill_to_position
from app.main import app
from app.models import (
    Alert, ArbFundingEvent, ArbLeg, ArbOrder, ArbPosition, FundingEvent, Order,
)
from app.routes.dashboard import _equity_curve, _execution_quality, _performance
from app.routing import StrategyRouter


@pytest.fixture
def client(tmp_path):
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with TestClient(app) as c:
        c.app.state.strategy_router = StrategyRouter(f)
        yield c


def _seed_directional():
    """A directional round-trip + a directional funding event (the numbers whose
    invariance under arb rows we assert)."""
    with session_scope() as db:
        a = Alert(idempotency_key="d1", strategy_id="S1", action="buy",
                  raw_payload="{}", signal_price=99.5)
        db.add(a); db.flush()
        db.add(Order(alert_id=a.id, exchange="bybit", symbol="BTCUSDT", side="buy",
                     qty_usd=100.0, qty_base=1.0, status="success",
                     signal_price=99.5, fill_price=100.0, commission=0.10,
                     commission_asset="USDT"))
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
    with session_scope() as db:
        a = Alert(idempotency_key="d2", strategy_id="S1", action="sell",
                  raw_payload="{}", signal_price=120.0)
        db.add(a); db.flush()
        db.add(Order(alert_id=a.id, exchange="bybit", symbol="BTCUSDT", side="sell",
                     qty_usd=120.0, qty_base=1.0, status="success",
                     signal_price=120.0, fill_price=120.0, commission=0.12,
                     commission_asset="USDT"))
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "sell", 1.0, 120.0)
        db.add(FundingEvent(exchange="bybit", symbol="BTCUSDT",
                            funding_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
                            amount=-0.05))


def _seed_arb_book():
    """A FULLY-POPULATED arb book: an ArbPosition + both legs + ArbOrders (with
    big commissions) + ArbFundingEvents (with big amounts). If ANY of these bled
    into a directional query, the directional numbers below would move."""
    with session_scope() as db:
        arb = ArbPosition(idempotency_key="arb1", asset="BTC", status="open",
                          opened_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        db.add(arb); db.flush()
        spot = ArbLeg(arb_id=arb.id, exchange="bybit", account="arb", product="spot",
                      symbol="BTCUSDT", side="buy", filled_qty=0.02, avg_fill=50000.0,
                      commission=7.77, commission_asset="BTC", status="success")
        perp = ArbLeg(arb_id=arb.id, exchange="hyperliquid", account="arb",
                      product="perp", symbol="BTC", side="sell", filled_qty=0.02,
                      avg_fill=50000.0, commission=8.88, status="success")
        db.add(spot); db.add(perp); db.flush()
        # ArbOrders mirror Order's execution fields (the bleed vector for
        # _execution_quality) — big fees that must NEVER show up there.
        for lg in (spot, perp):
            db.add(ArbOrder(arb_leg_id=lg.id, exchange=lg.exchange, account="arb",
                            product=lg.product, symbol=lg.symbol, side=lg.side,
                            qty_base=lg.filled_qty, avg_fill=lg.avg_fill,
                            commission=lg.commission,
                            commission_asset=lg.commission_asset, status="success"))
        # Arb funding — big amounts in the SEPARATE table.
        db.add(ArbFundingEvent(exchange="hyperliquid", account="arb", symbol="BTC",
                               funding_time=datetime(2026, 5, 2, tzinfo=timezone.utc),
                               amount=123.45))
        db.add(ArbFundingEvent(exchange="bybit", account="arb", symbol="BTCUSDT",
                               funding_time=datetime(2026, 5, 3, tzinfo=timezone.utc),
                               amount=67.89))


# ===========================================================================
# 1. _execution_quality fees/slippage EQUAL with vs without arb rows
# ===========================================================================

def test_execution_quality_blind_to_arb_orders():
    _seed_directional()
    with session_scope() as db:
        before = _execution_quality(db)
    _seed_arb_book()
    with session_scope() as db:
        after = _execution_quality(db)
    assert after == before, "ArbOrder rows bled into _execution_quality"
    # And concretely: only the directional bybit fees (0.10+0.12) are reported —
    # never the arb's 7.77/8.88.
    bybit_fees = next(r for r in after["fees_by_exchange"] if r["exchange"] == "bybit")
    assert bybit_fees["total"] == pytest.approx(0.22)
    assert all(r["exchange"] != "hyperliquid" for r in after["fees_by_exchange"])
    assert after["total_fees"] == pytest.approx(0.22)


# ===========================================================================
# 2. FundingEvent sum + equity-curve points EQUAL with arb funding present
# ===========================================================================

def test_funding_sum_and_equity_blind_to_arb_funding():
    _seed_directional()
    with session_scope() as db:
        before_points = _equity_curve(db)
        before_funding = sum(fe.amount for fe in db.query(FundingEvent).all())
    _seed_arb_book()
    with session_scope() as db:
        after_points = _equity_curve(db)
        after_funding = sum(fe.amount for fe in db.query(FundingEvent).all())
        # Directional FundingEvent table is untouched by the arb funding rows.
        assert db.query(ArbFundingEvent).count() == 2     # they DO exist…
    assert after_funding == before_funding == pytest.approx(-0.05)   # …but only -0.05 here
    assert after_points == before_points, "ArbFundingEvent bled into the equity curve"


def test_performance_headline_blind_to_arb(client):
    """The /performance headline (funding + commission) is identical with the arb
    book present — under the deterministic default-account stub (no live reads,
    flat BTCUSDT after the round-trip)."""
    _seed_directional()
    with session_scope() as db:
        before = _performance(db, client.app.state.strategy_router)
    _seed_arb_book()
    with session_scope() as db:
        after = _performance(db, client.app.state.strategy_router)
    assert after["totals"]["funding"] == before["totals"]["funding"] == pytest.approx(-0.05)
    assert after["totals"]["commission"] == before["totals"]["commission"] == pytest.approx(0.22)
    assert after["totals"]["total"] == before["totals"]["total"]


def test_directional_dashboard_renders_unchanged_with_arb_book(client):
    """End-to-end: /performance still renders the directional numbers (no arb
    leakage in the HTML) with a full arb book present."""
    _seed_directional()
    _seed_arb_book()
    r = client.get("/performance")
    assert r.status_code == 200
    # the directional fee total is the small directional number, never 7.77/8.88.
    assert "7.77" not in r.text and "8.88" not in r.text
    assert "123.45" not in r.text     # arb funding never reaches /performance


# ===========================================================================
# 3. reconcile.audit_pnl does not read arb_* tables (structural)
# ===========================================================================

def test_audit_pnl_does_not_read_arb_tables(client, stub_exchange):
    """audit_pnl queries only StrategyPosition/Order/Alert. With a full arb book
    present and the directional ledger flat, the audit is clean and surfaces no
    arb rows. We also assert structurally that the arb ORM classes are never
    queried by instrumenting the Session."""
    from app import reconcile
    _seed_directional()      # round-trip -> flat directional ledger
    _seed_arb_book()

    f = client.app.state.strategy_router
    # Instrument db.query to record every queried entity for this call.
    queried: list = []
    import app.db as db_mod
    real_scope = db_mod.session_scope
    from contextlib import contextmanager

    @contextmanager
    def _recording_scope():
        with real_scope() as db:
            orig = db.query
            def _q(*ents, **kw):
                queried.extend(ents)
                return orig(*ents, **kw)
            db.query = _q  # type: ignore[method-assign]
            yield db

    import app.reconcile as rec_mod
    rec_mod.session_scope = _recording_scope
    try:
        result = reconcile.audit_pnl(f)
    finally:
        rec_mod.session_scope = real_scope

    # No arb ORM class was ever passed to db.query inside audit_pnl.
    arb_classes = {ArbPosition, ArbLeg, ArbOrder, ArbFundingEvent}
    assert not (set(queried) & arb_classes), f"audit_pnl queried arb tables: {queried}"
    # And it reports nothing about the arb book.
    blob = repr(result)
    assert "arb" not in blob.lower()
    assert result["clean"] is True
