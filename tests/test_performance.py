"""/performance page: rendering + PnL/fee/equity computation."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.executor import _apply_fill_to_position
from app.main import app
from app.models import Alert, FundingEvent, Order
from app.routes.dashboard import _equity_curve, _equity_svg, _performance
from app.routing import StrategyRouter


@pytest.fixture
def client(tmp_path):
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with TestClient(app) as c:
        c.app.state.strategy_router = StrategyRouter(f)
        yield c


def _seed_round_trip():
    """Buy 1 @100 then sell 1 @120 (realized +20). Buy signal 99.5 -> 0.5 slippage;
    commission 0.10 + 0.12; funding -0.05. Ledger price == order fill_price."""
    with session_scope() as db:
        a = Alert(idempotency_key="k1", strategy_id="S1", action="buy",
                  raw_payload="{}", signal_price=99.5)
        db.add(a); db.flush()
        db.add(Order(alert_id=a.id, exchange="bybit", symbol="BTCUSDT", side="buy",
                     qty_usd=100.0, qty_base=1.0, status="success",
                     signal_price=99.5, fill_price=100.0, commission=0.10,
                     commission_asset="USDT"))
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
    with session_scope() as db:
        a = Alert(idempotency_key="k2", strategy_id="S1", action="sell",
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


def test_performance_empty_state_renders(client):
    r = client.get("/performance")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")
    assert "Live Performance" in r.text
    assert "No PnL history yet" in r.text
    assert "viewport-fit=cover" in r.text          # mobile/Safari responsive


def test_performance_renders_with_data(client):
    _seed_round_trip()
    r = client.get("/performance")
    assert r.status_code == 200
    assert "S1" in r.text                       # per-strategy + per-exchange rows
    assert "<polyline" in r.text                # equity curve drawn
    assert "Recent orders" in r.text
    assert "bybit/BTCUSDT" in r.text            # order row venue


def test_performance_numbers(client):
    _seed_round_trip()
    with session_scope() as db:
        perf = _performance(db, client.app.state.strategy_router)
    s1 = next(r for r in perf["per_strategy"] if r["strategy_id"] == "S1")
    assert s1["realized"] == pytest.approx(20.0)
    assert s1["unrealized"] == pytest.approx(0.0)        # flat after round-trip
    assert s1["commission"] == pytest.approx(0.22)
    assert s1["slippage"] == pytest.approx(0.5)          # (100-99.5)*1 on the buy
    assert s1["funding"] == pytest.approx(-0.05)         # single-owner attribution
    # total = realized + unrealized + funding - commission (slippage NOT included)
    assert s1["total"] == pytest.approx(20.0 - 0.05 - 0.22)
    assert perf["totals"]["total"] == pytest.approx(19.73)


def test_open_position_unrealized_uses_live_mark(client, stub_exchange):
    """Open position: unrealized marks to the live exchange position, not the stale
    fill. S1 solely owns BTCUSDT here, so its unrealized is 'real' (not attributed)."""
    with session_scope() as db:
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 2.0, 100.0)
    stub_exchange.positions["BTCUSDT"] = (2.0, 110.0)     # exchange truth: +2 @ mark 110
    with session_scope() as db:
        perf = _performance(db, client.app.state.strategy_router)
    s1 = next(r for r in perf["per_strategy"] if r["strategy_id"] == "S1")
    assert s1["unrealized"] == pytest.approx(20.0)        # 2 * (110 - 100), attribution
    assert s1["unrealized_attributed"] is False           # sole owner -> exact
    assert perf["open_positions"][0]["mark"] == pytest.approx(110.0)
    assert perf["open_positions"][0]["basis"] == "real"
    # Headline + actual table come from the exchange read.
    assert perf["totals"]["unrealized"] == pytest.approx(20.0)
    assert perf["exchange_positions"][0]["net_qty_base"] == pytest.approx(2.0)
    assert perf["exchange_positions"][0]["source"] == "exchange"
    assert perf["exchange_positions"][0]["unrealized"] == pytest.approx(20.0)


def test_actual_position_reads_exchange_not_ledger(tmp_path, stub_exchange):
    """The 'actual' table + headline unrealized come from the exchange, even when the
    per-strategy ledger drifts (e.g. a manual top-up that wasn't recorded as a fill)."""
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    router = StrategyRouter(f)
    with session_scope() as db:
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
    # Ledger intent says 1 BTC, but the exchange actually holds 3 BTC @ 110.
    stub_exchange.positions["BTCUSDT"] = (3.0, 110.0)
    with session_scope() as db:
        perf = _performance(db, router)
    ap = perf["exchange_positions"][0]
    assert ap["net_qty_base"] == pytest.approx(3.0)       # exchange wins over ledger's 1.0
    assert ap["unrealized"] == pytest.approx(3 * (110 - 100))
    assert perf["totals"]["unrealized"] == pytest.approx(30.0)
    # the intent leg still shows the ledger's (drifted) 1.0
    assert perf["open_positions"][0]["net_qty_base"] == pytest.approx(1.0)


def test_open_positions_net_into_actual_exchange_positions(tmp_path, stub_exchange):
    """Two strategies on the SAME symbol show as two intent legs but ONE netted
    exchange position; the shared-symbol legs are flagged 'attributed'."""
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies:\n"
                 "  LONG:\n    base_asset: BTC\n    venues:\n      bybit: true\n"
                 "  SHORT:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    router = StrategyRouter(f)
    with session_scope() as db:
        _apply_fill_to_position(db, "LONG", "bybit", "BTCUSDT", "buy", 3.0, 100.0)
        _apply_fill_to_position(db, "SHORT", "bybit", "BTCUSDT", "sell", 1.0, 100.0)
    stub_exchange.positions["BTCUSDT"] = (2.0, 110.0)     # exchange net = +2 @ 110
    with session_scope() as db:
        perf = _performance(db, router)
    # two per-strategy intent legs, both attributed (shared symbol)
    assert len(perf["open_positions"]) == 2
    assert all(p["basis"] == "attributed" for p in perf["open_positions"])
    # ONE real exchange position = net +2 BTC @ mark 110, blended entry 100 -> +20
    assert len(perf["exchange_positions"]) == 1
    ap = perf["exchange_positions"][0]
    assert ap["net_qty_base"] == pytest.approx(2.0)
    assert ap["unrealized"] == pytest.approx(2 * (110 - 100))
    assert perf["totals"]["unrealized"] == pytest.approx(20.0)
    assert perf["totals"]["unrealized_attributed"] is True


def test_offsetting_legs_show_no_exchange_position(tmp_path, stub_exchange):
    """Exchange flat (legs offset) -> no actual position, even with ledger rows."""
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies:\n"
                 "  LONG:\n    base_asset: BTC\n    venues:\n      bybit: true\n"
                 "  SHORT:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    router = StrategyRouter(f)
    with session_scope() as db:
        _apply_fill_to_position(db, "LONG", "bybit", "BTCUSDT", "buy", 2.0, 100.0)
        _apply_fill_to_position(db, "SHORT", "bybit", "BTCUSDT", "sell", 2.0, 100.0)
    # exchange holds nothing (default (0.0, 0.0))
    with session_scope() as db:
        perf = _performance(db, router)
    assert perf["exchange_positions"] == []
    assert perf["totals"]["unrealized"] == pytest.approx(0.0)


def test_unrealized_guarded_when_avg_entry_zero(client, stub_exchange):
    """A position with avg_entry_price=0 (e.g. migrated in) reports 0 unrealized,
    never the whole notional (qty * mark)."""
    from app.models import StrategyPosition
    with session_scope() as db:
        db.add(StrategyPosition(
            strategy_id="S1", exchange="bybit", symbol="BTCUSDT",
            net_qty_base=1.0, net_qty_usd=50000.0, last_price=50000.0,
            avg_entry_price=0.0, realized_pnl=0.0,
            updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
    stub_exchange.prices["BTCUSDT"] = 50000.0
    with session_scope() as db:
        perf = _performance(db, client.app.state.strategy_router)
    s1 = next(r for r in perf["per_strategy"] if r["strategy_id"] == "S1")
    assert s1["unrealized"] == pytest.approx(0.0)         # NOT 1 * 50000


def test_equity_curve_starts_from_zero_and_accumulates(client):
    _seed_round_trip()
    with session_scope() as db:
        points = _equity_curve(db)
        svg = _equity_svg(points)
    # buy(-0.10 comm), sell(+20 realized -0.12 comm), funding(-0.05) -> 19.73
    assert points[-1][1] == pytest.approx(19.73)
    assert svg is not None and svg["polyline"]


def test_equity_curve_marks_to_market_at_tip(client):
    """Live unrealized is folded into a final point so the curve ends at the real
    current total PnL (realized history + unrealized)."""
    _seed_round_trip()
    with session_scope() as db:
        base = _equity_curve(db)
        marked = _equity_curve(db, unrealized=50.0)
    assert len(marked) == len(base) + 1                  # one mark-to-market tip
    assert marked[-1][1] == pytest.approx(19.73 + 50.0)


def test_equity_curve_no_tip_when_flat(client):
    """No open positions (unrealized=0) -> no synthetic tip appended."""
    _seed_round_trip()
    with session_scope() as db:
        assert len(_equity_curve(db, unrealized=0.0)) == len(_equity_curve(db))
