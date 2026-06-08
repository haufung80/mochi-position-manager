"""Equity curve is now driven by periodic EquitySnapshot rows (the TRUE total PnL),
captured hourly by the funding worker — fixing the flat-line-with-end-spike that
happened when realized PnL couldn't be reconstructed from pre-price-capture fills.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import session_scope
from app.executor import _apply_fill_to_position
from app.funding_worker import write_equity_snapshot
from app.models import EquitySnapshot
from app.routes.dashboard import _equity_curve, _equity_metrics, _equity_series, _performance
from app.routing import StrategyRouter


def _router(tmp_path, yaml: str) -> StrategyRouter:
    f = tmp_path / "s.yaml"
    f.write_text(yaml)
    return StrategyRouter(f)


def test_write_equity_snapshot_equals_headline_and_captures_realized(tmp_path, stub_exchange):
    """The worker writes ONE snapshot whose total_pnl equals the live /performance
    headline, and a *booked* realized gain is captured — the whole point of the fix."""
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with session_scope() as db:                       # closed round-trip: +50 realized, flat now
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "sell", 1.0, 150.0)

    assert write_equity_snapshot(router) is True
    with session_scope() as db:                       # read scalars inside the session
        snaps = db.query(EquitySnapshot).all()
        assert len(snaps) == 1
        total_pnl, realized = snaps[0].total_pnl, snaps[0].realized
        by_ex = json.loads(snaps[0].by_exchange)
        headline = _performance(db, router)["totals"]["total"]
    assert total_pnl == pytest.approx(headline)
    assert realized == pytest.approx(50.0)
    assert by_ex.get("bybit") == pytest.approx(50.0)   # per-exchange total captured


def test_equity_curve_empty_without_snapshots():
    with session_scope() as db:
        assert _equity_curve(db) == []                          # nothing to plot
        assert _equity_curve(db, end_total=12.5) == []          # no snapshots -> no tip (empty state)


def test_anchor_never_draws_backward_segment():
    """A snapshot stamped in the future (clock skew) must not make the anchor go
    backward in time — else the time-positioned SVG draws a reversed segment."""
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=future, total_pnl=30.0))
    with session_scope() as db:
        pts = _equity_curve(db, end_total=30.0)
    assert pts[-1][0] >= pts[0][0]


def test_equity_series_windows_and_per_exchange():
    """Windows filter by captured_at; per-exchange JSON becomes its own line; the
    live tip is appended to every series."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=now - timedelta(days=10), total_pnl=10.0,
                              by_exchange=json.dumps({"bybit": 6.0, "hyperliquid": 4.0})))
        db.add(EquitySnapshot(captured_at=now - timedelta(hours=2), total_pnl=30.0,
                              by_exchange=json.dumps({"bybit": 18.0, "hyperliquid": 12.0})))
    live_by_ex = {"bybit": 20.0, "hyperliquid": 13.0}
    with session_scope() as db:
        wide = _equity_series(db, None, live_total=33.0, live_by_ex=live_by_ex)            # All
        day = _equity_series(db, timedelta(hours=24), live_total=33.0, live_by_ex=live_by_ex)
    # All-time: 2 snapshots + 1 live tip on the aggregate; one line per venue too
    assert set(wide) == {"Total", "bybit", "hyperliquid"}
    assert len(wide["Total"]) == 3 and wide["Total"][-1][1] == pytest.approx(33.0)
    assert wide["bybit"][-1][1] == pytest.approx(20.0)            # venue line tipped to live
    # 24h window keeps only the 2h-old snapshot (+ tip)
    assert len(day["Total"]) == 2


def test_equity_metrics_drawdown_and_period():
    """Peak, max drawdown ($ and % of peak), and period Δ over the Total series."""
    series = [(None, 0.0), (None, 50.0), (None, 20.0), (None, 40.0)]   # peak 50, dd 30, end 40
    m = _equity_metrics(series)
    assert m["current"] == pytest.approx(40.0)
    assert m["period"] == pytest.approx(40.0)             # 40 - 0
    assert m["peak"] == pytest.approx(50.0)
    assert m["max_drawdown"] == pytest.approx(30.0)       # 50 -> 20
    assert m["max_drawdown_pct"] == pytest.approx(60.0)   # 30 / 50
    assert m["dd_from_peak"] == pytest.approx(10.0)       # 50 - 40
    assert _equity_metrics([]) is None
