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
    from app.routes.dashboard import _clear_equity_cache
    from app.routes.funding_arb import _clear_arb_equity_cache
    Base.metadata.drop_all(bind=engine)
    init_db()
    _clear_equity_cache()           # module-global caches must not leak across tests
    _clear_arb_equity_cache()
    yield


@pytest.fixture(autouse=True)
def _risk_off(_clean_db):
    """Default the pre-trade per-order cap OFF in tests (so integration tests aren't
    coupled to it). The risk-gate tests set the cap / kill-switch explicitly."""
    from app.db import session_scope
    from app.risk import update_risk_settings
    with session_scope() as db:
        update_risk_settings(db, per_order_max_notional=0.0)


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


from app.schemas import OrderResult, OrderStatus  # noqa: E402


class _SharedState:
    """Mutable test knobs shared across every (name, account) FakeExchange built
    by one FakeRegistry. Legacy tests configure these on the single returned
    `stub_exchange` and expect them to apply to ALL venues (and to aggregate
    `.calls` across venues); sharing the state keeps that contract while still
    handing out a DISTINCT FakeExchange instance per (name, account)."""

    def __init__(self):
        self.calls = []                # aggregated across venues/accounts
        self.next_result = OrderResult(success=True, exchange_order_id="FAKE_1",
                                       filled_qty_base=0.001, avg_price=50000.0,
                                       fee_source="exchange")  # fills are "enriched" by default
        self.spot_result = None        # if set, returned by spot_market_order
        self.positions = {}            # symbol -> (signed_qty, mark_price)
        self.entries = {}              # symbol -> exchange avg entry
        self.klines = {}               # symbol -> historical close
        self.prices = {}               # symbol -> latest price (default 50000.0)
        self.step_sizes = {}           # symbol -> perp step (default 0.001)
        self.funding = {}              # symbol -> list[{"time_ms","amount"}]
        self.min_notionals = {}        # symbol -> perp min value (default 0.0)
        # --- spot knobs ---
        self.spot_balances = {}        # base_asset -> free base balance
        self.spot_step_sizes = {}      # symbol -> spot step (default 0.001)
        self.spot_min_notionals = {}   # symbol -> spot min value (default 10.0)
        self.spot_base_fee = 0.0       # base-coin fee deducted on a spot BUY
        # --- resting limit-order knobs (limit-entry feature) ---
        self.limit_orders = {}         # order_id -> dict(symbol,side,qty,price,filled,avg,state,commission,commission_asset)
        self._limit_seq = 0            # counter for fake resting-order ids


# The mutable test knobs that are SHARED across every (name, account) fake built
# by one FakeRegistry (so legacy single-fake tests keep working). Anything NOT in
# this set — methods, ad-hoc per-instance overrides like
# `fake.get_position = boom` — lives on the instance normally.
_SHARED_KNOBS = frozenset(_SharedState().__dict__)


