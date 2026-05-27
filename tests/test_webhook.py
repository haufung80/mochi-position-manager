from fastapi.testclient import TestClient

from app.main import app
from app.routing import StrategyRouter
from app.db import session_scope
from app.models import Alert, Order, Position
from app.schemas import OrderResult


def _client(strategies_yaml):
    app.state.strategy_router = StrategyRouter(strategies_yaml)
    return TestClient(app)


def test_rejects_bad_secret(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json={
        "secret": "wrong", "strategy_id": "TEST_BTC", "action": "buy", "alert_id": "a1",
    })
    assert r.status_code == 401


def test_rejects_invalid_action(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "moonshot", "alert_id": "a1",
    })
    assert r.status_code == 422


def test_happy_path_creates_alert_order_and_position(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "buy", "alert_id": "a1",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["order_status"] == "success"

    with session_scope() as db:
        assert db.query(Alert).count() == 1
        order = db.query(Order).one()
        assert order.status == "success"
        assert order.attempts == 1
        pos = db.query(Position).one()
        assert pos.exchange == "bybit"
        assert pos.symbol == "BTCUSDT"
        assert pos.net_qty_base > 0

    # second identical hit -> duplicate
    r2 = c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "buy", "alert_id": "a1",
    })
    assert r2.json()["status"] == "duplicate"
    with session_scope() as db:
        assert db.query(Alert).count() == 1  # not double-recorded
        assert db.query(Order).count() == 1  # no extra order


def test_sell_decrements_position(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "buy", "alert_id": "buy1",
    })
    c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "sell", "alert_id": "sell1",
    })
    with session_scope() as db:
        pos = db.query(Position).one()
        assert pos.net_qty_base == 0.0  # +0.001 - 0.001


def test_close_action_zeroes_position(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "buy", "alert_id": "buy1",
    })
    c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "close", "alert_id": "close1",
    })
    with session_scope() as db:
        pos = db.query(Position).one()
        assert pos.net_qty_base == 0.0


def test_unknown_strategy_logs_but_skips(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "DOES_NOT_EXIST",
        "action": "buy", "alert_id": "a1",
    })
    body = r.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "unknown_strategy"
    with session_scope() as db:
        assert db.query(Alert).count() == 1  # alert IS logged
        assert db.query(Order).count() == 0  # but no order placed


def test_disabled_strategy_logs_but_skips(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_DISABLED",
        "action": "buy", "alert_id": "a1",
    })
    assert r.json()["status"] == "skipped"
    assert r.json()["reason"] == "disabled_strategy"


def test_failed_order_marks_retrying(strategies_yaml, stub_exchange, silent_notifier):
    stub_exchange.next_result = OrderResult(success=False, error_message="exchange down")
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json={
        "secret": "test-secret-12345", "strategy_id": "TEST_BTC",
        "action": "buy", "alert_id": "a1",
    })
    assert r.json()["order_status"] == "retrying"
    with session_scope() as db:
        order = db.query(Order).one()
        assert order.status == "retrying"
        assert order.attempts == 1
        assert order.next_retry_at is not None
        assert "exchange down" in order.error_message
        # no position movement on failure
        assert db.query(Position).count() == 0


def test_health_endpoint(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
