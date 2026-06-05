import os
import tempfile
from pathlib import Path
import pytest

# Set env BEFORE importing app modules — pydantic-settings reads at import.
os.environ.setdefault("WEBHOOK_SECRET", "test-secret-12345")
os.environ.setdefault("DRY_RUN", "true")

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

# Clear any cached settings the app modules may have grabbed at import time
from app.config import get_settings  # noqa: E402
get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_db():
    from app.db import engine, init_db, Base
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield


@pytest.fixture
def strategies_yaml(tmp_path):
    """Test strategies covering both execution modes.

    TEST_BTC      — bybit only, managed (sar=false), NO position_size → paper mode
    TEST_MULTI    — bybit + hyperliquid, managed (fan-out tests)
    TEST_DISABLED — both venues disabled
    TEST_SAR      — bybit SOL, sar=true → alert-driven (quantity from TV)
    TEST_MANAGED  — bybit XRP, managed with position_size=1000 (USDT notional)
    """
    p = tmp_path / "strategies.yaml"
    p.write_text(
        """
strategies:
  TEST_BTC:
    base_asset: BTC
    venues:
      bybit: true
      hyperliquid: false
  TEST_MULTI:
    base_asset: ETH
    venues:
      bybit: true
      hyperliquid: true
  TEST_DISABLED:
    base_asset: ETH
    venues:
      bybit: false
      hyperliquid: false
  TEST_SAR:
    base_asset: SOL
    sar: true
    venues:
      bybit: true
      hyperliquid: false
  TEST_MANAGED:
    base_asset: XRP
    position_size: 1000
    venues:
      bybit: true
      hyperliquid: false
"""
    )
    return p


@pytest.fixture
def stub_exchange(monkeypatch):
    """Replace the registry with a recording fake."""
    from app.exchanges import registry as reg_mod
    from app.schemas import OrderResult

    class FakeExchange:
        name = "fake"

        def __init__(self):
            self.calls = []
            self.next_result = OrderResult(success=True, exchange_order_id="FAKE_1",
                                           filled_qty_base=0.001, avg_price=50000.0)
            # symbol -> (signed_qty, mark_price), returned by get_position()
            self.positions = {}
            # managed-sizing inputs (configurable per symbol)
            self.prices = {}        # symbol -> latest price (default 50000.0)
            self.step_sizes = {}    # symbol -> step size (default 0.001)
            self.funding = {}       # symbol -> list[{"time_ms", "amount"}]
            self.min_notionals = {} # symbol -> min order value (default 0.0 = none)

        def market_order(self, symbol, side, quantity, leverage=1.0):
            self.calls.append(("market", symbol, side, quantity, leverage))
            return self.next_result

        def close_position(self, symbol):
            self.calls.append(("close", symbol))
            return OrderResult(success=True, exchange_order_id="CLOSED")

        def get_position(self, symbol):
            self.calls.append(("get_position", symbol))
            return self.positions.get(symbol, (0.0, 0.0))

        def get_price(self, symbol):
            self.calls.append(("get_price", symbol))
            return self.prices.get(symbol, 50000.0)

        def get_step_size(self, symbol):
            self.calls.append(("get_step_size", symbol))
            return self.step_sizes.get(symbol, 0.001)

        def get_funding(self, symbol, start_ms, end_ms):
            self.calls.append(("get_funding", symbol, start_ms, end_ms))
            return list(self.funding.get(symbol, []))

        def get_min_notional(self, symbol):
            self.calls.append(("get_min_notional", symbol))
            return self.min_notionals.get(symbol, 0.0)

    fake = FakeExchange()

    class FakeRegistry:
        def get(self, name):
            return fake

    monkeypatch.setattr(reg_mod, "_registry", FakeRegistry())
    return fake


@pytest.fixture
def silent_notifier(monkeypatch):
    """Disable Telegram outbound during tests."""
    from app import notifier as notif_mod

    class SilentNotifier:
        enabled = False
        calls = []
        def __getattr__(self, name):
            def _noop(*a, **kw):
                self.calls.append((name, a, kw))
            return _noop

    s = SilentNotifier()
    monkeypatch.setattr(notif_mod, "_notifier", s)
    return s
