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


def test_missing_qty_ok_for_managed(strategies_yaml, stub_exchange, silent_notifier):
    """Managed (sar=false) strategies size themselves — quantity is optional."""
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_MANAGED", quantity=None))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"


def test_missing_qty_rejected_for_sar(strategies_yaml, stub_exchange, silent_notifier):
    """Alert-driven (sar=true) strategies require a quantity."""
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_SAR", quantity=None))
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
    # TEST_SAR is alert-driven (sar=true), so the alert's quantity flows through.
    c = _client(strategies_yaml)
    r = c.post("/webhook/tradingview", json=_payload("TEST_SAR", quantity=0.05))
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
        assert (order.exchange, order.symbol) == ("bybit", "SOLUSDT")
        assert order.status == "success"
        # order.qty_base after fill = whatever the exchange actually filled
        # (stub returns its hardcoded filled_qty_base=0.001 from conftest)
        assert order.qty_base == 0.001
        pos = db.query(Position).one()
        assert (pos.exchange, pos.symbol) == ("bybit", "SOLUSDT")
        assert pos.net_qty_base > 0


def test_alert_payload_drives_qty(strategies_yaml, stub_exchange, silent_notifier):
    """sar=true strategies are alert-driven: each alert's `quantity` flows to the
    exchange adapter unchanged (the stub returns its own filled_qty_base)."""
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_SAR", alert_id="a1",
                                                quantity=0.002))
    c.post("/webhook/tradingview", json=_payload("TEST_SAR", alert_id="a2",
                                                quantity=0.005))
    # Verify the adapter was called with each alert's quantity
    qty_calls = [c[3] for c in stub_exchange.calls if c[0] == "market"]
    assert qty_calls == [0.002, 0.005]


def test_orders_fire_at_2x_leverage(strategies_yaml, stub_exchange, silent_notifier):
    """The account is run at 2x. Each adapter sets leverage on the symbol right
    before the market order, so the value must reach market_order(). Asserted as
    a literal (not the DEFAULT_LEVERAGE constant) so a silent change to it fails
    here and forces a deliberate decision."""
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", quantity=0.001))
    lev_calls = [c[4] for c in stub_exchange.calls if c[0] == "market"]
    assert lev_calls == [2.0]


# ---------- managed sizing (sar=false) end-to-end ----------

def test_managed_open_sizes_from_position_size(strategies_yaml, stub_exchange, silent_notifier):
    """sar=false + position_size: flat -> open a sized order (notional / price),
    NOT the alert's quantity."""
    c = _client(strategies_yaml)
    stub_exchange.prices["XRPUSDT"] = 0.5
    stub_exchange.step_sizes["XRPUSDT"] = 0.1
    r = c.post("/webhook/tradingview",
               json=_payload("TEST_MANAGED", action="buy", quantity=None))
    assert r.json()["status"] == "accepted"
    qty_calls = [call[3] for call in stub_exchange.calls if call[0] == "market"]
    assert qty_calls == [2000.0]                        # 1000 / 0.5


def test_managed_paper_uses_min_unit_and_warns(strategies_yaml, stub_exchange, silent_notifier):
    """sar=false without position_size: paper mode -> 1 step + Telegram warning."""
    c = _client(strategies_yaml)
    stub_exchange.step_sizes["BTCUSDT"] = 0.001
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", action="buy", quantity=None))
    qty_calls = [call[3] for call in stub_exchange.calls if call[0] == "market"]
    assert qty_calls == [0.001]
    assert any(call[0] == "paper_trade" for call in silent_notifier.calls)


def test_managed_double_down_rejected(strategies_yaml, stub_exchange, silent_notifier):
    """In a position, a same-direction signal is rejected (audit row, no fill)."""
    c = _client(strategies_yaml)
    stub_exchange.prices["XRPUSDT"] = 0.5
    stub_exchange.step_sizes["XRPUSDT"] = 0.1
    c.post("/webhook/tradingview",
           json=_payload("TEST_MANAGED", action="buy", alert_id="o1", quantity=None))
    c.post("/webhook/tradingview",
           json=_payload("TEST_MANAGED", action="buy", alert_id="o2", quantity=None))
    with session_scope() as db:
        statuses = sorted(o.status for o in db.query(Order).all())
    assert statuses == ["rejected", "success"]
    assert any(call[0] == "order_rejected" for call in silent_notifier.calls)


