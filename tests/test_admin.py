"""Tests for /admin/strategies UI endpoints.

Schema: strategy = base_asset + venues. Per-signal qty NOT in the form;
it's supplied by the TradingView alert payload at runtime.
"""
from __future__ import annotations
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app
from app.routing import StrategyRouter


SECRET = "test-secret-12345"


@pytest.fixture
def strategies_file(tmp_path, monkeypatch):
    f = tmp_path / "strategies.yaml"
    f.write_text("strategies: {}\n")
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "strategies_file", str(f))
    return f


@pytest.fixture
def client(strategies_file):
    with TestClient(app) as c:
        c.app.state.strategy_router = StrategyRouter(strategies_file)
        yield c


# ---------- form rendering ----------

def test_page_renders_dropdown_with_supported_assets(client):
    r = client.get("/admin/strategies")
    assert r.status_code == 200
    # base_asset is a dropdown, populated from SUPPORTED_BASE_ASSETS
    for asset in ("BTC", "ETH", "SOL", "BNB"):
        assert f">{asset}</option>" in r.text
    # venue checkboxes for both supported exchanges
    assert 'name="venue_hyperliquid"' in r.text
    assert 'name="venue_bybit"' in r.text
    # qty input REMOVED — driven by TV alert now
    assert 'name="quantity"' not in r.text
    assert 'name="quantity_usd"' not in r.text


# ---------- auth ----------

def test_post_without_secret_rejected(client):
    r = client.post("/admin/strategies", data={
        "strategy_id": "X", "base_asset": "BTC", "venue_hyperliquid": "on",
    })
    assert r.status_code == 401


def test_post_with_wrong_secret_returns_401(client):
    r = client.post("/admin/strategies", data={
        "secret": "wrong", "strategy_id": "X", "base_asset": "BTC",
        "venue_hyperliquid": "on",
    })
    assert r.status_code == 401


# ---------- create / update ----------

def test_post_creates_strategy_with_single_venue(client, strategies_file):
    r = client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "MR_BTC_6H",
        "base_asset": "BTC", "venue_hyperliquid": "on",
    }, follow_redirects=False)
    assert r.status_code == 303

    data = yaml.safe_load(strategies_file.read_text())
    entry = data["strategies"]["MR_BTC_6H"]
    assert entry == {
        "base_asset": "BTC",
        "venues": {"hyperliquid": True, "bybit": False},
    }
    assert "quantity_usd" not in entry  # NOT in YAML schema anymore

    s = client.app.state.strategy_router.get("MR_BTC_6H")
    enabled = s.enabled_venues()
    assert len(enabled) == 1
    assert (enabled[0].exchange, enabled[0].symbol) == ("hyperliquid", "BTC")


def test_post_creates_multi_venue(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "MULTI",
        "base_asset": "ETH",
        "venue_hyperliquid": "on", "venue_bybit": "on",
    }, follow_redirects=False)

    s = client.app.state.strategy_router.get("MULTI")
    assert {(v.exchange, v.symbol) for v in s.enabled_venues()} == {
        ("hyperliquid", "ETH"),
        ("bybit", "ETHUSDT"),
    }


def test_post_with_no_venues_saved_inactive(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "PAUSED", "base_asset": "BTC",
    }, follow_redirects=False)
    s = client.app.state.strategy_router.get("PAUSED")
    assert s is not None and s.enabled is False


def test_post_rejects_unsupported_base_asset(client):
    r = client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "DOGE",
        "venue_hyperliquid": "on",
    })
    assert r.status_code == 400


def test_post_accepts_all_supported_base_assets(client, strategies_file):
    for asset in ("BTC", "ETH", "SOL", "BNB"):
        client.post("/admin/strategies", data={
            "secret": SECRET, "strategy_id": f"S_{asset}",
            "base_asset": asset, "venue_hyperliquid": "on",
        }, follow_redirects=False)
    routes = {r.strategy_id: r.base_asset for r in client.app.state.strategy_router.all()}
    assert routes == {"S_BTC": "BTC", "S_ETH": "ETH", "S_SOL": "SOL", "S_BNB": "BNB"}


def test_post_updates_existing_strategy(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_hyperliquid": "on",
    }, follow_redirects=False)
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "ETH",
        "venue_bybit": "on",
    }, follow_redirects=False)

    s = client.app.state.strategy_router.get("X")
    assert s.base_asset == "ETH"
    enabled = s.enabled_venues()
    assert len(enabled) == 1
    assert (enabled[0].exchange, enabled[0].symbol) == ("bybit", "ETHUSDT")


# ---------- delete ----------

def test_delete_removes_strategy(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_hyperliquid": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/delete/X",
                    data={"secret": SECRET}, follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.strategy_router.get("X") is None


def test_delete_without_secret_rejected(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_hyperliquid": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/delete/X", data={"secret": "wrong"})
    assert r.status_code == 401


def test_delete_unknown_strategy_404(client):
    r = client.post("/admin/strategies/delete/UNKNOWN",
                    data={"secret": SECRET})
    assert r.status_code == 404


# ---------- toggle ----------

def test_toggle_venue_flips_state(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_hyperliquid": "on",
    }, follow_redirects=False)

    r = client.post("/admin/strategies/toggle/X/bybit",
                    data={"secret": SECRET}, follow_redirects=False)
    assert r.status_code == 303
    venues = {v.exchange: v.enabled for v in client.app.state.strategy_router.get("X").venues}
    assert venues == {"hyperliquid": True, "bybit": True}

    client.post("/admin/strategies/toggle/X/bybit",
                data={"secret": SECRET}, follow_redirects=False)
    venues = {v.exchange: v.enabled for v in client.app.state.strategy_router.get("X").venues}
    assert venues == {"hyperliquid": True, "bybit": False}


def test_toggle_unknown_strategy_returns_404(client):
    r = client.post("/admin/strategies/toggle/DOES_NOT_EXIST/bybit",
                    data={"secret": SECRET})
    assert r.status_code == 404


def test_toggle_unsupported_exchange_returns_400(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_hyperliquid": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/toggle/X/binance",
                    data={"secret": SECRET})
    assert r.status_code == 400
