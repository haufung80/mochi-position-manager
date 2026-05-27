"""Tests for /admin/strategies UI endpoints (new venue fan-out schema)."""
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
    """Empty strategies.yaml in a temp dir + settings pointed at it."""
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies: {}\n")
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "strategies_file", str(f))
    return f


@pytest.fixture
def client(strategies_file):
    with TestClient(app) as c:
        c.app.state.strategy_router = StrategyRouter(strategies_file)
        yield c


def test_get_page_renders(client):
    r = client.get("/admin/strategies")
    assert r.status_code == 200
    assert "Strategy Configuration" in r.text
    assert "Base asset" in r.text
    # checkboxes for both supported exchanges should render
    assert "venue_hyperliquid" in r.text
    assert "venue_bybit" in r.text


def test_post_without_secret_rejected(client):
    r = client.post("/admin/strategies", data={
        "strategy_id": "X", "base_asset": "BTC", "quantity_usd": "10",
        "venue_hyperliquid": "on",
    })
    assert r.status_code == 401


def test_post_with_wrong_secret_returns_401(client):
    r = client.post("/admin/strategies", data={
        "secret": "wrong",
        "strategy_id": "X", "base_asset": "BTC", "quantity_usd": "10",
        "venue_hyperliquid": "on",
    })
    assert r.status_code == 401
    assert r.json() == {"detail": "bad secret"}


def test_post_creates_strategy_with_single_venue(client, strategies_file):
    r = client.post(
        "/admin/strategies",
        data={
            "secret": SECRET,
            "strategy_id": "MR_VOTING_BTC_6H",
            "base_asset": "BTC",
            "quantity_usd": "20",
            "venue_hyperliquid": "on",
            # bybit checkbox NOT submitted (unchecked == absent)
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/strategies"

    data = yaml.safe_load(strategies_file.read_text())
    entry = data["strategies"]["MR_VOTING_BTC_6H"]
    assert entry["base_asset"] == "BTC"
    assert entry["quantity_usd"] == 20.0
    assert entry["venues"] == {"hyperliquid": True, "bybit": False}

    # router reloaded — strategy resolves to one enabled venue with right symbol
    s = client.app.state.strategy_router.get("MR_VOTING_BTC_6H")
    assert s is not None
    enabled = s.enabled_venues()
    assert len(enabled) == 1
    assert enabled[0].exchange == "hyperliquid"
    assert enabled[0].symbol == "BTC"


def test_post_creates_strategy_with_both_venues(client, strategies_file):
    r = client.post(
        "/admin/strategies",
        data={
            "secret": SECRET,
            "strategy_id": "MULTI",
            "base_asset": "ETH",
            "quantity_usd": "15",
            "venue_hyperliquid": "on",
            "venue_bybit": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    data = yaml.safe_load(strategies_file.read_text())
    assert data["strategies"]["MULTI"]["venues"] == {"hyperliquid": True, "bybit": True}

    s = client.app.state.strategy_router.get("MULTI")
    assert len(s.enabled_venues()) == 2
    symbols = {v.exchange: v.symbol for v in s.enabled_venues()}
    assert symbols == {"hyperliquid": "ETH", "bybit": "ETHUSDT"}


def test_post_with_no_venues_is_saved_but_inactive(client, strategies_file):
    """All-venues-off is allowed (pause without losing config)."""
    client.post("/admin/strategies", data={
        "secret": SECRET,
        "strategy_id": "PAUSED",
        "base_asset": "BTC",
        "quantity_usd": "10",
        # no venue_ fields
    }, follow_redirects=False)

    s = client.app.state.strategy_router.get("PAUSED")
    assert s is not None
    assert s.enabled is False
    assert len(s.enabled_venues()) == 0


def test_post_rejects_zero_qty(client):
    r = client.post("/admin/strategies", data={
        "secret": SECRET,
        "strategy_id": "X", "base_asset": "BTC", "quantity_usd": "0",
        "venue_hyperliquid": "on",
    })
    assert r.status_code == 400


def test_post_rejects_bad_base_asset(client):
    r = client.post("/admin/strategies", data={
        "secret": SECRET,
        "strategy_id": "X", "base_asset": "BTC$%^", "quantity_usd": "10",
        "venue_hyperliquid": "on",
    })
    assert r.status_code == 400


def test_post_updates_existing_strategy(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "quantity_usd": "10", "venue_hyperliquid": "on",
    }, follow_redirects=False)
    # update — different asset + venue
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "ETH",
        "quantity_usd": "50", "venue_bybit": "on",
    }, follow_redirects=False)

    s = client.app.state.strategy_router.get("X")
    assert s.base_asset == "ETH"
    assert s.quantity_usd == 50.0
    enabled = s.enabled_venues()
    assert len(enabled) == 1
    assert enabled[0].exchange == "bybit"
    assert enabled[0].symbol == "ETHUSDT"


def test_delete_removes_strategy(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "quantity_usd": "10", "venue_hyperliquid": "on",
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
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "quantity_usd": "10", "venue_hyperliquid": "on",
    }, follow_redirects=False)

    r = client.post("/admin/strategies/delete/X", data={"secret": "wrong"})
    assert r.status_code == 401
    data = yaml.safe_load(strategies_file.read_text())
    assert "X" in data["strategies"]


def test_toggle_venue_flips_enabled_state(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "quantity_usd": "10", "venue_hyperliquid": "on",
    }, follow_redirects=False)

    s = client.app.state.strategy_router.get("X")
    venues_before = {v.exchange: v.enabled for v in s.venues}
    assert venues_before == {"hyperliquid": True, "bybit": False}

    # toggle bybit on
    r = client.post("/admin/strategies/toggle/X/bybit",
                    data={"secret": SECRET}, follow_redirects=False)
    assert r.status_code == 303

    s = client.app.state.strategy_router.get("X")
    assert {v.exchange: v.enabled for v in s.venues} == {"hyperliquid": True, "bybit": True}

    # toggle bybit back off
    client.post("/admin/strategies/toggle/X/bybit",
                data={"secret": SECRET}, follow_redirects=False)
    s = client.app.state.strategy_router.get("X")
    assert {v.exchange: v.enabled for v in s.venues} == {"hyperliquid": True, "bybit": False}


def test_toggle_unknown_strategy_returns_404(client):
    r = client.post("/admin/strategies/toggle/DOES_NOT_EXIST/bybit",
                    data={"secret": SECRET})
    assert r.status_code == 404


def test_toggle_unsupported_exchange_returns_400(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "quantity_usd": "10", "venue_hyperliquid": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/toggle/X/binance",
                    data={"secret": SECRET})
    assert r.status_code == 400
