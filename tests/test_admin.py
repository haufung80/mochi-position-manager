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
    assert "viewport-fit=cover" in r.text          # mobile/Safari responsive
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
        "sar": False,
        "venues": {"hyperliquid": True, "bybit": False},
    }
    assert "quantity_usd" not in entry  # NOT in YAML schema anymore

    s = client.app.state.strategy_router.get("MR_BTC_6H")
    enabled = s.enabled_venues()
    assert len(enabled) == 1
    assert (enabled[0].exchange, enabled[0].symbol) == ("hyperliquid", "BTC")


def test_post_creates_limit_entry_strategy(client, strategies_file):
    r = client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "LIM_BTC", "base_asset": "BTC",
        "venue_bybit": "on", "position_size": "1000", "entry_limit": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
    data = yaml.safe_load(strategies_file.read_text())
    assert data["strategies"]["LIM_BTC"]["entry"] == "limit"
    assert client.app.state.strategy_router.get("LIM_BTC").entry == "limit"


def test_default_entry_is_market_and_omitted_from_yaml(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "MKT_BTC", "base_asset": "BTC",
        "venue_bybit": "on",
    }, follow_redirects=False)
    data = yaml.safe_load(strategies_file.read_text())
    assert "entry" not in data["strategies"]["MKT_BTC"]      # default omitted, schema unchanged
    assert client.app.state.strategy_router.get("MKT_BTC").entry == "market"


def test_toggle_entry_endpoint_flips_market_limit(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "TOG", "base_asset": "BTC",
        "venue_bybit": "on", "position_size": "1000",
    }, follow_redirects=False)
    assert client.app.state.strategy_router.get("TOG").entry == "market"
    r = client.post("/admin/strategies/toggle-entry/TOG",
                    data={"secret": SECRET}, follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.strategy_router.get("TOG").entry == "limit"
    client.post("/admin/strategies/toggle-entry/TOG",
                data={"secret": SECRET}, follow_redirects=False)
    assert client.app.state.strategy_router.get("TOG").entry == "market"


def test_toggle_entry_bad_secret_rejected(client):
    r = client.post("/admin/strategies/toggle-entry/whatever",
                    data={"secret": "wrong"}, follow_redirects=False)
    assert r.status_code in (401, 403)


# --- manual "Fire limit order" ----------------------------------------------

def _create_managed(client, sid, size="140"):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": sid, "base_asset": "SOL",
        "venue_bybit": "on", "position_size": size,
    }, follow_redirects=False)


def test_fire_limit_explicit_qty_places_working(client, strategies_file, stub_exchange, silent_notifier):
    from app.db import session_scope
    from app.models import Order
    _create_managed(client, "FL")
    r = client.post("/admin/strategies/fire-limit", data={
        "secret": SECRET, "strategy_id": "FL", "side": "buy",
        "limit_price": "69.8", "fire_quantity": "2",
    }, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        o = db.query(Order).filter_by(order_type="limit").one()
        assert o.status == "working" and o.side == "buy"
        assert o.limit_price == 69.8 and o.qty_base == 2.0
    assert any(c[0] == "limit_order_placed" for c in silent_notifier.calls)   # Telegram alert fired


def test_fire_limit_managed_sizing_when_qty_blank(client, strategies_file, stub_exchange, silent_notifier):
    from app.db import session_scope
    from app.models import Order
    _create_managed(client, "FL4", size="140")
    r = client.post("/admin/strategies/fire-limit", data={
        "secret": SECRET, "strategy_id": "FL4", "side": "sell", "limit_price": "70",
    }, follow_redirects=False)   # no quantity -> 140/70 = 2.0 (step 0.001)
    assert r.status_code == 303
    with session_scope() as db:
        o = db.query(Order).filter_by(order_type="limit").one()
        assert o.status == "working" and o.side == "sell" and o.qty_base == pytest.approx(2.0)


def test_fire_limit_bad_secret(client, strategies_file):
    _create_managed(client, "FL2")
    r = client.post("/admin/strategies/fire-limit", data={
        "secret": "wrong", "strategy_id": "FL2", "side": "buy", "limit_price": "69.8",
    }, follow_redirects=False)
    assert r.status_code == 401


def test_fire_limit_unknown_strategy(client, strategies_file):
    r = client.post("/admin/strategies/fire-limit", data={
        "secret": SECRET, "strategy_id": "NOPE", "side": "buy", "limit_price": "69.8",
    }, follow_redirects=False)
    assert r.status_code == 404


def test_fire_limit_refused_during_kill_switch(client, strategies_file):
    from app.db import session_scope
    from app.risk import update_risk_settings
    from app.models import Order
    _create_managed(client, "FL3")
    with session_scope() as db:
        update_risk_settings(db, kill_switch=True)
    r = client.post("/admin/strategies/fire-limit", data={
        "secret": SECRET, "strategy_id": "FL3", "side": "buy",
        "limit_price": "69.8", "fire_quantity": "2",
    }, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        assert db.query(Order).count() == 0       # halted -> nothing placed


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


# ---------- reload-strategies ----------
# Regression guard: a no-auth duplicate of this route once lived in
# dashboard.py and shadowed the secret-protected one (it was registered
# first). If that ever returns, a wrong secret would be ignored and yield
# 200 instead of 401 — so the wrong-secret case is the real assertion.

def test_reload_strategies_wrong_secret_rejected(client):
    r = client.post("/admin/reload-strategies", data={"secret": "wrong"})
    assert r.status_code == 401


def test_reload_strategies_with_secret_ok(client):
    r = client.post("/admin/reload-strategies", data={"secret": SECRET})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "count": 0}


# ---------- SAR (stop-and-reverse) flag ----------
# Label-only marker today; no order-behaviour change. These lock in the
# config/UI plumbing so it can't silently regress.

def test_post_creates_strategy_with_sar(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "SAR_ONE", "base_asset": "BTC",
        "venue_bybit": "on", "sar": "on",
    }, follow_redirects=False)
    data = yaml.safe_load(strategies_file.read_text())
    assert data["strategies"]["SAR_ONE"]["sar"] is True
    assert client.app.state.strategy_router.get("SAR_ONE").sar is True