def test_managed_opposite_signal_closes_to_flat(strategies_yaml, stub_exchange, silent_notifier):
    """In a long, an opposite signal closes to flat using the ledger net (actual
    fill), not the configured size."""
    from app.models import StrategyPosition
    c = _client(strategies_yaml)
    stub_exchange.prices["XRPUSDT"] = 0.5
    stub_exchange.step_sizes["XRPUSDT"] = 0.1
    c.post("/webhook/tradingview",
           json=_payload("TEST_MANAGED", action="buy", alert_id="o1", quantity=None))
    c.post("/webhook/tradingview",
           json=_payload("TEST_MANAGED", action="sell", alert_id="o2", quantity=None))
    with session_scope() as db:
        row = db.query(StrategyPosition).filter_by(strategy_id="TEST_MANAGED").one()
        assert abs(row.net_qty_base) < 1e-9             # back to flat
    market_qtys = [call[3] for call in stub_exchange.calls if call[0] == "market"]
    assert market_qtys[0] == 2000.0                     # open: sized from budget
    assert market_qtys[1] == 0.001                      # close: abs(net) = stub fill


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
    assert "viewport-fit=cover" in r.text          # mobile/Safari responsive
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
    vbyx = {v["exchange"]: v for v in by_id["TEST_BTC"]["venues"]}
    assert vbyx["bybit"]["symbol"] == "BTCUSDT"
    assert abs(vbyx["bybit"]["net_qty_base"] - 0.05) < 1e-9
    assert abs(vbyx["bybit"]["net_qty_usd"] - 3000.0) < 1e-6   # 0.05 * 60000
    # TEST_MULTI shares ETHUSDT (skipped by sync) -> listed (configured) but flat
    assert abs(by_id["TEST_MULTI"]["net_base"]) < 1e-9


def test_sync_positions_requires_secret(strategies_yaml, stub_exchange, silent_notifier):
    c = _client(strategies_yaml)
    r = c.post("/admin/strategies/sync-positions", data={"secret": "wrong"})
    assert r.status_code == 401


def test_qty_formatter_strips_trailing_zeros():
    from app.routes.dashboard import _fmt_qty
    assert _fmt_qty(0.29) == "0.29"
    assert _fmt_qty(-0.003) == "-0.003"
    assert _fmt_qty(5.8) == "5.8"
    assert _fmt_qty(-2.9) == "-2.9"
    assert _fmt_qty(0.0) == "0"
    assert _fmt_qty(0.002) == "0.002"


def test_qty_formatter_normalizes_negative_zero():
    """Float dust that rounds to 0 at 8 dp must render as a clean '0', not '-0'."""
    from app.routes.dashboard import _fmt_qty
    assert _fmt_qty(-1e-9) == "0"
    assert _fmt_qty(-0.0) == "0"
    assert _fmt_qty(1e-12) == "0"
    assert _fmt_qty(-1e-12) == "0"


def test_usd_formatter_clamps_negative_zero():
    """Sub-cent dust must render '0.00' (no minus); real amounts pass through."""
    from app.routes.dashboard import _fmt_usd
    assert _fmt_usd(-2.8e-14) == "0.00"
    assert _fmt_usd(-0.0) == "0.00"
    assert _fmt_usd(0.0) == "0.00"
    assert _fmt_usd(-0.004) == "0.00"     # sub-cent -> clamped
    assert _fmt_usd(-0.006) == "-0.01"    # rounds to a cent -> kept
    assert _fmt_usd(-214.79) == "-214.79"
    assert _fmt_usd(348.85) == "348.85"


def test_when_filter_renders_in_display_timezone():
    """Stored UTC timestamps render in the configured display tz (America/Toronto
    by default). Storage stays UTC; only the dashboard view shifts. Fixed
    instants keep this DST-stable."""
    from datetime import datetime, timezone
    from app.routes.dashboard import _fmt_when

    # Summer -> Toronto is EDT (UTC-4): 15:00:17 UTC == 11:00:17 EDT
    summer = datetime(2026, 6, 2, 15, 0, 17, tzinfo=timezone.utc)
    assert _fmt_when(summer) == "2026-06-02 11:00:17 EDT"
    # Winter -> Toronto is EST (UTC-5): 15:00:00 UTC == 10:00:00 EST
    winter = datetime(2026, 1, 15, 15, 0, 0, tzinfo=timezone.utc)
    assert _fmt_when(winter) == "2026-01-15 10:00:00 EST"
    # SQLite drops tzinfo -> a naive value is assumed UTC
    assert _fmt_when(datetime(2026, 6, 2, 15, 0, 17)) == "2026-06-02 11:00:17 EDT"
    # custom format + None
    assert _fmt_when(summer, "%H:%M:%S") == "11:00:17"
    assert _fmt_when(None) == ""


