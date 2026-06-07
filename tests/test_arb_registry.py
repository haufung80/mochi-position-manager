"""A.1 — multi-account registry + the HL same-address construction guard.

These run OFFLINE. The Bybit adapter is network-free to construct (pybit's HTTP
client makes no call until a method is invoked). The Hyperliquid adapter, however,
builds the SDK ``Exchange``/``Info`` (which fetch ``spotMeta``/``meta``) the moment
a private key is present — so the HL tests monkeypatch those SDK seams to a stub.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.exchanges import hyperliquid as hl_mod
from app.exchanges.registry import ExchangeRegistry, get_registry, reset_registry


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    get_settings.cache_clear()
    yield
    reset_registry()
    get_settings.cache_clear()


@pytest.fixture
def offline_hl(monkeypatch):
    """Make constructing a real HL adapter (with a private key) network-free by
    stubbing the SDK ``Exchange`` (which would otherwise fetch spot/perp meta)."""
    class _StubHLExchange:
        def __init__(self, wallet, base_url, **kwargs):
            self.wallet = wallet
            self.account_address = kwargs.get("account_address")

    monkeypatch.setattr(hl_mod, "HLExchange", _StubHLExchange)
    # Info is still constructed (skip_ws=True) but only used lazily by methods we
    # never call here; stub it too so __init__ stays offline.
    monkeypatch.setattr(hl_mod, "Info", lambda *a, **kw: object())
    return _StubHLExchange


# A throwaway but VALID secp256k1 key + its address (so eth_account derives the
# same address the guard compares against). Generated offline, never funded.
_KEY = "0x0000000000000000000000000000000000000000000000000000000000000001"
_ADDR = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"  # address of _KEY


# --- default account keeps every existing call site identical ----------------

def test_default_account_is_the_implicit_one(monkeypatch):
    reg = ExchangeRegistry()
    a = reg.get("bybit")
    b = reg.get("bybit", "default")
    assert a is b  # same cached instance — one-arg get() == account="default"


def test_arb_account_is_a_distinct_instance(monkeypatch):
    monkeypatch.setenv("BYBIT_ARB_API_KEY", "ak")
    monkeypatch.setenv("BYBIT_ARB_API_SECRET", "as")
    get_settings.cache_clear()
    reg = ExchangeRegistry()
    default = reg.get("bybit")
    arb = reg.get("bybit", "arb")
    assert default is not arb
    assert reg.get("bybit", "arb") is arb  # cache key includes the account


def test_bybit_and_hyperliquid_arb_are_distinct(monkeypatch, offline_hl):
    monkeypatch.setenv("BYBIT_ARB_API_KEY", "ak")
    monkeypatch.setenv("BYBIT_ARB_API_SECRET", "as")
    monkeypatch.setenv("HYPERLIQUID_ARB_PRIVATE_KEY", _KEY)
    monkeypatch.setenv("HYPERLIQUID_ARB_ACCOUNT_ADDRESS", "0x00000000000000000000000000000000000000aa")
    # directional HL address differs so the guard passes
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", "0x00000000000000000000000000000000000000bb")
    get_settings.cache_clear()
    reg = ExchangeRegistry()
    by = reg.get("bybit", "arb")
    hl = reg.get("hyperliquid", "arb")
    assert by is not hl
    assert by.name == "bybit" and hl.name == "hyperliquid"


# --- HL same-address catastrophe is refused at CONSTRUCTION ------------------

def test_hl_arb_address_equal_to_directional_raises(monkeypatch, offline_hl):
    # arb address == directional address -> the guard must raise before any order.
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", _KEY)
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", _ADDR)
    monkeypatch.setenv("HYPERLIQUID_ARB_PRIVATE_KEY", _KEY)
    monkeypatch.setenv("HYPERLIQUID_ARB_ACCOUNT_ADDRESS", _ADDR)
    get_settings.cache_clear()
    reg = ExchangeRegistry()
    # directional builds fine (default account is never guarded)
    reg.get("hyperliquid")
    with pytest.raises(ValueError, match="SAME as the directional"):
        reg.get("hyperliquid", "arb")


def test_hl_arb_address_derived_from_key_equal_to_directional_raises(monkeypatch, offline_hl):
    """Even with HYPERLIQUID_ARB_ACCOUNT_ADDRESS left blank, the address derived
    from the arb private key must not collide with the directional address."""
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", _ADDR)
    monkeypatch.setenv("HYPERLIQUID_ARB_PRIVATE_KEY", _KEY)  # derives _ADDR
    monkeypatch.setenv("HYPERLIQUID_ARB_ACCOUNT_ADDRESS", "")
    get_settings.cache_clear()
    reg = ExchangeRegistry()
    with pytest.raises(ValueError, match="SAME as the directional"):
        reg.get("hyperliquid", "arb")


def test_hl_arb_distinct_address_constructs(monkeypatch, offline_hl):
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", "0x00000000000000000000000000000000000000bb")
    monkeypatch.setenv("HYPERLIQUID_ARB_PRIVATE_KEY", _KEY)
    monkeypatch.setenv("HYPERLIQUID_ARB_ACCOUNT_ADDRESS", "0x00000000000000000000000000000000000000aa")
    get_settings.cache_clear()
    reg = ExchangeRegistry()
    hl = reg.get("hyperliquid", "arb")
    assert hl.name == "hyperliquid"


# --- unknown / empty-cred accounts raise clearly ----------------------------

def test_unknown_account_raises():
    reg = ExchangeRegistry()
    with pytest.raises(ValueError, match="Unknown account"):
        reg.get("bybit", "nope")


def test_empty_arb_creds_raise():
    # arb creds unset -> a non-default account must fail loudly.
    reg = ExchangeRegistry()
    with pytest.raises(ValueError, match="Missing credentials"):
        reg.get("bybit", "arb")


def test_unknown_exchange_raises():
    reg = ExchangeRegistry()
    with pytest.raises(ValueError, match="Unknown exchange"):
        reg.get("kraken")


# --- reset / singleton hooks -------------------------------------------------

def test_reset_registry_clears_singleton():
    first = get_registry()
    assert get_registry() is first
    reset_registry()
    assert get_registry() is not first


def test_account_credentials_default_matches_directional():
    s = get_settings()
    by = s.account_credentials("bybit")  # default
    assert by["api_key"] == s.bybit_api_key
    assert by["dry_run"] == s.dry_run
    hl = s.account_credentials("hyperliquid", "default")
    assert hl["private_key"] == s.hyperliquid_private_key
    assert hl["vault_address"] == s.hyperliquid_vault_address
