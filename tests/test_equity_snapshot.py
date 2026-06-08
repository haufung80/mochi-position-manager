"""Equity curve is now driven by periodic EquitySnapshot rows (the TRUE total PnL),
captured hourly by the funding worker — fixing the flat-line-with-end-spike that
happened when realized PnL couldn't be reconstructed from pre-price-capture fills.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest

from app.db import session_scope
from app.executor import _apply_fill_to_position
from app.funding_worker import write_equity_snapshot
from app.models import EquitySnapshot
from app.routes.dashboard import _equity_curve, _performance
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
        headline = _performance(db, router)["totals"]["total"]
    assert total_pnl == pytest.approx(headline)
    assert realized == pytest.approx(50.0)


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