def test_net_positions_table_no_negative_zero_and_dims_flat(strategies_yaml, stub_exchange,
                                                            silent_notifier, monkeypatch):
    """The per-symbol Net positions table must not render '$-0.00' for a dust
    residual, and the dust row must be dimmed (color:#666)."""
    import re
    from app import network
    from app.executor import _apply_fill_to_position
    monkeypatch.setattr(network, "get_outbound_ip", lambda force_refresh=False: "1.2.3.4")

    # Buy then sell a hair more -> a tiny negative dust residue in the Position ledger.
    with session_scope() as db:
        _apply_fill_to_position(db, "X", "bybit", "BNBUSDT", "buy", 0.05, 678.1)
    with session_scope() as db:
        _apply_fill_to_position(db, "X", "bybit", "BNBUSDT", "sell", 0.05000000001, 678.1)

    c = _client(strategies_yaml)
    r = c.get("/")
    assert r.status_code == 200
    assert "-0.00" not in r.text                       # no negative-zero dollar anywhere
    # the BNBUSDT row's opening <tr> carries the dim style
    m = re.search(r"(<tr[^>]*>)(?:(?!</tr>).)*?BNBUSDT", r.text, re.S)
    assert m and "color:#666" in m.group(1)


def test_venue_and_strategy_flat_thresholds():
    """Dust reads as flat; a real position (even a $5 min-order sliver) does not.
    A strategy is flat only when EVERY venue is flat."""
    from app.routes.dashboard import _venue_flat, _strategy_flat
    # dust / closed
    assert _venue_flat(0.0, 0.0)
    assert _venue_flat(-1e-9, -7e-7)      # -1e-9 BNB residue from a round-trip
    assert _venue_flat(5e-5, 0.0)         # sub-threshold base, no price
    # real positions are NOT flat
    assert not _venue_flat(0.001, 100.0)  # 0.001 BTC @ $100k
    assert not _venue_flat(0.58, 406.0)   # 0.58 BNB
    assert not _venue_flat(0.0, 5.0)      # $5 notional (exchange min order)
    assert not _venue_flat(0.5, 0.0)      # real base qty, stale 0 price
    # strategy: flat iff all venues flat
    assert _strategy_flat([{"net_qty_base": -1e-9, "net_qty_usd": -7e-7},
                           {"net_qty_base": 0.0, "net_qty_usd": 0.0}])
    assert not _strategy_flat([{"net_qty_base": -1e-9, "net_qty_usd": -7e-7},
                               {"net_qty_base": 0.5, "net_qty_usd": 350.0}])


def test_strategy_position_dust_reads_as_flat(strategies_yaml, stub_exchange, silent_notifier):
    """A near-zero residual left after closing a position must read as flat —
    not an un-dimmed '-$0.00'. Regression for the Jinja `select` truthiness bug
    (BNB_MACD_REV_LONG_15m showed -0$ and was not dimmed)."""
    from app.routes.dashboard import _strategy_positions
    from app.executor import _apply_fill_to_position

    # Buy then sell a hair more -> a tiny negative dust residue in the ledger.
    with session_scope() as db:
        _apply_fill_to_position(db, "TEST_BTC", "bybit", "BTCUSDT", "buy", 0.05, 60000.0)
    with session_scope() as db:
        _apply_fill_to_position(db, "TEST_BTC", "bybit", "BTCUSDT",
                                "sell", 0.05000000001, 60000.0)

    router = StrategyRouter(strategies_yaml)
    with session_scope() as db:
        by_id = {s["strategy_id"]: s for s in _strategy_positions(db, router.all())}

    assert by_id["TEST_BTC"]["flat"] is True
    assert 0 < abs(by_id["TEST_BTC"]["net_base"]) < 1e-6   # genuinely dust, not exactly 0


def test_slippage_bps_is_side_adjusted():
    """Positive = worse than signal; sign flips for sells; None when unpriced."""
    from app.routes.dashboard import _slip_bps
    assert round(_slip_bps(100.5, 100.0, "buy"), 1) == 50.0    # bought above signal -> adverse
    assert round(_slip_bps(99.5, 100.0, "buy"), 1) == -50.0    # bought below -> improvement
    assert round(_slip_bps(99.5, 100.0, "sell"), 1) == 50.0    # sold below -> adverse
    assert round(_slip_bps(100.5, 100.0, "sell"), 1) == -50.0  # sold above -> improvement
    assert _slip_bps(None, 100.0, "buy") is None
    assert _slip_bps(100.0, None, "buy") is None
    assert _slip_bps(100.0, 0.0, "buy") is None


