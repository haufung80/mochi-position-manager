import pytest

from app.routing import StrategyRouter, symbol_for, SUPPORTED_EXCHANGES


def test_loads_strategy_with_base_asset(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    btc = r.get("TEST_BTC")
    assert btc is not None
    assert btc.base_asset == "BTC"
    assert btc.quantity_usd == 100.0
    # one venue per exchange in YAML
    venues = {v.exchange: v for v in btc.venues}
    assert venues["bybit"].enabled is True
    assert venues["bybit"].symbol == "BTCUSDT"
    assert venues["hyperliquid"].enabled is False
    assert venues["hyperliquid"].symbol == "BTC"


def test_enabled_venues_filter(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    btc = r.get("TEST_BTC")
    enabled = btc.enabled_venues()
    assert len(enabled) == 1
    assert enabled[0].exchange == "bybit"


def test_strategy_enabled_property_when_any_venue_enabled(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("TEST_BTC").enabled is True
    assert r.get("TEST_DISABLED").enabled is False


def test_multi_venue_strategy(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    multi = r.get("TEST_MULTI")
    assert multi is not None
    enabled = multi.enabled_venues()
    assert len(enabled) == 2
    ex = {v.exchange: v.symbol for v in enabled}
    assert ex == {"bybit": "ETHUSDT", "hyperliquid": "ETH"}


def test_unknown_returns_none(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("UNKNOWN") is None


def test_symbol_for_known_exchanges():
    assert symbol_for("hyperliquid", "BTC") == "BTC"
    assert symbol_for("bybit", "BTC") == "BTCUSDT"
    assert symbol_for("hyperliquid", "ETH") == "ETH"
    assert symbol_for("bybit", "SOL") == "SOLUSDT"


def test_symbol_for_unknown_exchange_raises():
    with pytest.raises(ValueError, match="unsupported exchange"):
        symbol_for("binance", "BTC")


def test_supported_exchanges_constant():
    assert "hyperliquid" in SUPPORTED_EXCHANGES
    assert "bybit" in SUPPORTED_EXCHANGES


def test_unsupported_venue_is_skipped_not_fatal(tmp_path):
    """A typo in a venue name should not crash — it should be logged
    and the rest of the strategy should still load."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        "strategies:\n"
        "  X:\n"
        "    base_asset: BTC\n"
        "    quantity_usd: 20\n"
        "    venues:\n"
        "      hyperliquid: true\n"
        "      ftx: true\n"  # unknown exchange
    )
    r = StrategyRouter(p)
    s = r.get("X")
    assert s is not None
    venues = {v.exchange for v in s.venues}
    assert venues == {"hyperliquid"}  # ftx silently dropped


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
    quantity_usd: 20
    venues:
      hyperliquid: true
  MISSING_BASE_ASSET:
    quantity_usd: 20
    venues:
      hyperliquid: true
  ZERO_QTY:
    base_asset: ETH
    quantity_usd: 0
    venues:
      hyperliquid: true
""")
    r = StrategyRouter(f)
    routes = {x.strategy_id for x in r.all()}
    assert "GOOD" in routes
    assert "MISSING_BASE_ASSET" not in routes
    assert "ZERO_QTY" not in routes


def test_reload_picks_up_changes(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("TEST_BTC").quantity_usd == 100.0
    strategies_yaml.write_text(
        "strategies:\n"
        "  TEST_BTC:\n"
        "    base_asset: BTC\n"
        "    quantity_usd: 500\n"
        "    venues:\n"
        "      bybit: true\n"
    )
    r.reload()
    assert r.get("TEST_BTC").quantity_usd == 500.0


def test_venue_leverage_is_hardcoded_to_one(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    btc = r.get("TEST_BTC")
    for v in btc.venues:
        assert v.leverage == 1.0
