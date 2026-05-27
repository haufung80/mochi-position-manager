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
    """New schema (post-redesign): one strategy fans out to multiple venues.

    TEST_BTC routes to bybit only (single venue, used by happy-path tests
    that assert exactly one Order is created per alert).
    TEST_MULTI routes to BOTH bybit and hyperliquid (used by fan-out tests).
    TEST_DISABLED has both venues disabled.
    """
    p = tmp_path / "strategies.yaml"
    p.write_text(
        """
strategies:
  TEST_BTC:
    base_asset: BTC
    quantity_usd: 100
    venues:
      bybit: true
      hyperliquid: false
  TEST_MULTI:
    base_asset: ETH
    quantity_usd: 50
    venues:
      bybit: true
      hyperliquid: true
  TEST_DISABLED:
    base_asset: ETH
    quantity_usd: 100
    venues:
      bybit: false
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

        def market_order(self, symbol, side, qty_usd, leverage=1.0, reduce_only=False):
            self.calls.append(("market", symbol, side, qty_usd, leverage, reduce_only))
            return self.next_result

        def close_position(self, symbol):
            self.calls.append(("close", symbol))
            return OrderResult(success=True, exchange_order_id="CLOSED")

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