def test_fee_formatter_trims_to_six_dp():
    from app.routes.dashboard import _fmt_fee
    assert _fmt_fee(0.2715) == "0.2715"
    assert _fmt_fee(0.271500) == "0.2715"
    assert _fmt_fee(1) == "1"
    assert _fmt_fee(0.0) == "0"
    assert _fmt_fee(None) == "0"


def test_bybit_fill_details_aggregates_executions(monkeypatch):
    """VWAP price + summed fee across partial executions for one order."""
    from app.exchanges.bybit import BybitExchange
    monkeypatch.setattr(BybitExchange, "FILL_POLL_DELAY", 0)
    ex = BybitExchange.__new__(BybitExchange)  # skip __init__ (no network/pybit client)

    class FakeClient:
        def get_executions(self, **kw):
            return {"result": {"list": [
                {"execQty": "0.3", "execPrice": "100.0", "execFee": "0.030", "feeCurrency": "USDT"},
                {"execQty": "0.2", "execPrice": "110.0", "execFee": "0.022", "feeCurrency": "USDT"},
            ]}}
    ex._client = FakeClient()

    avg, fee, ccy, tot = ex._fill_details("BTCUSDT", "oid1", 0.5)
    assert abs(tot - 0.5) < 1e-9
    assert abs(avg - 104.0) < 1e-9      # (0.3*100 + 0.2*110) / 0.5
    assert abs(fee - 0.052) < 1e-9
    assert ccy == "USDT"


def test_hyperliquid_fill_fee_matches_oid(monkeypatch):
    from app.exchanges.hyperliquid import HyperliquidExchange
    monkeypatch.setattr(HyperliquidExchange, "FILL_POLL_DELAY", 0)
    ex = HyperliquidExchange.__new__(HyperliquidExchange)  # skip __init__ (no network)
    ex._account_address = "0xabc"

    class FakeInfo:
        def user_fills(self, addr):
            return [
                {"oid": 111, "fee": "0.01", "feeToken": "USDC"},
                {"oid": 111, "fee": "0.02", "feeToken": "USDC"},
                {"oid": 999, "fee": "9.9", "feeToken": "USDC"},
            ]
    ex._info = FakeInfo()

    fee, token = ex._fill_fee("111")
    assert abs(fee - 0.03) < 1e-9
    assert token == "USDC"
    assert ex._fill_fee("123") == (0.0, "USDC")  # no match -> zero


def test_order_records_signal_price_fill_and_commission(strategies_yaml, stub_exchange,
                                                        silent_notifier):
    """Signal price flows alert -> order; fill price + real commission land on
    the order; the alert keeps the signal price too."""
    stub_exchange.next_result = OrderResult(
        success=True, exchange_order_id="X1", filled_qty_base=0.001,
        avg_price=50123.0, commission=0.05, commission_asset="USDT")
    c = _client(strategies_yaml)
    body = _payload("TEST_BTC", quantity=0.05)
    body["price"] = 50000.0  # signal price ({{close}})
    assert c.post("/webhook/tradingview", json=body).status_code == 200

    with session_scope() as db:
        o = db.query(Order).filter(Order.status == "success").one()
        assert o.signal_price == 50000.0
        assert o.fill_price == 50123.0
        assert abs(o.commission - 0.05) < 1e-12
        assert o.commission_asset == "USDT"
        assert db.query(Alert).one().signal_price == 50000.0


def test_orders_json_includes_execution_fields(strategies_yaml, stub_exchange, silent_notifier):
    stub_exchange.next_result = OrderResult(
        success=True, exchange_order_id="X1", filled_qty_base=0.001,
        avg_price=50500.0, commission=0.07, commission_asset="USDT")
    c = _client(strategies_yaml)
    body = _payload("TEST_BTC", quantity=0.05)
    body["price"] = 50000.0
    c.post("/webhook/tradingview", json=body)

    o = c.get("/orders").json()[0]
    assert o["signal_price"] == 50000.0
    assert o["fill_price"] == 50500.0
    assert o["commission"] == 0.07
    assert o["commission_asset"] == "USDT"
    assert o["slippage_bps"] is not None and o["slippage_bps"] > 0  # bought above signal


