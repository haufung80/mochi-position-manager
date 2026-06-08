"""Equity backfill from exchange history -> daily per-exchange + aggregate snapshots.
The live exchange calls can't be exercised here (no creds); these cover the pure
daily-aggregation math + persistence (idempotent, never touches live rows)."""
from __future__ import annotations
import json
from datetime import datetime, timezone

import pytest

from app import equity_backfill
from app.db import session_scope
from app.equity_backfill import _build_daily_rows, backfill_equity
from app.models import EquitySnapshot


def _ms(y, mo, d, h=0):
    return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp() * 1000)


def test_build_daily_rows_cumulative_and_rebase():
    start, now = _ms(2026, 5, 28), _ms(2026, 6, 2)
    bybit = [(_ms(2026, 5, 29), 100.0), (_ms(2026, 5, 30), -5.0), (_ms(2026, 6, 1), 50.0)]
    hl = [(_ms(2026, 5, 28), 0.0), (_ms(2026, 5, 31), 40.0)]
    rows = _build_daily_rows(bybit, hl, start, now)
    by = {datetime.fromtimestamp(d / 1000, tz=timezone.utc).day: (b, h) for d, b, h in rows}
    assert len(rows) == 6                          # May28..Jun2 inclusive
    assert by[28] == pytest.approx((0.0, 0.0))     # nothing yet on the 28th
    assert by[29] == pytest.approx((100.0, 0.0))
    assert by[30] == pytest.approx((95.0, 0.0))    # 100 - 5 funding
    assert by[31] == pytest.approx((95.0, 40.0))   # hl cumulative point on the 31st
    assert by[1] == pytest.approx((145.0, 40.0))   # + 50 bybit
    assert by[2] == pytest.approx((145.0, 40.0))   # carry forward, no new events


def test_backfill_equity_writes_and_is_idempotent(monkeypatch):
    start, now = _ms(2026, 5, 28), _ms(2026, 6, 2)

    class FakeBybit:
        def get_closed_pnl(self, s, e): return [(_ms(2026, 5, 29), 100.0), (_ms(2026, 6, 1), 50.0)]
        def get_account_funding(self, s, e): return [(_ms(2026, 5, 30), -5.0)]

    class FakeHL:
        def get_pnl_history(self): return [(_ms(2026, 5, 28), 0.0), (_ms(2026, 5, 31), 40.0)]

    class FakeReg:
        def get(self, name, account="default"):
            return FakeBybit() if name == "bybit" else FakeHL()

    monkeypatch.setattr(equity_backfill, "get_registry", lambda: FakeReg())
    s = backfill_equity(start, now_ms=now)
    assert s["rows"] == 6 and s["bybit_events"] == 3 and s["hl_points"] == 2
    with session_scope() as db:
        snaps = (db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill")
                 .order_by(EquitySnapshot.captured_at).all())
        assert len(snaps) == 6
        last = json.loads(snaps[-1].by_exchange)
        assert last["bybit"] == pytest.approx(145.0)
        assert last["hyperliquid"] == pytest.approx(40.0)
        assert snaps[-1].total_pnl == pytest.approx(185.0)
    backfill_equity(start, now_ms=now)             # re-run replaces, never duplicates
    with session_scope() as db:
        assert db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill").count() == 6


def test_backfill_no_data_leaves_existing_untouched(monkeypatch):
    start, now = _ms(2026, 5, 28), _ms(2026, 6, 2)
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=datetime(2026, 5, 28, 12, tzinfo=timezone.utc),
                              total_pnl=10.0, by_exchange="{}", source="backfill"))

    class Empty:
        def get_closed_pnl(self, s, e): return []
        def get_account_funding(self, s, e): return []
        def get_pnl_history(self): return []

    monkeypatch.setattr(equity_backfill, "get_registry",
                        lambda: type("R", (), {"get": lambda self, n, account="default": Empty()})())
    s = backfill_equity(start, now_ms=now)
    assert s["rows"] == 0 and any("no data" in e for e in s["errors"])
    with session_scope() as db:                    # a failed pull must NOT wipe prior backfill
        assert db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill").count() == 1


def test_backfill_does_not_touch_live_snapshots(monkeypatch):
    start, now = _ms(2026, 5, 28), _ms(2026, 6, 2)
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=datetime(2026, 6, 2, 9, tzinfo=timezone.utc),
                              total_pnl=200.0, by_exchange="{}", source="live"))

    class FakeBybit:
        def get_closed_pnl(self, s, e): return [(_ms(2026, 5, 29), 10.0)]
        def get_account_funding(self, s, e): return []

    class FakeHL:
        def get_pnl_history(self): return []

    monkeypatch.setattr(equity_backfill, "get_registry",
                        lambda: type("R", (), {"get": lambda self, n, account="default":
                                               FakeBybit() if n == "bybit" else FakeHL()})())
    backfill_equity(start, now_ms=now)
    with session_scope() as db:
        assert db.query(EquitySnapshot).filter(EquitySnapshot.source == "live").count() == 1
        assert db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill").count() == 6


def test_maybe_backfill_equity_one_time_and_config(monkeypatch):
    """The startup hook skips when blank, runs once when configured, and is one-time
    (skips once backfill rows exist)."""
    from app import funding_worker
    calls = []
    monkeypatch.setattr("app.equity_backfill.backfill_equity",
                        lambda start_ms, now_ms=None: calls.append(start_ms)
                        or {"rows": 0, "bybit_events": 0, "hl_points": 0, "errors": []})
    monkeypatch.setattr(funding_worker, "get_settings",
                        lambda: type("S", (), {"equity_backfill_start": ""})())
    funding_worker._maybe_backfill_equity()
    assert calls == []                              # blank -> skip
    monkeypatch.setattr(funding_worker, "get_settings",
                        lambda: type("S", (), {"equity_backfill_start": "2026-05-28"})())
    funding_worker._maybe_backfill_equity()
    assert len(calls) == 1                          # configured + no rows -> runs once
    with session_scope() as db:                     # a backfill row already present...
        db.add(EquitySnapshot(captured_at=datetime(2026, 5, 28, 12, tzinfo=timezone.utc),
                              total_pnl=1.0, by_exchange="{}", source="backfill"))
    funding_worker._maybe_backfill_equity()
    assert len(calls) == 1                          # ...so it doesn't run again (one-time)
