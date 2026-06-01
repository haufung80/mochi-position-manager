import pytest

from app.routing import StrategyRouter, SUPPORTED_BASE_ASSETS
from app.exchanges.symbols import symbol_for, SUPPORTED_EXCHANGES


def test_loads_strategy_with_base_asset(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    btc = r.get("TEST_BTC")
    assert btc is not None
    assert btc.base_asset == "BTC"
    venues = {v.exchange: v for v in btc.venues}
    assert venues["bybit"].enabled is True
    assert venues["bybit"].symbol == "BTCUSDT"
    assert venues["hyperliquid"].enabled is False
    assert venues["hyperliquid"].symbol == "BTC"


def test_enabled_venues_filter(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    enabled = r.get("TEST_BTC").enabled_venues()
    assert len(enabled) == 1
    assert enabled[0].exchange == "bybit"


def test_enabled_property(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("TEST_BTC").enabled is True
    assert r.get("TEST_DISABLED").enabled is False


def test_multi_venue_strategy(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    enabled = r.get("TEST_MULTI").enabled_venues()
    assert {(v.exchange, v.symbol) for v in enabled} == {
        ("bybit", "ETHUSDT"),
        ("hyperliquid", "ETH"),
    }


def test_venue_order_is_canonical_regardless_of_yaml(tmp_path):
    """Venues load in SUPPORTED_EXCHANGES order no matter how the YAML lists
    them — so the dashboard, per-strategy view, and fan-out stay consistent."""
    p = tmp_path / "order.yaml"
    p.write_text(
        "strategies:\n"
        "  A:\n"
        "    base_asset: BTC\n"
        "    venues:\n"
        "      bybit: true\n"          # bybit first in YAML
        "      hyperliquid: true\n"
        "  B:\n"
        "    base_asset: ETH\n"
        "    venues:\n"
        "      hyperliquid: true\n"    # hyperliquid first in YAML
        "      bybit: true\n"
    )
    r = StrategyRouter(p)
    order_a = [v.exchange for v in r.get("A").venues]
    order_b = [v.exchange for v in r.get("B").venues]
    assert order_a == order_b == list(SUPPORTED_EXCHANGES)


def test_unknown_returns_none(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("UNKNOWN") is None


# ---------- symbol mapping ----------

def test_symbol_for_known_exchanges():
    assert symbol_for("hyperliquid", "BTC") == "BTC"
    assert symbol_for("bybit", "BTC") == "BTCUSDT"
    assert symbol_for("hyperliquid", "ETH") == "ETH"
    assert symbol_for("bybit", "SOL") == "SOLUSDT"
    assert symbol_for("bybit", "BNB") == "BNBUSDT"


def test_symbol_for_unknown_exchange_raises():
    with pytest.raises(ValueError, match="unsupported exchange"):
        symbol_for("binance", "BTC")


def test_symbol_for_unsupported_base_asset_raises():
    with pytest.raises(ValueError, match="unsupported base_asset"):
        symbol_for("hyperliquid", "DOGE")


def test_supported_constants():
    assert "BTC" in SUPPORTED_BASE_ASSETS
    assert "ETH" in SUPPORTED_BASE_ASSETS
    assert "SOL" in SUPPORTED_BASE_ASSETS
    assert "BNB" in SUPPORTED_BASE_ASSETS
    assert "hyperliquid" in SUPPORTED_EXCHANGES
    assert "bybit" in SUPPORTED_EXCHANGES


# ---------- resilience ----------

def test_unsupported_venue_is_skipped_not_fatal(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "strategies:\n"
        "  X:\n"
        "    base_asset: BTC\n"
        "    venues:\n"
        "      hyperliquid: true\n"
        "      ftx: true\n"
    )
    r = StrategyRouter(p)
    s = r.get("X")
    assert s is not None
    assert {v.exchange for v in s.venues} == {"hyperliquid"}


def test_unsupported_base_asset_skips_entry(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "strategies:\n"
        "  GOOD:\n"
        "    base_asset: BTC\n"
        "    venues:\n"
        "      hyperliquid: true\n"
        "  BAD:\n"
        "    base_asset: DOGE\n"
        "    venues:\n"
        "      hyperliquid: true\n"
    )
    r = StrategyRouter(p)
    assert r.get("GOOD") is not None
    assert r.get("BAD") is None


def test_router_resilient_to_missing_file(tmp_path):
    f = tmp_path / "does-not-exist.yaml"
    r = StrategyRouter(f)
    assert r.all() == []


def test_router_resilient_to_malformed_yaml(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("strategies:\n  - this should be a dict not a list\n")
    r = StrategyRouter(f)
    assert r.all() == []


def test_router_skips_bad_entries_loads_good_ones(tmp_path):
    f = tmp_path / "mixed.yaml"
    f.write_text("""
strategies:
  GOOD:
    base_asset: BTC
    venues:
      hyperliquid: true
  MISSING_BASE_ASSET:
    venues:
      hyperliquid: true
  EMPTY_VENUES:
    base_asset: ETH
    venues: {}
""")
    r = StrategyRouter(f)
    routes = {x.strategy_id for x in r.all()}
    assert "GOOD" in routes
    assert "MISSING_BASE_ASSET" not in routes
    assert "EMPTY_VENUES" not in routes


def test_reload_picks_up_changes(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("TEST_BTC").base_asset == "BTC"
    strategies_yaml.write_text(
        "strategies:\n"
        "  TEST_BTC:\n"
        "    base_asset: SOL\n"
        "    venues:\n"
        "      bybit: true\n"
    )
    r.reload()
    assert r.get("TEST_BTC").base_asset == "SOL"
    assert r.get("TEST_BTC").venues[0].symbol == "SOLUSDT"


def test_hyperliquid_adapter_imports_cleanly():
    """Regression: ensure the HL adapter + its eth_account dep chain import
    without errors. Catches parsimonious/inspect.getargspec issues on
    Python 3.11+ that don't surface until first use in production
    (since the import is lazy inside the registry)."""
    from app.exchanges.hyperliquid import HyperliquidExchange  # noqa: F401


def test_bybit_adapter_imports_cleanly():
    """Same regression guard for the Bybit adapter."""
    from app.exchanges.bybit import BybitExchange  # noqa: F401


def test_hl_market_open_signature_matches_our_call():
    """Regression: ensure the HL SDK's market_open() still accepts the
    kwargs we pass. The SDK has made breaking changes here before — e.g.
    0.23 dropped `reduce_only` — and those crashes don't surface until a
    real webhook is routed to HL in production."""
    import inspect
    from hyperliquid.exchange import Exchange as HLExchange
    params = set(inspect.signature(HLExchange.market_open).parameters)
    expected = {"name", "is_buy", "sz", "px", "slippage"}
    missing = expected - params
    assert not missing, (
        f"HL SDK's market_open no longer accepts {missing}; "
        f"update app/exchanges/hyperliquid.py to match new signature."
    )
