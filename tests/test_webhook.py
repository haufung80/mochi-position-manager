"""End-to-end tests for POST /webhook/tradingview.

These all stub the exchange adapter so no real orders fire; they exercise
the dedup / fan-out / failure logic of the middleware itself."""
from __future__ import annotations
from fastapi.testclient import TestClient

from app.main import app
from app.routing import StrategyRouter
from app.db import session_scope
from app.models import Alert, Order, Position
from app.schemas import OrderResult


SECRET = "test-secret-12345"


def _client(strategies_yaml):
    app.state.strategy_router = StrategyRouter(strategies_yaml)
    return TestClient(app)


def _payload(strategy_id: str, *, action: str = "buy", alert_id: str = "a1",
             quantity: float | None = 0.001) -> dict:
    """Build a TV alert body. `quantity` is in BASE-ASSET units (e.g. 0.001 BTC)."""
    body = {"secret": SECRET, "strategy_id": strategy_id, "action": action,
            "alert_id": alert_id}
    if quantity is not None:
        body["quantity"] = quantity
    return body


def test_rejects_bad_secret(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    body = _payload("TEST_BTC")
    body["secret"] = "wrong"
    r = c.post("/webhook/tradingview", json=body)
    assert r.status_code == 401


def test_rejects_invalid_action(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    body = _payload("TEST_BTC", action="moonshot")
    r = c.post("/webhook/tradingview", json=body)
    assert r.status_code == 422


def test_rejects_missing_qty(strategies_yaml, stub_exchange, silent_notifier):
    """quantity is required on every alert."""
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_BTC", quantity=None))
    assert r.status_code == 422
    assert "quantity" in r.text


def test_rejects_zero_qty(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_BTC", quantity=0))
    assert r.status_code == 422


def test_rejects_close_action(strategies_yaml, stub_exchange, silent_notifier):
    """TradingView only sends buy/sell — close* actions should be rejected
    at the schema layer so we don't silently process malformed payloads."""
    c = _client(strategies_yaml)
    body = _payload("TEST_BTC", action="close")
    r = c.post("/webhook/tradingview", json=body)
    assert r.status_code == 422


def test_sell_against_long_closes_it(strategies_yaml, stub_exchange, silent_notifier):
    """A sell after a buy of equal size returns the position to flat
    (one-way mode behavior). Replaces the old explicit close action path."""
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", action="buy",
                                                alert_id="buy1"))
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", action="sell",
                                                alert_id="sell1"))
    with session_scope() as db:
        pos = db.query(Position).one()
        assert pos.net_qty_base == 0.0


def test_happy_path_single_venue(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_BTC", quantity=0.05))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert len(body["orders"]) == 1
    assert body["orders"][0]["exchange"] == "bybit"
    assert body["orders"][0]["symbol"] == "BTCUSDT"
    assert body["orders"][0]["status"] == "success"

    # The stub exchange was called with the alert's `quantity` (0.05)
    qty_calls = [c[3] for c in stub_exchange.calls if c[0] == "market"]
    assert qty_calls == [0.05]

    with session_scope() as db:
        assert db.query(Alert).count() == 1
        order = db.query(Order).one()
        # order.qty_base after fill = whatever the exchange actually filled
        # (stub returns its hardcoded filled_qty_base=0.001 from conftest)
        assert order.qty_base == 0.001
        pos = db.query(Position).one()
        assert (pos.exchange, pos.symbol) == ("bybit", "BTCUSDT")
        assert pos.net_qty_base > 0


def test_alert_payload_drives_qty(strategies_yaml, stub_exchange, silent_notifier):
    """Different alerts can fire different quantities against the same strategy.
    The stub returns its own filled_qty_base; what we're checking is that the
    middleware passes through the alert's `quantity` to the exchange adapter."""
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", alert_id="a1",
                                                quantity=0.002))
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", alert_id="a2",
                                                quantity=0.005))
    # Verify the adapter was called with each alert's quantity
    qty_calls = [c[3] for c in stub_exchange.calls if c[0] == "market"]
    assert qty_calls == [0.002, 0.005]


def test_fan_out_creates_one_order_per_enabled_venue(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_MULTI", quantity=0.05))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert len(body["orders"]) == 2
    by_ex = {o["exchange"]: o for o in body["orders"]}
    assert by_ex["bybit"]["symbol"] == "ETHUSDT"
    assert by_ex["hyperliquid"]["symbol"] == "ETH"

    with session_scope() as db:
        assert db.query(Alert).count() == 1
        assert db.query(Order).count() == 2
        positions = {(p.exchange, p.symbol): p for p in db.query(Position).all()}
        assert ("bybit", "ETHUSDT") in positions
        assert ("hyperliquid", "ETH") in positions


def test_duplicate_alert_does_not_create_extra_orders(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    body = _payload("TEST_MULTI", alert_id="dup1")
    assert c.post("/webhook/tradingview", json=body).json()["status"] == "accepted"
    assert c.post("/webhook/tradingview", json=body).json()["status"] == "duplicate"

    with session_scope() as db:
        assert db.query(Alert).count() == 1
        assert db.query(Order).count() == 2  # first alert's 2 venues only


def test_unknown_strategy_logs_but_skips(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("DOES_NOT_EXIST"))
    body = r.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "unknown_strategy"
    with session_scope() as db:
        assert db.query(Alert).count() == 1
        assert db.query(Order).count() == 0


def test_strategy_with_all_venues_disabled_skips(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_DISABLED"))
    body = r.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "no_enabled_venues"


def test_failed_order_marks_retrying(strategies_yaml, stub_exchange, silent_notifier):
    stub_exchange.next_result = OrderResult(success=False, error_message="exchange down")
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_BTC"))
    body = r.json()
    assert body["orders"][0]["status"] == "retrying"
    with session_scope() as db:
        order = db.query(Order).one()
        assert order.status == "retrying"
        assert order.next_retry_at is not None
        assert db.query(Position).count() == 0


def test_health_endpoint(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_dashboard_renders_with_strategies(strategies_yaml, stub_exchange,
                                            silent_notifier, monkeypatch):
    """Regression: ensure the dashboard template renders without crashing
    when there ARE strategies. Catches schema drift (e.g. template references
    a field that's been removed from the dataclass)."""
    from app import network
    monkeypatch.setattr(network, "get_outbound_ip", lambda force_refresh=False: "1.2.3.4")

    c = _client(strategies_yaml)
    r = c.get("/")
    assert r.status_code == 200
    assert "TEST_BTC" in r.text
    assert "TEST_MULTI" in r.text
    assert "1.2.3.4" in r.text


def test_egress_ip_endpoint(strategies_yaml, stub_exchange, silent_notifier, monkeypatch):
    from app import network
    monkeypatch.setattr(network, "get_outbound_ip", lambda force_refresh=False: "5.6.7.8")
    c = _client(strategies_yaml)
    r = c.get("/network/egress-ip")
    assert r.status_code == 200
    assert r.json() == {"egress_ip": "5.6.7.8"}


def test_egress_ip_endpoint_handles_lookup_failure(strategies_yaml, stub_exchange,
                                                    silent_notifier, monkeypatch):
    from app import network
    monkeypatch.setattr(network, "get_outbound_ip", lambda force_refresh=False: None)
    c = _client(strategies_yaml)
    r = c.get("/network/egress-ip")
    assert r.status_code == 200
    assert r.json() == {"egress_ip": None}
