import pytest
from app.routing import StrategyRouter


def test_loads_strategies(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    btc = r.get("TEST_BTC")
    assert btc is not None
    assert btc.exchange == "bybit"
    assert btc.symbol == "BTCUSDT"
    assert btc.quantity_usd == 100.0
    assert btc.leverage == 2.0
    assert btc.enabled is True


def test_disabled_route_loads(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    d = r.get("TEST_DISABLED")
    assert d is not None and d.enabled is False


def test_unknown_returns_none(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("UNKNOWN") is None


def test_unsupported_exchange_is_skipped_not_fatal(tmp_path, caplog):
    """Bad entries are logged and skipped — they must not crash the router,
    so one typo can't bring down the whole app at startup."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        "strategies:\n"
        "  BAD:\n"
        "    exchange: ftx\n"
        "    symbol: BTC\n"
        "    quantity_usd: 100\n"
        "  GOOD:\n"
        "    exchange: hyperliquid\n"
        "    symbol: BTC\n"
        "    quantity_usd: 20\n"
    )
    r = StrategyRouter(p)
    assert r.get("BAD") is None
    assert r.get("GOOD") is not None


def test_reload_picks_up_changes(strategies_yaml):
    r = StrategyRouter(strategies_yaml)
    assert r.get("TEST_BTC").quantity_usd == 100.0
    strategies_yaml.write_text(
        "strategies:\n"
        "  TEST_BTC:\n"
        "    exchange: bybit\n"
        "    symbol: BTCUSDT\n"
        "    quantity_usd: 500\n"
        "    leverage: 5\n"
    )
    r.reload()
    assert r.get("TEST_BTC").quantity_usd == 500.0
