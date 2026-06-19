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
from app.routes.dashboard import (_by_strategy_totals, _equity_chart_payload, _equity_curve,
                                   _equity_dataset, _equity_metrics, _equity_series,
                                   _load_snapshots, _load_strategy_snapshots, _performance)
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


def test_write_equity_snapshot_captures_by_strategy(tmp_path, stub_exchange):
    """The snapshot ALSO stores a per-strategy breakdown (mirrors by_exchange); the
    per-strategy values sum to the directional (ex-funding) total."""
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n"
                              "  S2:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with session_scope() as db:
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "sell", 1.0, 150.0)   # +50
        _apply_fill_to_position(db, "S2", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
        _apply_fill_to_position(db, "S2", "bybit", "BTCUSDT", "sell", 1.0, 110.0)   # +10
    assert write_equity_snapshot(router) is True
    with session_scope() as db:
        by_strat = json.loads(db.query(EquitySnapshot).one().by_strategy)
        per_strat_sum = sum(r["total"] for r in _performance(db, router)["per_strategy"])
    assert by_strat == {"S1": pytest.approx(50.0), "S2": pytest.approx(10.0)}
    assert sum(by_strat.values()) == pytest.approx(per_strat_sum)   # Σ == directional total


def test_write_equity_snapshot_by_strategy_includes_unrealized(tmp_path, stub_exchange):
    """The captured by_strategy value INCLUDES live unrealized MTM (the user's core
    requirement): a strategy left with an OPEN position whose mark != entry has
    by_strategy[sid] = realized + unrealized − commission, i.e. it moves with the mark."""
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with session_scope() as db:                       # open +2 @ 100, no close -> unrealized only
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 2.0, 100.0)
    stub_exchange.positions["BTCUSDT"] = (2.0, 110.0)  # exchange mark 110 -> +20 unrealized
    assert write_equity_snapshot(router) is True
    with session_scope() as db:
        by_strat = json.loads(db.query(EquitySnapshot).one().by_strategy)
    assert by_strat["S1"] == pytest.approx(20.0)       # 2 * (110 - 100); realized 0, commission 0


def test_load_strategy_snapshots_skips_empty_and_sums():
    """_load_strategy_snapshots returns (ts, Σ_strategies, by_strategy) and SKIPS rows
    with no per-strategy breakdown (backfilled / pre-feature) so the curve has no
    flat-zero prefix."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=now - timedelta(hours=3), total_pnl=5.0,
                              by_strategy="{}"))                       # empty -> skipped
        db.add(EquitySnapshot(captured_at=now - timedelta(hours=1), total_pnl=30.0,
                              by_strategy=json.dumps({"S1": 18.0, "S2": 12.0})))
    with session_scope() as db:
        rows = _load_strategy_snapshots(db)
    assert len(rows) == 1                                # the empty-by_strategy row is skipped
    _, agg, by = rows[0]
    assert agg == pytest.approx(30.0) and by == {"S1": 18.0, "S2": 12.0}


def test_load_strategy_snapshots_tolerates_malformed_value():
    """A non-numeric/None value in by_strategy must NOT TypeError out of sum() and 500 the
    page — the bad row is dropped (coerced like _load_snapshots), good rows still load."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=now - timedelta(hours=2), total_pnl=1.0,
                              by_strategy='{"S1": null}'))               # malformed -> dropped
        db.add(EquitySnapshot(captured_at=now - timedelta(hours=1), total_pnl=9.0,
                              by_strategy=json.dumps({"S1": 9.0})))
    with session_scope() as db:
        rows = _load_strategy_snapshots(db)                              # must not raise
    assert len(rows) == 1 and rows[0][1] == pytest.approx(9.0)


