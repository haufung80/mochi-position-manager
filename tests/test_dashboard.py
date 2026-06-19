"""Dashboard JSON endpoints: live-data cache headers + execution quality.

Two things are guarded here:

1. Cache headers — the live trading views (`/orders`, `/positions`, `/alerts`,
   `/strategy-positions`) must send `Cache-Control: no-store` so a browser or
   proxy never serves a stale/empty snapshot. This is the regression guard for
   the "/orders looked empty in the browser" thread: the endpoint never told
   the browser not to cache, so an early empty response could stick.
2. Execution quality — `/orders` surfaces signal-vs-fill slippage (bps) and the
   real commission charged per fill.
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import Alert, Order
from app.routing import StrategyRouter


@pytest.fixture
def client(tmp_path):
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies: {}\n")
    with TestClient(app) as c:
        # /strategy-positions reads app.state.strategy_router; the others don't.
        c.app.state.strategy_router = StrategyRouter(f)
        yield c


def _add_order(**kwargs) -> None:
    """Insert one Alert + Order, overriding Order fields via kwargs."""
    with session_scope() as db:
        alert = Alert(idempotency_key=kwargs.pop("idempotency_key", "k1"),
                      strategy_id="S", action="buy", raw_payload="{}")
        db.add(alert)
        db.flush()
        defaults = dict(
            alert_id=alert.id, exchange="bybit", symbol="BTCUSDT", side="buy",
            qty_usd=50.0, status="success",
        )
        defaults.update(kwargs)
        db.add(Order(**defaults))


# ---------- cache headers (empty-list regression) ----------

@pytest.mark.parametrize("path", [
    "/orders", "/positions", "/alerts", "/strategy-positions",
])
def test_live_json_endpoints_send_no_store(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")


def test_dashboard_html_sends_no_store(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")


def test_dashboard_uses_unified_components(client):
    """Homepage renders the shared design-system components (one visual vocabulary
    across all pages): card-stats with .label/.big and canonical `.pill.s-*` status
    pills — not the retired standalone `.stat`/`.stat-label` or compound `.pill
    <status>`. Guards the design-unification from silently regressing."""
    _add_order(status="dead", fill_price=50000.0, signal_price=50000.0)
    r = client.get("/")
    assert r.status_code == 200
    assert r.text.count('class="card stat"') == 3   # 3 stat cards, the data-page pattern
    assert 'class="big' in r.text                    # big-number stat (not the old standalone .stat)
    assert 'class="pill s-dead"' in r.text           # canonical status pill
    assert 'class="pill dead"' not in r.text         # legacy compound pill retired
    assert "stat-label" not in r.text                # legacy label class retired


# ---------- execution quality on /orders ----------

def test_orders_reports_slippage_and_commission(client):
    # Buy filled 10 bps above signal (paid up): (50050-50000)/50000*1e4 = 10.
    _add_order(qty_base=0.001, signal_price=50000.0, fill_price=50050.0,
               commission=0.0275, commission_asset="USDT")
    rows = client.get("/orders").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["signal_price"] == 50000.0
    assert row["fill_price"] == 50050.0
    assert row["slippage_bps"] == pytest.approx(10.0)
    assert row["commission"] == 0.0275
    assert row["commission_asset"] == "USDT"


def test_orders_sell_slippage_sign_is_side_adjusted(client):
    # Sell filled BELOW signal = worse execution -> positive bps.
    _add_order(side="sell", qty_base=0.001,
               signal_price=50000.0, fill_price=49950.0)
    row = client.get("/orders").json()[0]
    assert row["slippage_bps"] == pytest.approx(10.0)


def test_orders_slippage_none_without_fill_price(client):
    _add_order(status="pending", signal_price=50000.0)  # not filled yet
    row = client.get("/orders").json()[0]
    assert row["slippage_bps"] is None


def test_orders_status_filter(client):
    _add_order(idempotency_key="a", status="success", fill_price=50000.0,
               signal_price=50000.0)
    _add_order(idempotency_key="b", status="dead")
    assert len(client.get("/orders").json()) == 2
    dead = client.get("/orders?status=dead").json()
    assert [o["status"] for o in dead] == ["dead"]
