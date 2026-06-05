"""Behavioural / adverse-input scenarios — the production data shapes the original
example tests never fed in, plus the reconciliation audit that now catches them:

- an UNPRICED reversal (realized read as $0 — the bug), and
- a symbol shared by long + short strategies (one-way netting).
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.executor import _apply_fill_to_position
from app.main import app
from app.models import Alert, Order, StrategyPosition
from app.routes.dashboard import _performance
from app.routing import StrategyRouter

SECRET = "test-secret-12345"   # matches conftest WEBHOOK_SECRET


def _router(tmp_path, yaml: str) -> StrategyRouter:
    f = tmp_path / "s.yaml"
    f.write_text(yaml)
    return StrategyRouter(f)


def _unpriced_short_then_cover(sid: str, cover_price: float):
    """A short opened on an UNPRICED fill, covered at cover_price, with a flat ledger
    row whose realized was never computed (the production state)."""
    with session_scope() as db:
        a1 = Alert(idempotency_key=f"{sid}-1", strategy_id=sid, action="sell", raw_payload="{}")
        db.add(a1); db.flush()
        db.add(Order(alert_id=a1.id, exchange="bybit", symbol="BTCUSDT", side="sell",
                     qty_base=1.0, qty_usd=0.0, status="success", fill_price=None))
        a2 = Alert(idempotency_key=f"{sid}-2", strategy_id=sid, action="buy", raw_payload="{}")
        db.add(a2); db.flush()
        db.add(Order(alert_id=a2.id, exchange="bybit", symbol="BTCUSDT", side="buy",
                     qty_base=1.0, qty_usd=0.0, status="success", fill_price=cover_price))
        db.add(StrategyPosition(strategy_id=sid, exchange="bybit", symbol="BTCUSDT",
                                net_qty_base=0.0, net_qty_usd=0.0, last_price=cover_price,
                                avg_entry_price=0.0, realized_pnl=0.0,
                                updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))


def test_unpriced_reversal_realized_is_zero_then_recovered(tmp_path, stub_exchange):
    """Given a short opened on an unpriced fill and covered lower, the page first reads
    realized $0 (the bug), and the kline reconciliation recovers the real profit."""
    from app import reconcile
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    _unpriced_short_then_cover("S1", cover_price=90.0)
    stub_exchange.klines["BTCUSDT"] = 100.0          # the unpriced short really filled @ 100

    with session_scope() as db:
        before = _performance(db, router)
    assert next(r for r in before["per_strategy"] if r["strategy_id"] == "S1")["realized"] == pytest.approx(0.0)

    reconcile.backfill_pnl_from_klines(router)
    with session_scope() as db:
        after = _performance(db, router)
    assert next(r for r in after["per_strategy"] if r["strategy_id"] == "S1")["realized"] == pytest.approx(10.0)


def test_shared_symbol_nets_on_exchange_and_flags_attribution(tmp_path, stub_exchange):
    """A long and a short strategy on the SAME symbol: the exchange holds ONE netted
    position, per-strategy unrealized is flagged attributed (notional), and the
    realizable total differs from the per-strategy sum."""
    router = _router(tmp_path,
        "strategies:\n"
        "  LONG:\n    base_asset: BTC\n    venues:\n      bybit: true\n"
        "  SHORT:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with session_scope() as db:
        _apply_fill_to_position(db, "LONG", "bybit", "BTCUSDT", "buy", 3.0, 100.0)
        _apply_fill_to_position(db, "SHORT", "bybit", "BTCUSDT", "sell", 1.0, 120.0)
    stub_exchange.positions["BTCUSDT"] = (2.0, 110.0)    # exchange net +2 @ 110
    stub_exchange.entries["BTCUSDT"] = 100.0

    with session_scope() as db:
        perf = _performance(db, router)
    assert len(perf["open_positions"]) == 2                      # two intent legs
    assert all(p["basis"] == "attributed" for p in perf["open_positions"])
    assert len(perf["exchange_positions"]) == 1                  # one netted exchange position
    assert perf["totals"]["unrealized_attributed"] is True
    # notional per-strategy sum (+30 long, +10 short = +40) != realizable (+2*(110-100)=+20)
    assert perf["totals"]["unrealized"] == pytest.approx(40.0)
    assert perf["totals"]["unrealized_realizable"] == pytest.approx(20.0)


def test_audit_clean_when_ledger_reconciles(tmp_path, stub_exchange):
    """A priced position the ledger agrees with → audit reports clean."""
    from app import reconcile
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with session_scope() as db:
        a = Alert(idempotency_key="c1", strategy_id="S1", action="buy", raw_payload="{}")
        db.add(a); db.flush()
        db.add(Order(alert_id=a.id, exchange="bybit", symbol="BTCUSDT", side="buy",
                     qty_base=1.0, qty_usd=100.0, status="success", fill_price=100.0))
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
    stub_exchange.positions["BTCUSDT"] = (1.0, 100.0)     # exchange agrees with ledger
    result = reconcile.audit_pnl(router)
    assert result["clean"] is True


def test_audit_flags_unpriced_realized_drift(tmp_path, stub_exchange):
    """The unpriced-round-trip bug is caught automatically: the audit flags realized
    drift between the ledger (0) and the fill replay (+10)."""
    from app import reconcile
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    _unpriced_short_then_cover("S1", cover_price=90.0)
    stub_exchange.klines["BTCUSDT"] = 100.0
    result = reconcile.audit_pnl(router)
    assert result["clean"] is False
    issue = next(s for s in result["strategy_issues"] if s["strategy_id"] == "S1")
    assert issue["realized_drift"] == pytest.approx(10.0)


def test_audit_flags_exchange_drift(tmp_path, stub_exchange):
    """A ledger net the exchange doesn't hold (manual/unrecorded trade) is flagged."""
    from app import reconcile
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with session_scope() as db:
        a = Alert(idempotency_key="e1", strategy_id="S1", action="buy", raw_payload="{}")
        db.add(a); db.flush()
        db.add(Order(alert_id=a.id, exchange="bybit", symbol="BTCUSDT", side="buy",
                     qty_base=1.0, qty_usd=100.0, status="success", fill_price=100.0))
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
    stub_exchange.positions["BTCUSDT"] = (5.0, 100.0)     # exchange holds 5, ledger 1
    result = reconcile.audit_pnl(router)
    assert result["clean"] is False
    drift = next(x for x in result["exchange_drift"] if x["symbol"] == "BTCUSDT")
    assert drift["drift"] == pytest.approx(4.0)