def test_equity_series_per_strategy_lines_and_live_tip():
    """The per-strategy snapshots feed the SAME _equity_series: one line per strategy +
    a Σ 'Total', each tipped to the live value."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=now - timedelta(hours=2), total_pnl=30.0,
                              by_strategy=json.dumps({"S1": 18.0, "S2": 12.0})))
    live = {"S1": 20.0, "S2": 13.0}
    with session_scope() as db:
        s = _equity_series(_load_strategy_snapshots(db), None, sum(live.values()), live)
    assert set(s) == {"Total", "S1", "S2"}
    assert s["Total"][-1][1] == pytest.approx(33.0)      # Σ live tip
    assert s["S1"][-1][1] == pytest.approx(20.0)


def test_equity_series_per_strategy_no_history_is_empty():
    """With NO populated by_strategy snapshots the per-strategy curve is the genuine
    empty state (forward-only) — the page shows the 'builds forward' message."""
    assert _equity_series([], timedelta(hours=24), 5.0, {"S1": 5.0}) == {}


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
        snaps = _load_snapshots(db)
        wide = _equity_series(snaps, None, live_total=33.0, live_by_ex=live_by_ex)         # All
        day = _equity_series(snaps, timedelta(hours=24), live_total=33.0, live_by_ex=live_by_ex)
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


def test_equity_metrics_capital_base_percentages():
    """With a capital base: return-% (on capital), period-return-%, and drawdown-%
    measured against peak EQUITY (capital + peak PnL). No base -> no %-fields."""
    series = [(None, 0.0), (None, 100.0), (None, 60.0), (None, 200.0)]   # peak 100, dd 40, end 200
    m = _equity_metrics(series, capital_base=2000.0)
    assert m["capital_base"] == 2000.0
    assert m["return_pct"] == pytest.approx(200.0 / 2000.0 * 100)        # 10%
    assert m["period_return_pct"] == pytest.approx(200.0 / 2000.0 * 100)
    assert m["max_drawdown_pct"] == pytest.approx(40.0 / (2000.0 + 100.0) * 100)  # vs peak equity
    assert m["sharpe"] is None                                           # < 8 points
    assert "return_pct" not in _equity_metrics(series)                   # no base -> no %


def test_equity_chart_payload_shape():
    """The ECharts payload (the /performance curve path) carries each series with Total
    drawn LAST (on top, green/red by sign; venues branded) as [epoch_ms, value] points,
    plus the capital base; nothing to plot -> None."""
    t1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, tzinfo=timezone.utc)
    p = _equity_chart_payload({"bybit": [(t1, 6.0), (t2, 18.0)],
                               "Total": [(t1, 10.0), (t2, 30.0)]}, capital_base=2000.0)
    assert [s["name"] for s in p["series"]][-1] == "Total"     # Total ordered last -> drawn on top
    total = next(s for s in p["series"] if s["name"] == "Total")
    assert total["is_total"] and total["color"] == "#4ade80"   # positive total -> green
    assert total["last"] == pytest.approx(30.0)
    assert total["data"][-1] == [int(t2.timestamp() * 1000), 30.0]   # [epoch_ms, value]
    assert p["capital_base"] == pytest.approx(2000.0)
    assert _equity_chart_payload({}) is None                   # nothing to plot
    assert _equity_chart_payload({"Total": [(t1, -5.0)]})["series"][0]["color"] == "#f87171"  # neg -> red


def test_equity_metrics_sharpe_with_enough_points():
    """Sharpe (est.) is computed from >= 8 DAILY returns (one equity point per day)."""
    base = datetime(2026, 5, 18, tzinfo=timezone.utc)
    vals = [0, 10, 5, 15, 12, 22, 18, 28, 34, 30, 40]   # 11 daily points -> 10 daily returns
    series = [(base + timedelta(days=i), float(v)) for i, v in enumerate(vals)]
    m = _equity_metrics(series, capital_base=1000.0)
    assert isinstance(m["sharpe"], float)


def test_equity_metrics_apr():
    """APR annualizes return-on-capital over the data's actual span; a sub-0.5d span
    has no APR (avoids absurd annualization in the first hour)."""
    base = datetime(2026, 5, 20, tzinfo=timezone.utc)
    series = [(base + timedelta(days=i), float(i * 10)) for i in range(11)]   # 0..100 over 10 days
    m = _equity_metrics(series, capital_base=1000.0)
    assert m["return_pct"] == pytest.approx(10.0)                 # 100 / 1000
    assert m["apr_days"] == pytest.approx(10.0)
    assert m["apr"] == pytest.approx(0.1 * 365 / 10 * 100)        # 365% annualized
    short = [(base, 0.0), (base + timedelta(hours=2), 5.0)]       # 2h span
    assert "apr" not in _equity_metrics(short, capital_base=1000.0)


def test_equity_dataset_caches_within_ttl(tmp_path, stub_exchange, monkeypatch):
    """The dataset (snapshots + perf + per-strategy snapshots + exec-quality) is cached:
    a 2nd call within the TTL re-runs nothing (incl. the full orders scan); force=True
    rebuilds it. So a window switch / reload re-fetches none of the per-strategy work."""
    from app.routes import dashboard
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    with session_scope() as db:
        db.add(EquitySnapshot(captured_at=datetime(2026, 6, 1, tzinfo=timezone.utc), total_pnl=5.0))
    calls = {"perf": 0, "exec": 0}
    real_perf, real_exec = dashboard._performance, dashboard._execution_quality_by_strategy
    monkeypatch.setattr(dashboard, "_performance",
                        lambda db, r: calls.__setitem__("perf", calls["perf"] + 1) or real_perf(db, r))
    monkeypatch.setattr(dashboard, "_execution_quality_by_strategy",
                        lambda db: calls.__setitem__("exec", calls["exec"] + 1) or real_exec(db))
    dashboard._clear_equity_cache()
    with session_scope() as db:
        s1, p1, ss1, eq1 = dashboard._equity_dataset(db, router)
        s2, p2, ss2, eq2 = dashboard._equity_dataset(db, router)      # cached
    assert calls["perf"] == 1 and calls["exec"] == 1                 # exec scan runs ONCE, not per call
    assert s1 is s2 and p1 is p2 and ss1 is ss2 and eq1 is eq2        # all four reused
    with session_scope() as db:
        dashboard._equity_dataset(db, router, force=True)            # ?refresh -> rebuild
    assert calls["perf"] == 2 and calls["exec"] == 2


def test_equity_series_empty_window_keeps_live_tip():
    """A window with no snapshots in it still shows the live point (+ per-venue tips),
    so the chart and metrics aren't blank when there IS a real current PnL (short
    windows, a stalled worker, a fresh deploy before the first snapshot)."""
    now = datetime.now(timezone.utc)
    snaps = [(now - timedelta(days=10), 10.0, {"bybit": 10.0})]      # only an out-of-window point
    s = _equity_series(snaps, timedelta(hours=24), live_total=33.0,
                       live_by_ex={"bybit": 20.0, "hyperliquid": 13.0})
    assert s["Total"][-1][1] == pytest.approx(33.0)                  # live tip kept, not dropped
    assert s["bybit"][-1][1] == pytest.approx(20.0)
    assert s["hyperliquid"][-1][1] == pytest.approx(13.0)
    assert _equity_chart_payload(s) is not None                      # renders a chart...
    assert _equity_metrics(s["Total"])["current"] == pytest.approx(33.0)   # ...and metrics
    assert _equity_series(snaps, timedelta(hours=24)) == {}          # but no live value -> empty


def test_write_equity_snapshot_clears_dataset_cache(tmp_path, stub_exchange):
    """Writing a fresh snapshot invalidates the cached dataset, so the next render sees
    the new point instead of a (<=TTL) stale one — the worker's hourly point shows up
    without waiting out the cache."""
    from app.routes import dashboard
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    dashboard._clear_equity_cache()
    with session_scope() as db:
        snaps1, *_ = dashboard._equity_dataset(db, router)    # warm the cache (0 snapshots)
    assert snaps1 == []
    write_equity_snapshot(router)                             # persists a point AND clears the cache
    with session_scope() as db:
        snaps2, *_ = dashboard._equity_dataset(db, router)    # cache cleared -> rebuilt with the new row
    assert len(snaps2) == 1


def test_snapshot_pinned_to_top_of_hour(tmp_path, stub_exchange):
    """write_equity_snapshot stamps the row at the given top-of-hour timestamp, so the
    hourly curve points land on HH:00; the helpers expose that boundary."""
    from app.funding_worker import write_equity_snapshot, _hour_floor, _seconds_to_next_hour
    top = _hour_floor()
    assert top.minute == 0 and top.second == 0 and top.microsecond == 0
    assert 1 <= _seconds_to_next_hour() <= 3600
    router = _router(tmp_path, "strategies:\n  S1:\n    base_asset: BTC\n    venues:\n      bybit: true\n")
    assert write_equity_snapshot(router, captured_at=top) is True
    with session_scope() as db:
        cap = db.query(EquitySnapshot).order_by(EquitySnapshot.captured_at.desc()).first().captured_at
    assert cap.minute == 0 and cap.second == 0           # stamped on the hour