class FakeExchange:
    """Recording fake implementing the perp + spot Protocol surface. Identity is
    ``(name, account)``; the data knobs in ``_SHARED_KNOBS`` live on a shared
    `_SharedState` (so the legacy single-fake contract holds), while methods and
    ad-hoc per-instance overrides stay on the instance."""

    def __init__(self, name="fake", account="default", state=None):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "_s", state or _SharedState())

    def __getattr__(self, item):
        # Only reached when normal lookup misses (so methods/instance attrs win).
        if item in _SHARED_KNOBS:
            return getattr(object.__getattribute__(self, "_s"), item)
        raise AttributeError(item)

    def __setattr__(self, key, value):
        if key in _SHARED_KNOBS:
            setattr(self._s, key, value)         # shared across all instances
        else:
            object.__setattr__(self, key, value)  # instance-local (e.g. method override)

    # ---- perp surface ----
    def market_order(self, symbol, side, quantity, leverage=1.0):
        self._s.calls.append(("market", symbol, side, quantity, leverage,
                              self.name, self.account))
        return self._s.next_result

    def close_position(self, symbol):
        self._s.calls.append(("close", symbol, self.name, self.account))
        return OrderResult(success=True, exchange_order_id="CLOSED")

    def get_position(self, symbol):
        self._s.calls.append(("get_position", symbol))
        return self._s.positions.get(symbol, (0.0, 0.0))

    def get_position_detail(self, symbol):
        self._s.calls.append(("get_position_detail", symbol))
        qty, mark = self._s.positions.get(symbol, (0.0, 0.0))
        entry = self._s.entries.get(symbol, 0.0)
        return {"qty": qty, "mark": mark, "entry": entry,
                "unrealized": qty * (mark - entry) if entry else 0.0}

    def get_kline_close(self, symbol, ts_ms):
        self._s.calls.append(("get_kline_close", symbol, ts_ms))
        return self._s.klines.get(symbol, 0.0)

    def get_price(self, symbol):
        self._s.calls.append(("get_price", symbol))
        return self._s.prices.get(symbol, 50000.0)

    def get_step_size(self, symbol):
        self._s.calls.append(("get_step_size", symbol))
        return self._s.step_sizes.get(symbol, 0.001)

    def get_funding(self, symbol, start_ms, end_ms):
        self._s.calls.append(("get_funding", symbol, start_ms, end_ms))
        return list(self._s.funding.get(symbol, []))

    def get_min_notional(self, symbol):
        self._s.calls.append(("get_min_notional", symbol))
        return self._s.min_notionals.get(symbol, 0.0)

    # ---- resting limit-order surface ----
    # Places a "working" order; tests drive its fill trajectory by mutating
    # `state.limit_orders[oid]` (filled / state / avg) between order_status() polls.
    def limit_order(self, symbol, side, quantity, price, *, client_order_id="", leverage=1.0):
        self._s.calls.append(("limit", symbol, side, quantity, price, client_order_id,
                              self.name, self.account))
        self._s._limit_seq += 1
        oid = client_order_id or f"LIM{self._s._limit_seq}"
        self._s.limit_orders[oid] = {
            "symbol": symbol, "side": side, "qty": quantity, "price": price,
            "filled": 0.0, "avg": price, "state": "working",
            "commission": 0.0, "commission_asset": "",
        }
        return OrderResult(success=True, exchange_order_id=oid,
                           filled_qty_base=0.0, avg_price=price)

    def cancel_order(self, symbol, order_id):
        self._s.calls.append(("cancel", symbol, order_id, self.name, self.account))
        o = self._s.limit_orders.get(order_id)
        if o is not None:
            o["state"] = "cancelled"
        return True

    def order_status(self, symbol, order_id):
        self._s.calls.append(("order_status", symbol, order_id))
        o = self._s.limit_orders.get(order_id)
        if o is None:
            return OrderStatus(state="unknown", exchange_order_id=order_id)
        return OrderStatus(state=o["state"], filled_qty_base=o["filled"], avg_price=o["avg"],
                           commission=o.get("commission", 0.0),
                           commission_asset=o.get("commission_asset", ""),
                           exchange_order_id=order_id)

    # ---- spot surface (WORKS on both venues, incl. the HL fake) ----
    def spot_market_order(self, symbol, side, qty):
        self._s.calls.append(("spot_market", symbol, side, qty,
                              self.name, self.account))
        if self._s.spot_result is not None:
            return self._s.spot_result
        price = self._s.prices.get(symbol, 50000.0)
        filled = qty
        commission_asset = ""
        commission = 0.0
        # Model a base-coin BUY fee (Bybit-style): net received base = qty - fee.
        if side == "buy" and self._s.spot_base_fee:
            commission = self._s.spot_base_fee
            commission_asset = symbol.replace("USDT", "").split("/")[0]
            filled = max(0.0, qty - commission)
        return OrderResult(success=True, exchange_order_id="FAKE_SPOT",
                           filled_qty_base=filled, avg_price=price,
                           commission=commission, commission_asset=commission_asset)

    def get_spot_balance(self, base_asset):
        self._s.calls.append(("get_spot_balance", base_asset, self.name, self.account))
        return self._s.spot_balances.get(base_asset, 0.0)

    def get_spot_step_size(self, symbol):
        self._s.calls.append(("get_spot_step_size", symbol))
        return self._s.spot_step_sizes.get(symbol, 0.001)

    def get_spot_min_notional(self, symbol):
        self._s.calls.append(("get_spot_min_notional", symbol))
        return self._s.spot_min_notionals.get(symbol, 10.0)


class FakeRegistry:
    """Hands out a DISTINCT FakeExchange per (name, account). All instances share
    one `_SharedState` so the legacy single-fake test contract still holds."""

    def __init__(self, state=None):
        self.state = state or _SharedState()
        self._instances: dict[tuple[str, str], FakeExchange] = {}

    def get(self, name, account="default"):
        name = name.lower()
        account = (account or "default").lower()
        key = (name, account)
        if key not in self._instances:
            self._instances[key] = FakeExchange(name=name, account=account,
                                                state=self.state)
        return self._instances[key]


@pytest.fixture
def stub_exchange(monkeypatch):
    """Replace the registry with a recording fake.

    Returns ONE representative FakeExchange (bybit/default); its knobs and `.calls`
    are shared across every (name, account) the registry hands out, so legacy
    tests that set `stub_exchange.next_result`/`.prices`/... and read aggregated
    `.calls` keep working unchanged."""
    from app.exchanges import registry as reg_mod
    fake_reg = FakeRegistry()
    monkeypatch.setattr(reg_mod, "_registry", fake_reg)
    return fake_reg.get("bybit", "default")


@pytest.fixture
def arb_registry(monkeypatch):
    """A FakeRegistry installed as the active registry, returned directly so arb
    tests can pull DISTINCT (name, account) fakes (e.g. get('hyperliquid','arb')
    is a different instance from get('hyperliquid','default')). Shared knobs +
    `.calls` live on `arb_registry.state`."""
    from app.exchanges import registry as reg_mod
    fake_reg = FakeRegistry()
    monkeypatch.setattr(reg_mod, "_registry", fake_reg)
    return fake_reg


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