def test_sar_defaults_false_when_unchecked(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "PLAIN", "base_asset": "BTC",
        "venue_bybit": "on",
    }, follow_redirects=False)
    data = yaml.safe_load(strategies_file.read_text())
    assert data["strategies"]["PLAIN"]["sar"] is False
    assert client.app.state.strategy_router.get("PLAIN").sar is False


def test_toggle_sar_flips_state(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_bybit": "on",
    }, follow_redirects=False)
    assert client.app.state.strategy_router.get("X").sar is False

    r = client.post("/admin/strategies/toggle-sar/X",
                    data={"secret": SECRET}, follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.strategy_router.get("X").sar is True

    client.post("/admin/strategies/toggle-sar/X",
                data={"secret": SECRET}, follow_redirects=False)
    assert client.app.state.strategy_router.get("X").sar is False


def test_toggle_sar_unknown_strategy_404(client):
    r = client.post("/admin/strategies/toggle-sar/NOPE", data={"secret": SECRET})
    assert r.status_code == 404


def test_toggle_sar_wrong_secret_rejected(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_bybit": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/toggle-sar/X", data={"secret": "wrong"})
    assert r.status_code == 401


def test_list_renders_sar_toggle(client, strategies_file):
    """Render the strategy table with a route present — exercises the new SAR
    pill cell (other render tests use an empty list)."""
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_bybit": "on", "sar": "on",
    }, follow_redirects=False)
    r = client.get("/admin/strategies")
    assert r.status_code == 200
    assert "/admin/strategies/toggle-sar/X" in r.text   # SAR toggle form rendered
    assert 'name="sar"' in r.text                        # SAR checkbox in the add form


# ---------- position_size (managed sizing) ----------

def test_post_persists_position_size(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "MGD", "base_asset": "BTC",
        "venue_bybit": "on", "position_size": "1500",
    }, follow_redirects=False)
    data = yaml.safe_load(strategies_file.read_text())
    assert data["strategies"]["MGD"]["position_size"] == 1500.0
    assert client.app.state.strategy_router.get("MGD").position_size == 1500.0


def test_blank_position_size_is_paper(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "PAP", "base_asset": "BTC",
        "venue_bybit": "on",
    }, follow_redirects=False)
    data = yaml.safe_load(strategies_file.read_text())
    assert "position_size" not in data["strategies"]["PAP"]   # omitted when blank
    assert client.app.state.strategy_router.get("PAP").position_size is None


def test_invalid_position_size_rejected(client):
    r = client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_bybit": "on", "position_size": "-5",
    })
    assert r.status_code == 400


