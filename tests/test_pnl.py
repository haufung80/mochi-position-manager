"""PnL accounting on StrategyPosition: avg-entry + realized PnL across
increase / partial-close / full-close / cross-zero reversal, both sides."""
from __future__ import annotations
import pytest

from app.db import session_scope
from app.executor import _apply_fill_to_position
from app.models import Position, StrategyPosition


def _fill(side, qty, price, sid="S", exchange="bybit", symbol="BTCUSDT"):
    with session_scope() as db:
        _apply_fill_to_position(db, sid, exchange, symbol, side, qty, price)


def _snapshot(sid="S", exchange="bybit", symbol="BTCUSDT"):
    """(net_qty_base, avg_entry_price, realized_pnl) read inside a session."""
    with session_scope() as db:
        r = (db.query(StrategyPosition)
               .filter_by(strategy_id=sid, exchange=exchange, symbol=symbol).one())
        return r.net_qty_base, r.avg_entry_price, r.realized_pnl


def test_open_from_flat_sets_avg_entry():
    _fill("buy", 1.0, 100.0)
    net, avg, pnl = _snapshot()
    assert net == pytest.approx(1.0)
    assert avg == pytest.approx(100.0)
    assert pnl == pytest.approx(0.0)


def test_increase_weighted_averages_entry():
    _fill("buy", 1.0, 100.0)
    _fill("buy", 1.0, 200.0)
    net, avg, pnl = _snapshot()
    assert net == pytest.approx(2.0)
    assert avg == pytest.approx(150.0)                  # (1*100 + 1*200)/2
    assert pnl == pytest.approx(0.0)


def test_partial_close_realizes_proportionally():
    _fill("buy", 2.0, 100.0)
    _fill("sell", 1.0, 120.0)                          # close half the long at +20
    net, avg, pnl = _snapshot()
    assert net == pytest.approx(1.0)
    assert avg == pytest.approx(100.0)                  # entry unchanged on partial close
    assert pnl == pytest.approx(20.0)                   # (120-100)*1


def test_full_close_realizes_all_and_resets_entry():
    _fill("buy", 2.0, 100.0)
    _fill("sell", 2.0, 110.0)
    net, avg, pnl = _snapshot()
    assert abs(net) < 1e-9
    assert avg == pytest.approx(0.0)
    assert pnl == pytest.approx(20.0)                   # (110-100)*2


def test_cross_zero_reversal_long_to_short():
    _fill("buy", 1.0, 100.0)
    _fill("sell", 3.0, 120.0)                          # close 1 @ +20, open short 2 @ 120
    net, avg, pnl = _snapshot()
    assert net == pytest.approx(-2.0)
    assert avg == pytest.approx(120.0)                  # new short leg entry
    assert pnl == pytest.approx(20.0)                   # only the closed long realizes


def test_short_side_realized_sign():
    _fill("sell", 1.0, 100.0)                          # open short @ 100
    _fill("buy", 1.0, 90.0)                            # cover @ 90 -> short profit +10
    net, avg, pnl = _snapshot()
    assert abs(net) < 1e-9
    assert pnl == pytest.approx(10.0)                   # short: (entry-exit) = 100-90


def test_apply_fill_returns_per_fill_realized_delta():
    """_apply_fill_to_position RETURNS the realized PnL this fill produced (the value
    that gets stamped on Order.realized_pnl for the Recent-orders view): 0 on an
    open/increase, the closed-portion PnL on a reduce/close."""
    with session_scope() as db:                         # open from flat -> nothing closed
        assert _apply_fill_to_position(db, "R", "bybit", "BTCUSDT", "buy", 2.0, 100.0) == pytest.approx(0.0)
    with session_scope() as db:                         # increase -> still nothing closed
        assert _apply_fill_to_position(db, "R", "bybit", "BTCUSDT", "buy", 2.0, 200.0) == pytest.approx(0.0)
    with session_scope() as db:                         # partial close 1 of 4 @ avg 150 -> +30
        assert _apply_fill_to_position(db, "R", "bybit", "BTCUSDT", "sell", 1.0, 180.0) == pytest.approx(30.0)


def test_concurrent_fills_no_lost_update():
    """Regression: concurrent fills on the SAME (strategy, exchange, symbol) must
    not lose updates. The per-strategy ledger is read-modify-write, serialized by
    _LEDGER_LOCK — without it, stale reads silently drop fills (verified to drop
    ~38/40 before the lock)."""
    import threading

    n = 40
    barrier = threading.Barrier(n)
    errors: list = []

    def worker():
        try:
            barrier.wait()                 # release all at once -> max contention
            with session_scope() as db:
                _apply_fill_to_position(db, "C", "bybit", "BTCUSDT", "buy", 1.0, 100.0)
        except Exception as e:             # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    net, _, _ = _snapshot(sid="C")
    assert net == pytest.approx(float(n))  # every fill counted, none lost


def test_aggregate_position_accumulates_across_strategies():
    _fill("buy", 1.0, 100.0, sid="A")
    _fill("buy", 2.0, 100.0, sid="B")
    with session_scope() as db:
        pos = db.query(Position).filter_by(exchange="bybit", symbol="BTCUSDT").one()
        agg = pos.net_qty_base
    assert agg == pytest.approx(3.0)                    # aggregate nets both
    assert _snapshot(sid="A")[0] == pytest.approx(1.0)
    assert _snapshot(sid="B")[0] == pytest.approx(2.0)
