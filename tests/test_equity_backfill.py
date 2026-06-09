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


def test_backfill_venue_error_leaves_existing_untouched(monkeypatch):
    """A venue EXCEPTION during a (re-)run must not replace good backfill rows with a
    partial set — existing rows are preserved and the error is reported, so the startup
    hook won't mark it done and will retry."""
    start, now = _ms(2026, 5, 28), _ms(2026, 6, 2)
    with session_scope() as db:
        for d in (28, 29, 30):
            db.add(EquitySnapshot(captured_at=datetime(2026, 5, d, 12, tzinfo=timezone.utc),
                                  total_pnl=10.0, by_exchange='{"bybit": 10.0}', source="backfill"))

    class BoomBybit:
        def get_closed_pnl(self, s, e): raise RuntimeError("bybit down")
        def get_account_funding(self, s, e): return []

    class FakeHL:
        def get_pnl_history(self): return [(_ms(2026, 5, 28), 0.0), (_ms(2026, 5, 31), 40.0)]

    monkeypatch.setattr(equity_backfill, "get_registry",
                        lambda: type("R", (), {"get": lambda self, n, account="default":
                                               BoomBybit() if n == "bybit" else FakeHL()})())
    s = backfill_equity(start, now_ms=now)
    assert s["rows"] == 0 and any("bybit" in e for e in s["errors"])
    with session_scope() as db:                    # prior rows preserved, not partial-replaced
        assert db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill").count() == 3


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


def test_maybe_backfill_equity_fingerprint_guard(monkeypatch):
    """Startup hook: skips when blank; runs once per (version, start) fingerprint stored
    in app_meta; re-runs when the fingerprint changes (a start change here; a
    _BACKFILL_VERSION bump after a code fix is identical); and a failed/partial run
    (errors, or 0 rows) does NOT mark done, so it retries until a clean run."""
    from app import funding_worker
    from app.models import AppMeta
    calls = []
    result = {"rows": 6, "bybit_events": 3, "hl_points": 3, "errors": []}     # clean by default
    monkeypatch.setattr("app.equity_backfill.backfill_equity",
                        lambda start_ms, now_ms=None: calls.append(start_ms) or dict(result))

    def cfg(start):
        monkeypatch.setattr(funding_worker, "get_settings",
                            lambda: type("S", (), {"equity_backfill_start": start})())

    cfg("")
    funding_worker._maybe_backfill_equity()
    assert calls == []                                       # blank -> skip

    cfg("2026-05-28")
    funding_worker._maybe_backfill_equity()
    funding_worker._maybe_backfill_equity()
    assert len(calls) == 1                                   # clean run -> one-time
    with session_scope() as db:                             # ...recorded a fingerprint marker
        ver = funding_worker._BACKFILL_VERSION
        assert db.get(AppMeta, "equity_backfill").value == f"v{ver}:2026-05-28"

    cfg("2026-01-01")                                        # fingerprint changed -> re-runs
    funding_worker._maybe_backfill_equity()
    assert len(calls) == 2

    result.update(rows=0, errors=["bybit: boom"])           # a failing/partial pull...
    cfg("2026-02-02")
    funding_worker._maybe_backfill_equity()
    funding_worker._maybe_backfill_equity()
    assert len(calls) == 4                                   # ...never marks done -> retries each boot

    result.update(rows=6, errors=[])                         # recovers -> marks done, then stops
    funding_worker._maybe_backfill_equity()
    funding_worker._maybe_backfill_equity()
    assert len(calls) == 5


def test_backfill_caps_current_day_to_the_hour(monkeypatch):
    """The latest (partial) day is stamped on the hour, not at the off-hour run time, so
    the backfill never introduces an off-hour point on the curve."""
    start = _ms(2026, 5, 28)
    now = _ms(2026, 6, 2, 2) + 46 * 60 * 1000                # Jun 2 02:46 UTC (off-hour run)

    class FakeBybit:
        def get_closed_pnl(self, s, e): return [(_ms(2026, 5, 29), 100.0)]
        def get_account_funding(self, s, e): return []

    class FakeHL:
        def get_pnl_history(self): return []

    monkeypatch.setattr(equity_backfill, "get_registry",
                        lambda: type("R", (), {"get": lambda self, n, account="default":
                                               FakeBybit() if n == "bybit" else FakeHL()})())
    backfill_equity(start, now_ms=now)
    with session_scope() as db:
        last = (db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill")
                .order_by(EquitySnapshot.captured_at.desc()).first().captured_at)
    assert last.hour == 2 and last.minute == 0 and last.second == 0   # 02:00, not 02:46


def test_cleanup_offhour_live_snapshots():
    """One-time sweep removes off-hour source='live' rows (pre-hour-align startup
    snapshots) but keeps on-hour live rows and all backfill rows; then it's idempotent."""
    from app import funding_worker
    from app.models import AppMeta
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc),
                              total_pnl=1.0, source="live"))         # on-hour -> keep
        db.add(EquitySnapshot(captured_at=datetime(2026, 6, 8, 20, 14, 33, tzinfo=timezone.utc),
                              total_pnl=2.0, source="live"))         # off-hour -> delete
        db.add(EquitySnapshot(captured_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
                              total_pnl=3.0, source="backfill"))     # backfill -> keep
    funding_worker._maybe_cleanup_offhour_live()
    with session_scope() as db:
        live = db.query(EquitySnapshot).filter(EquitySnapshot.source == "live").all()
        assert len(live) == 1 and live[0].captured_at.minute == 0
        assert db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill").count() == 1
        assert db.get(AppMeta, "offhour_live_cleanup") is not None
    with session_scope() as db:                                 # marker present -> idempotent (won't re-sweep)
        db.add(EquitySnapshot(captured_at=datetime(2026, 6, 8, 21, 5, tzinfo=timezone.utc),
                              total_pnl=4.0, source="live"))
    funding_worker._maybe_cleanup_offhour_live()
    with session_scope() as db:
        assert db.query(EquitySnapshot).filter(EquitySnapshot.source == "live").count() == 2
