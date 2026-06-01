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
    # Response acks fast with the dispatched venues; order RESULTS land in the
    # DB via the background task (which the TestClient runs before returning).
    assert body["status"] == "accepted"
    assert body["venues"] == ["bybit"]
    assert "orders" not in body  # results are async now, not echoed in the response

    # The stub exchange was called with the alert's `quantity` (0.05)
    qty_calls = [c[3] for c in stub_exchange.calls if c[0] == "market"]
    assert qty_calls == [0.05]

    with session_scope() as db:
        assert db.query(Alert).count() == 1
        order = db.query(Order).one()
        assert (order.exchange, order.symbol) == ("bybit", "BTCUSDT")
        assert order.status == "success"
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
    assert set(body["venues"]) == {"bybit", "hyperliquid"}

    with session_scope() as db:
        assert db.query(Alert).count() == 1
        assert db.query(Order).count() == 2
        orders = {o.exchange: o for o in db.query(Order).all()}
        assert orders["bybit"].symbol == "ETHUSDT"
        assert orders["hyperliquid"].symbol == "ETH"
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
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    # The order failed in the background task and was marked for retry.
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


def test_strategy_positions_tracked(strategies_yaml, stub_exchange, silent_notifier):
    """Per-strategy net is derived from successful orders, with a per-venue
    breakdown + a strategy total. TEST_MULTI fans to bybit + hyperliquid; the
    stub fills 0.001 base @ 50000 per venue (from conftest)."""
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_MULTI", quantity=0.05))
    r = c.get("/strategy-positions")
    assert r.status_code == 200
    by_id = {s["strategy_id"]: s for s in r.json()}
    assert "TEST_MULTI" in by_id
    s = by_id["TEST_MULTI"]
    venues = {v["exchange"]: v for v in s["venues"]}
    assert venues["bybit"]["symbol"] == "ETHUSDT"
    assert venues["hyperliquid"]["symbol"] == "ETH"
    assert venues["bybit"]["net_qty_base"] == 0.001
    assert venues["hyperliquid"]["net_qty_base"] == 0.001
    assert abs(s["net_base"] - 0.002) < 1e-9
    assert abs(s["net_usd"] - 100.0) < 1e-6  # 0.002 * 50000


def test_strategy_positions_net_of_buy_and_sell(strategies_yaml, stub_exchange, silent_notifier):
    """A sell nets against a prior buy in the per-strategy view."""
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", action="buy",
                                                alert_id="b1", quantity=0.05))
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", action="sell",
                                                alert_id="s1", quantity=0.05))
    by_id = {s["strategy_id"]: s for s in c.get("/strategy-positions").json()}
    # stub fills 0.001 each way -> net 0 for the strategy
    assert abs(by_id["TEST_BTC"]["net_base"]) < 1e-9


def test_sync_positions_rebaselines_to_exchange(strategies_yaml, stub_exchange, silent_notifier):
    """Admin sync sets each strategy's ledger to its LIVE exchange position.
    BTCUSDT is unique to TEST_BTC (synced); ETHUSDT is shared by TEST_MULTI +
    TEST_DISABLED (skipped — can't attribute an aggregate to one strategy)."""
    c = _client(strategies_yaml)
    stub_exchange.positions["BTCUSDT"] = (0.05, 60000.0)   # pretend the exchange holds this
    r = c.post("/admin/strategies/sync-positions", data={"secret": SECRET})
    assert r.status_code == 200, r.text
    by_id = {s["strategy_id"]: s for s in c.get("/strategy-positions").json()}
    assert "TEST_BTC" in by_id
    v = by_id["TEST_BTC"]["venues"][0]
    assert (v["exchange"], v["symbol"]) == ("bybit", "BTCUSDT")
    assert abs(v["net_qty_base"] - 0.05) < 1e-9
    assert abs(v["net_qty_usd"] - 3000.0) < 1e-6          # 0.05 * 60000
    assert "TEST_MULTI" not in by_id                       # shared ETHUSDT -> skipped


def test_sync_positions_requires_secret(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/admin/strategies/sync-positions", data={"secret": "wrong"})
    assert r.status_code == 401


def test_dashboard_renders_per_strategy_section_with_data(strategies_yaml, stub_exchange,
                                                          silent_notifier, monkeypatch):
    """Render the dashboard with a non-empty per-strategy ledger — exercises the
    flat-detection / venue-breakdown Jinja so a template bug can't slip through."""
    from app import network
    monkeypatch.setattr(network, "get_outbound_ip", lambda force_refresh=False: "1.2.3.4")
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_MULTI", quantity=0.05))
    r = c.get("/")
    assert r.status_code == 200
    assert "Net positions by strategy" in r.text
    assert "TEST_MULTI" in r.text
    assert "Total net exposure" in r.text