def test_execution_quality_aggregates_fees_and_slippage(strategies_yaml, stub_exchange,
                                                        silent_notifier):
    from app.routes.dashboard import _execution_quality
    stub_exchange.next_result = OrderResult(
        success=True, exchange_order_id="X", filled_qty_base=0.001,
        avg_price=50500.0, commission=0.07, commission_asset="USDT")
    c = _client(strategies_yaml)
    body = _payload("TEST_BTC", quantity=0.05)
    body["price"] = 50000.0
    c.post("/webhook/tradingview", json=body)

    with session_scope() as db:
        eq = _execution_quality(db)
    by_ex = {f["exchange"]: f for f in eq["fees_by_exchange"]}
    assert abs(by_ex["bybit"]["total"] - 0.07) < 1e-9
    assert by_ex["bybit"]["asset"] == "USDT"
    assert eq["total_fees"] > 0
    assert eq["avg_slippage_bps"] is not None and eq["avg_slippage_bps"] > 0


def test_sqlite_additive_migration_is_idempotent():
    """Running the column migration repeatedly must never raise (columns already
    present after create_all / a prior run)."""
    from app.db import _migrate_sqlite_columns
    _migrate_sqlite_columns()
    _migrate_sqlite_columns()


def test_position_increments_accumulate_via_upsert():
    """Atomic insert-or-increment: the per-symbol Position sums across
    strategies; each StrategyPosition tracks only its own fills."""
    from app.executor import _apply_fill_to_position
    from app.models import Position, StrategyPosition

    with session_scope() as db:
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 0.01, 60000.0)
    with session_scope() as db:
        _apply_fill_to_position(db, "S1", "bybit", "BTCUSDT", "buy", 0.02, 61000.0)
    with session_scope() as db:
        _apply_fill_to_position(db, "S2", "bybit", "BTCUSDT", "sell", 0.005, 62000.0)

    with session_scope() as db:
        pos = db.query(Position).filter_by(exchange="bybit", symbol="BTCUSDT").one()
        assert abs(pos.net_qty_base - 0.025) < 1e-9        # 0.01 + 0.02 - 0.005
        assert pos.last_price == 62000.0
        assert abs(pos.net_qty_usd - 0.025 * 62000.0) < 1e-3
        s1 = db.query(StrategyPosition).filter_by(
            strategy_id="S1", exchange="bybit", symbol="BTCUSDT").one()
        assert abs(s1.net_qty_base - 0.03) < 1e-9          # S1 only
        s2 = db.query(StrategyPosition).filter_by(
            strategy_id="S2", exchange="bybit", symbol="BTCUSDT").one()
        assert abs(s2.net_qty_base + 0.005) < 1e-9         # S2 only (short)


# ---------- JSON read endpoints ----------

def test_json_read_endpoints(strategies_yaml, stub_exchange, silent_notifier):
    """/positions, /alerts, /orders return the recorded activity + honor filters."""
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", quantity=0.05, alert_id="je1"))

    positions = c.get("/positions").json()
    assert any(p["exchange"] == "bybit" and p["symbol"] == "BTCUSDT" for p in positions)

    alerts = c.get("/alerts").json()
    assert alerts[0]["strategy_id"] == "TEST_BTC"
    assert "idempotency_key" in alerts[0] and "received_at" in alerts[0]

    orders = c.get("/orders").json()
    assert orders[0]["exchange"] == "bybit" and orders[0]["status"] == "success"

    assert all(o["status"] == "success" for o in c.get("/orders?status=success").json())
    assert c.get("/orders?status=dead").json() == []


# ---------- executor dead-letter path ----------

