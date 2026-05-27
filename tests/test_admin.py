"""Tests for /admin/strategies UI endpoints."""
from __future__ import annotations
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app
from app.routing import StrategyRouter


SECRET = "test-secret-12345"  # matches conftest default; avoids cache pollution


@pytest.fixture
def strategies_file(tmp_path, monkeypatch):
    """Empty strategies.yaml in a temp dir + settings pointed at it.

    We override the settings.strategies_file by patching the cached Settings
    object directly — avoiding env-var + cache_clear() dance that can pollute
    sibling test modules.
    """
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies: {}\n")
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "strategies_file", str(f))
    return f


@pytest.fixture
def client(strategies_file):
    """TestClient with state.strategy_router pointed at the temp file."""
    with TestClient(app) as c:
        # lifespan sets up app.state.strategy_router; override to our temp file
        c.app.state.strategy_router = StrategyRouter(strategies_file)
        yield c


def test_get_page_renders(client):
    r = client.get("/admin/strategies")
    assert r.status_code == 200
    assert "Strategy Configuration" in r.text
    assert "Webhook secret (required)" in r.text


def test_post_without_secret_rejected(client):
    r = client.post("/admin/strategies", data={
        "strategy_id": "X",
        "exchange": "hyperliquid",
        "symbol": "BTC",
        "quantity_usd": "10",
        "leverage": "1",
    })
    # FastAPI Form(...) returns 422 if a required form field is missing
    assert r.status_code == 422


def test_post_with_wrong_secret_returns_401(client):
    r = client.post("/admin/strategies", data={
        "secret": "wrong",
        "strategy_id": "X",
        "exchange": "hyperliquid",
        "symbol": "BTC",
        "quantity_usd": "10",
        "leverage": "1",
    })
    assert r.status_code == 401
    assert r.json() == {"detail": "bad secret"}


def test_post_creates_strategy(client, strategies_file):
    r = client.post(
        "/admin/strategies",
        data={
            "secret": SECRET,
            "strategy_id": "MR_VOTING_BTC_6H",
            "exchange": "hyperliquid",
            "symbol": "BTC",
            "quantity_usd": "20",
            "leverage": "2",
            "enabled": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/strategies"

    # file persisted in correct dict shape
    data = yaml.safe_load(strategies_file.read_text())
    assert "strategies" in data
    assert "MR_VOTING_BTC_6H" in data["strategies"]
    entry = data["strategies"]["MR_VOTING_BTC_6H"]
    assert entry == {
        "exchange": "hyperliquid",
        "symbol": "BTC",
        "quantity_usd": 20.0,
        "leverage": 2.0,
        "enabled": True,
    }

    # router reloaded — get() returns the new route
    route = client.app.state.strategy_router.get("MR_VOTING_BTC_6H")
    assert route is not None
    assert route.exchange == "hyperliquid"
    assert route.quantity_usd == 20.0


def test_post_rejects_unsupported_exchange(client):
    r = client.post("/admin/strategies", data={
        "secret": SECRET,
        "strategy_id": "X",
        "exchange": "binance",
        "symbol": "BTC",
        "quantity_usd": "10",
        "leverage": "1",
    })
    assert r.status_code == 400
    assert "unsupported exchange" in r.json()["detail"]


def test_post_rejects_zero_qty(client):
    r = client.post("/admin/strategies", data={
        "secret": SECRET,
        "strategy_id": "X",
        "exchange": "hyperliquid",
        "symbol": "BTC",
        "quantity_usd": "0",
        "leverage": "1",
    })
    assert r.status_code == 400


def test_post_updates_existing_strategy(client, strategies_file):
    # create
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "exchange": "hyperliquid",
        "symbol": "BTC", "quantity_usd": "10", "leverage": "1", "enabled": "true",
    }, follow_redirects=False)
    # update
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "exchange": "bybit",
        "symbol": "BTCUSDT", "quantity_usd": "50", "leverage": "3", "enabled": "false",
    }, follow_redirects=False)

    data = yaml.safe_load(strategies_file.read_text())
    assert data["strategies"]["X"] == {
        "exchange": "bybit",
        "symbol": "BTCUSDT",
        "quantity_usd": 50.0,
        "leverage": 3.0,
        "enabled": False,
    }


def test_delete_removes_strategy(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "exchange": "hyperliquid",
        "symbol": "BTC", "quantity_usd": "10", "leverage": "1", "enabled": "true",
    }, follow_redirects=False)
    assert client.app.state.strategy_router.get("X") is not None

    r = client.post("/admin/strategies/delete/X",
                    data={"secret": SECRET}, follow_redirects=False)
    assert r.status_code == 303

    data = yaml.safe_load(strategies_file.read_text())
    assert "X" not in data["strategies"]
    assert client.app.state.strategy_router.get("X") is None


def test_delete_without_secret_rejected(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "exchange": "hyperliquid",
        "symbol": "BTC", "quantity_usd": "10", "leverage": "1", "enabled": "true",
    }, follow_redirects=False)

    r = client.post("/admin/strategies/delete/X", data={"secret": "wrong"})
    assert r.status_code == 401
    # entry still there
    data = yaml.safe_load(strategies_file.read_text())
    assert "X" in data["strategies"]


def test_router_resilient_to_malformed_yaml(tmp_path, monkeypatch):
    """Broken YAML must not crash; router should be empty + log warning."""
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies:\n  - BAD: this should be a dict, not a list\n")
    r = StrategyRouter(f)
    assert r.all() == []


def test_router_resilient_to_missing_file(tmp_path):
    f = tmp_path / "does-not-exist.yaml"
    r = StrategyRouter(f)
    assert r.all() == []


def test_router_skips_bad_entries_loads_good_ones(tmp_path):
    f = tmp_path / "strategies.yaml"
    f.write_text("""
strategies:
  GOOD:
    exchange: hyperliquid
    symbol: BTC
    quantity_usd: 20
    leverage: 2
  BAD_EXCHANGE:
    exchange: foobar
    symbol: BTC
    quantity_usd: 20
  MISSING_SYMBOL:
    exchange: bybit
    quantity_usd: 20
""")
    r = StrategyRouter(f)
    routes = {x.strategy_id: x for x in r.all()}
    assert "GOOD" in routes
    assert "BAD_EXCHANGE" not in routes
    assert "MISSING_SYMBOL" not in routes