# ---------- inline set-size (per-row editor) ----------

def test_set_size_inline_updates_only_size(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC", "venue_bybit": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/set-size/X",
                    data={"secret": SECRET, "position_size": "1500"}, follow_redirects=False)
    assert r.status_code == 303
    entry = yaml.safe_load(strategies_file.read_text())["strategies"]["X"]
    assert entry["position_size"] == 1500.0
    assert entry["base_asset"] == "BTC"                       # untouched
    assert entry["venues"] == {"hyperliquid": False, "bybit": True}
    assert client.app.state.strategy_router.get("X").position_size == 1500.0


def test_set_size_blank_clears_to_paper(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC",
        "venue_bybit": "on", "position_size": "1000",
    }, follow_redirects=False)
    client.post("/admin/strategies/set-size/X",
                data={"secret": SECRET, "position_size": ""}, follow_redirects=False)
    entry = yaml.safe_load(strategies_file.read_text())["strategies"]["X"]
    assert "position_size" not in entry                        # cleared -> paper mode
    assert client.app.state.strategy_router.get("X").position_size is None


def test_set_size_unknown_strategy_404(client):
    r = client.post("/admin/strategies/set-size/NOPE",
                    data={"secret": SECRET, "position_size": "100"})
    assert r.status_code == 404


def test_set_size_wrong_secret_rejected(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC", "venue_bybit": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/set-size/X",
                    data={"secret": "wrong", "position_size": "100"})
    assert r.status_code == 401


def test_set_size_invalid_rejected(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "X", "base_asset": "BTC", "venue_bybit": "on",
    }, follow_redirects=False)
    r = client.post("/admin/strategies/set-size/X",
                    data={"secret": SECRET, "position_size": "-5"})
    assert r.status_code == 400


def test_set_size_form_renders_for_managed(client, strategies_file):
    client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "MGD", "base_asset": "BTC", "venue_bybit": "on",
    }, follow_redirects=False)
    r = client.get("/admin/strategies")
    assert "/admin/strategies/set-size/MGD" in r.text          # inline editor present


# ---------- risk controls ----------

def test_risk_post_sets_cap_and_kill_switch(client):
    from app.db import session_scope
    from app.risk import get_risk_settings
    r = client.post("/admin/risk", data={
        "secret": SECRET, "per_order_max_notional": "750", "kill_switch": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        rs = get_risk_settings(db)
        assert rs.per_order_max_notional == pytest.approx(750.0) and rs.kill_switch is True
    # blank cap -> off (0); the kill-switch checkbox absent -> off
    client.post("/admin/risk", data={"secret": SECRET, "per_order_max_notional": ""},
                follow_redirects=False)
    with session_scope() as db:
        rs = get_risk_settings(db)
        assert rs.per_order_max_notional == 0.0 and rs.kill_switch is False


def test_risk_post_requires_secret(client):
    r = client.post("/admin/risk", data={"per_order_max_notional": "750"},
                    follow_redirects=False)
    assert r.status_code == 401


def test_strategies_page_shows_risk_form(client):
    r = client.get("/admin/strategies")
    assert r.status_code == 200
    assert 'name="per_order_max_notional"' in r.text and 'name="kill_switch"' in r.text
    assert "Risk controls" in r.text


def test_save_warns_when_position_size_out_of_range(client, stub_exchange):
    """Configuring position_size surfaces a config-time warning when it's outside the
    placeable range: above the per-order cap (orders rejected) or below the venue minimum
    (orders won't fill)."""
    from app.db import session_scope
    from app.risk import update_risk_settings
    with session_scope() as db:
        update_risk_settings(db, per_order_max_notional=500.0)
    # above the per-order cap -> warn
    r = client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "BIG", "base_asset": "BTC",
        "venue_bybit": "on", "position_size": "1000"}, follow_redirects=False)
    assert r.status_code == 303 and "warn=" in r.headers["location"]
    assert "cap" in r.headers["location"]
    # below the venue minimum -> warn (stub min for the resolved symbol)
    stub_exchange.min_notionals["BTCUSDT"] = 50.0
    r = client.post("/admin/strategies", data={
        "secret": SECRET, "strategy_id": "TINY", "base_asset": "BTC",
        "venue_bybit": "on", "position_size": "10"}, follow_redirects=False)
    assert r.status_code == 303 and "minimum" in r.headers["location"]
