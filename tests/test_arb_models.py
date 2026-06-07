"""A.3 — the four funding-arb tables (create_all) + directional-schema regression.

The `_clean_db` autouse fixture (conftest) drops + recreates the schema per test,
so these run against a fresh SQLite DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db import engine, session_scope
from app.models import ArbFundingEvent, ArbLeg, ArbOrder, ArbPosition


def _now():
    return datetime.now(timezone.utc)


# --- tables exist ------------------------------------------------------------

def test_all_four_arb_tables_created():
    names = set(inspect(engine).get_table_names())
    assert {"arb_positions", "arb_legs", "arb_orders", "arb_funding_events"} <= names


def test_directional_tables_still_present():
    names = set(inspect(engine).get_table_names())
    assert {"alerts", "orders", "positions", "strategy_positions",
            "funding_events"} <= names


# --- ArbPosition.idempotency_key UNIQUE --------------------------------------

def test_arb_position_idempotency_key_unique():
    with session_scope() as db:
        db.add(ArbPosition(idempotency_key="dup", asset="BTC", status="opening"))
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(ArbPosition(idempotency_key="dup", asset="ETH", status="opening"))


def test_arb_position_roundtrip_fields():
    with session_scope() as db:
        db.add(ArbPosition(
            idempotency_key="k1", asset="BTC", strategy_tag="hl-cnc",
            notional_target=1000.0, status="open", realized_pnl=0.0,
            opened_at=_now()))
    with session_scope() as db:
        p = db.query(ArbPosition).filter_by(idempotency_key="k1").one()
        assert p.asset == "BTC" and p.strategy_tag == "hl-cnc"
        assert p.notional_target == 1000.0 and p.status == "open"
        assert p.closed_at is None and p.error_message == ""


# --- ArbLeg UNIQUE(arb_id, exchange, product, symbol) ------------------------

def test_arb_leg_identity_unique():
    with session_scope() as db:
        db.add(ArbLeg(arb_id=1, exchange="hyperliquid", account="arb",
                      product="perp", symbol="BTC", side="sell", target_qty=0.01))
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(ArbLeg(arb_id=1, exchange="hyperliquid", account="arb",
                          product="perp", symbol="BTC", side="sell", target_qty=0.02))


def test_arb_leg_spot_and_perp_same_symbol_coexist():
    """Same arb_id + symbol but different product (spot vs perp) is allowed —
    the cash-and-carry pair has both legs on BTC/BTCUSDT."""
    with session_scope() as db:
        db.add(ArbLeg(arb_id=1, exchange="bybit", account="arb", product="spot",
                      symbol="BTCUSDT", side="buy", target_qty=0.01))
        db.add(ArbLeg(arb_id=1, exchange="bybit", account="arb", product="perp",
                      symbol="BTCUSDT", side="sell", target_qty=0.01))
    with session_scope() as db:
        assert db.query(ArbLeg).filter_by(arb_id=1).count() == 2


def test_arb_leg_filled_qty_is_net_base():
    with session_scope() as db:
        db.add(ArbLeg(arb_id=2, exchange="bybit", account="arb", product="spot",
                      symbol="BTCUSDT", side="buy", target_qty=0.01,
                      filled_qty=0.00999, avg_fill=50000.0,
                      commission=0.00001, commission_asset="BTC"))
    with session_scope() as db:
        leg = db.query(ArbLeg).filter_by(arb_id=2).one()
        assert leg.filled_qty == 0.00999          # net of the base fee
        assert leg.commission_asset == "BTC"


# --- ArbOrder has account + product + arb_leg_id -----------------------------

def test_arb_order_has_account_product_and_leg_link():
    cols = {c["name"] for c in inspect(engine).get_columns("arb_orders")}
    assert {"arb_leg_id", "account", "product"} <= cols
    with session_scope() as db:
        db.add(ArbOrder(arb_leg_id=5, exchange="hyperliquid", account="arb",
                        product="perp", symbol="BTC", side="sell", qty_base=0.01))
    with session_scope() as db:
        o = db.query(ArbOrder).filter_by(arb_leg_id=5).one()
        assert o.account == "arb" and o.product == "perp"
        assert o.status == "pending" and o.attempts == 0


# --- ArbFundingEvent UNIQUE(exchange, account, symbol, funding_time) ---------

def test_arb_funding_event_unique():
    ts = _now()
    with session_scope() as db:
        db.add(ArbFundingEvent(arb_id=1, exchange="bybit", account="arb",
                               symbol="BTCUSDT", funding_time=ts, amount=0.5))
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(ArbFundingEvent(arb_id=1, exchange="bybit", account="arb",
                                   symbol="BTCUSDT", funding_time=ts, amount=0.9))


def test_arb_funding_event_arb_id_nullable():
    with session_scope() as db:
        db.add(ArbFundingEvent(arb_id=None, exchange="hyperliquid", account="arb",
                               symbol="BTC", funding_time=_now(), amount=-0.1))
    with session_scope() as db:
        assert db.query(ArbFundingEvent).filter_by(arb_id=None).count() == 1


# --- regression: Order schema is UNCHANGED -----------------------------------

def test_orders_table_schema_unchanged():
    """The directional `orders` table must be byte-for-byte the same as before
    the arb work — no new columns, no dropped NOT NULL. Pins the exact set."""
    cols = {c["name"] for c in inspect(engine).get_columns("orders")}
    expected = {
        "id", "alert_id", "exchange", "symbol", "side", "qty_usd", "qty_base",
        "reduce_only", "leverage", "signal_price", "fill_price", "commission",
        "commission_asset", "status", "attempts", "exchange_order_id",
        "error_message", "next_retry_at", "created_at", "updated_at",
    }
    assert cols == expected


def test_orders_alert_id_still_not_null():
    alert_col = next(c for c in inspect(engine).get_columns("orders")
                     if c["name"] == "alert_id")
    assert alert_col["nullable"] is False