def test_order_dead_letters_after_max_attempts(strategies_yaml, stub_exchange,
                                                silent_notifier, monkeypatch):
    """When an order exhausts retries it goes 'dead' and fires order_dead."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "retry_max_attempts", 1)  # die on first failure
    stub_exchange.next_result = OrderResult(success=False, error_message="exchange down")
    c = _client(strategies_yaml)
    c.post("/webhook/tradingview", json=_payload("TEST_BTC", alert_id="dead1"))
    with session_scope() as db:
        order = db.query(Order).one()
        assert order.status == "dead"
        assert order.next_retry_at is None
        assert order.attempts >= 1
    assert any(call[0] == "order_dead" for call in silent_notifier.calls)


# ---------- reconcile read-failure skip ----------

def test_sync_skips_on_read_failure(strategies_yaml, stub_exchange, silent_notifier):
    """If get_position() raises, sync skips that venue and reports the reason
    instead of trading on unknown state."""
    from app import reconcile

    def boom(symbol):
        if symbol == "BTCUSDT":
            raise RuntimeError("api down")
        return (0.0, 0.0)
    stub_exchange.get_position = boom

    result = reconcile.sync_strategy_positions(StrategyRouter(strategies_yaml))
    skipped = {(s["strategy_id"], s["symbol"]): s["reason"] for s in result["skipped"]}
    assert ("TEST_BTC", "BTCUSDT") in skipped
    assert "read failed" in skipped[("TEST_BTC", "BTCUSDT")]


# ---------- notifier message formatters ----------

def test_notifier_formatters_build_messages(monkeypatch):
    from app.notifier import TelegramNotifier, _fmt_qty
    n = TelegramNotifier(token="t", chat_id="c")
    sent = []
    monkeypatch.setattr(n, "send", lambda text, **kw: sent.append(text))
    n.order_succeeded("S1", "bybit", "BTCUSDT", "buy", 0.005, 96000.0)
    n.order_failed("S1", "bybit", "BTCUSDT", "sell", 0.005, 2, "boom")
    n.order_dead("S1", "bybit", "BTCUSDT", "sell", 0.005, 4, "boom")
    n.duplicate_alert("S1", "k1")
    n.unknown_strategy("WAT")
    n.disabled_strategy("S1")
    assert len(sent) == 6
    assert any("Order filled" in t and "BTC" in t for t in sent)
    assert any("FAILED" in t for t in sent)
    assert any("DEAD" in t for t in sent)
    assert any("Duplicate" in t for t in sent)
    assert any("Unknown strategy" in t for t in sent)
    assert any("Disabled strategy" in t for t in sent)
    # _fmt_qty strips the quote suffix and adds ~USD when a price is given
    assert _fmt_qty(0.005, "BTCUSDT", 96000.0) == "0.005 BTC (~$480.00)"
    assert _fmt_qty(0.005, "BTC") == "0.005 BTC"


# ---------- retry worker replay ----------

def test_retry_worker_replays_due_order(strategies_yaml, stub_exchange, silent_notifier):
    """A due 'retrying' order is re-executed and (stub succeeds) lands 'success',
    with attempts incremented and the position updated."""
    from datetime import datetime, timezone, timedelta
    from app.retry_worker import _run_due_retries

    with session_scope() as db:
        a = Alert(idempotency_key="r1", strategy_id="TEST_BTC", action="buy",
                  raw_payload="{}", source_ip="")
        db.add(a)
        db.flush()
        db.add(Order(
            alert_id=a.id, exchange="bybit", symbol="BTCUSDT", side="buy",
            qty_usd=0.0, qty_base=0.01, status="retrying", attempts=1,
            next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))

    _run_due_retries()  # stub fills successfully

    with session_scope() as db:
        o = db.query(Order).one()
        assert o.status == "success"
        assert o.attempts == 2
        assert db.query(Position).filter_by(exchange="bybit", symbol="BTCUSDT").count() == 1


def test_retry_worker_skips_not_yet_due(strategies_yaml, stub_exchange, silent_notifier):
    """An order whose next_retry_at is in the future is left alone."""
    from datetime import datetime, timezone, timedelta
    from app.retry_worker import _run_due_retries

    with session_scope() as db:
        a = Alert(idempotency_key="r2", strategy_id="TEST_BTC", action="buy",
                  raw_payload="{}", source_ip="")
        db.add(a)
        db.flush()
        db.add(Order(
            alert_id=a.id, exchange="bybit", symbol="BTCUSDT", side="buy",
            qty_usd=0.0, qty_base=0.01, status="retrying", attempts=1,
            next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))

    _run_due_retries()

    with session_scope() as db:
        assert db.query(Order).one().status == "retrying"  # untouched


def test_configured_strategy_shows_flat_without_fills(strategies_yaml, stub_exchange, silent_notifier):
    """Every configured strategy appears in the per-strategy view — flat if it
    has no fills yet — so a freshly added strategy shows up immediately."""
    c = _client(strategies_yaml)
    by_id = {s["strategy_id"]: s for s in c.get("/strategy-positions").json()}
    for sid in ("TEST_BTC", "TEST_MULTI", "TEST_DISABLED"):
        assert sid in by_id, f"{sid} should be listed even with no fills"
        assert by_id[sid]["net_base"] == 0.0
        assert by_id[sid]["configured"] is True


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
    assert "Execution quality" in r.text   # new section renders even with no priced fills