def test_audit_endpoint_renders_and_requires_secret(tmp_path, stub_exchange):
    """The /admin audit endpoint: 401 without the secret; with it, renders the drift
    report (both a strategy-realized issue and an exchange-net drift)."""
    router = _router(tmp_path,
        "strategies:\n"
        "  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n"
        "  S2:\n    base_asset: ETH\n    venues:\n      bybit: true\n")
    _unpriced_short_then_cover("S1", cover_price=90.0)            # realized drift -> strategy issue
    with session_scope() as db:                                  # priced long the exchange disagrees with
        a = Alert(idempotency_key="s2", strategy_id="S2", action="buy", raw_payload="{}")
        db.add(a); db.flush()
        db.add(Order(alert_id=a.id, exchange="bybit", symbol="ETHUSDT", side="buy",
                     qty_base=1.0, qty_usd=2000.0, status="success", fill_price=2000.0))
        _apply_fill_to_position(db, "S2", "bybit", "ETHUSDT", "buy", 1.0, 2000.0)
    stub_exchange.klines["BTCUSDT"] = 100.0
    stub_exchange.positions["ETHUSDT"] = (5.0, 2000.0)           # exchange holds 5, ledger 1

    with TestClient(app) as c:
        c.app.state.strategy_router = router
        assert c.post("/admin/strategies/audit", data={"secret": "nope"}).status_code == 401
        r = c.post("/admin/strategies/audit", data={"secret": SECRET})
    assert r.status_code == 200
    assert "Drift detected" in r.text
    assert "S1" in r.text and "ETHUSDT" in r.text                # both render paths exercised
