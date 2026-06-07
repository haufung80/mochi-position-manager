"""A.2 — spot adapters (Bybit AND a REAL Hyperliquid spot adapter) + Protocol.

Three layers:
  1. Through the FakeRegistry (conftest): DRY_RUN-style spot orders + balances on
     BOTH venues, incl. the HL fake whose spot WORKS (net-base OrderResult).
  2. Real BybitExchange spot methods against a stubbed pybit client: base-precision
     rounding, minOrderAmt, free balance, and base-coin BUY-fee netting.
  3. Real HyperliquidExchange spot methods against a stubbed `spotMeta`: Unit pair
     resolution ('UBTC/USDC' -> '@142'), szDecimals sizing, balance, DRY_RUN.
"""
from __future__ import annotations

import pytest

from app.exchanges.bybit import BybitExchange
from app.exchanges.hyperliquid import HyperliquidExchange
from app.exchanges.symbols import spot_symbol_for
from app.schemas import OrderResult


# --- spot symbol mapping -----------------------------------------------------

def test_spot_symbol_for_both_venues():
    assert spot_symbol_for("bybit", "BTC") == "BTCUSDT"
    assert spot_symbol_for("hyperliquid", "BTC") == "UBTC/USDC"
    assert spot_symbol_for("hyperliquid", "ETH") == "UETH/USDC"
    assert spot_symbol_for("hyperliquid", "SOL") == "USOL/USDC"
    with pytest.raises(ValueError):
        spot_symbol_for("hyperliquid", "DOGE")
    with pytest.raises(ValueError):
        spot_symbol_for("kraken", "BTC")


# --- 1. through the FakeRegistry (both venues, incl. HL spot working) --------

def test_fake_spot_order_both_venues_net_base(arb_registry):
    by = arb_registry.get("bybit", "arb")
    hl = arb_registry.get("hyperliquid", "arb")
    rb = by.spot_market_order("BTCUSDT", "buy", 0.01)
    rh = hl.spot_market_order("UBTC/USDC", "buy", 0.01)
    assert rb.success and rb.filled_qty_base == 0.01
    assert rh.success and rh.filled_qty_base == 0.01     # HL spot does NOT raise
    # distinct instances per (name, account)
    assert by is not hl
    assert arb_registry.get("hyperliquid", "default") is not hl


def test_fake_spot_base_fee_nets_filled(arb_registry):
    arb_registry.state.spot_base_fee = 0.00001
    by = arb_registry.get("bybit", "arb")
    r = by.spot_market_order("BTCUSDT", "buy", 0.01)
    assert r.filled_qty_base == pytest.approx(0.01 - 0.00001)
    assert r.commission_asset == "BTC"


def test_fake_spot_balance_read(arb_registry):
    arb_registry.state.spot_balances["BTC"] = 0.5
    by = arb_registry.get("bybit", "arb")
    assert by.get_spot_balance("BTC") == 0.5
    assert by.get_spot_balance("ETH") == 0.0


# --- 2. real Bybit spot adapter against a stubbed pybit client ---------------

class _StubBybitClient:
    """Minimal pybit HTTP stand-in returning canned spot instrument + fills."""

    def __init__(self, *, base_precision="0.000001", min_qty="0.000048",
                 min_amt="5", exec_fee="0.00000999", fee_ccy="BTC",
                 exec_qty="0.01", exec_price="50000"):
        self._inst = {
            "lotSizeFilter": {
                "basePrecision": base_precision,
                "minOrderQty": min_qty,
                "minOrderAmt": min_amt,
            }
        }
        self._exec = {"execQty": exec_qty, "execPrice": exec_price,
                      "execFee": exec_fee, "feeCurrency": fee_ccy}
        self.placed = []

    def get_instruments_info(self, category, symbol):
        assert category == "spot"
        return {"result": {"list": [self._inst]}}

    def get_tickers(self, category, symbol):
        return {"result": {"list": [{"lastPrice": "50000"}]}}

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"retCode": 0, "result": {"orderId": "OID1"}}

    def get_executions(self, category, symbol, orderId, limit):
        assert category == "spot"
        return {"result": {"list": [self._exec]}}

    def get_wallet_balance(self, accountType, coin):
        return {"result": {"list": [{"coin": [
            {"coin": "BTC", "availableToWithdraw": "0.4", "walletBalance": "0.5"}]}]}}


def _bybit_with(client) -> BybitExchange:
    ex = BybitExchange(api_key="k", api_secret="s", dry_run=False)
    ex._client = client
    return ex


def test_bybit_spot_buy_marketunit_basecoin_and_rounding():
    c = _StubBybitClient()
    ex = _bybit_with(c)
    r = ex.spot_market_order("BTCUSDT", "buy", 0.0100007)
    assert r.success
    # snapped DOWN to basePrecision (1e-6): 0.010000
    assert c.placed[0]["qty"] == "0.01"
    assert c.placed[0]["marketUnit"] == "baseCoin"
    assert c.placed[0]["category"] == "spot"


def test_bybit_spot_buy_fee_is_base_netted():
    # exec 0.01 BTC, fee 0.00000999 BTC -> net held = 0.00999001
    c = _StubBybitClient(exec_qty="0.01", exec_fee="0.00000999", fee_ccy="BTC")
    ex = _bybit_with(c)
    r = ex.spot_market_order("BTCUSDT", "buy", 0.01)
    assert r.commission_asset == "BTC"
    assert r.filled_qty_base == pytest.approx(0.01 - 0.00000999)


def test_bybit_spot_sell_not_base_netted():
    # a SELL fee is quote-denominated -> filled base is NOT reduced.
    c = _StubBybitClient(exec_qty="0.01", exec_fee="0.5", fee_ccy="USDT")
    ex = _bybit_with(c)
    r = ex.spot_market_order("BTCUSDT", "sell", 0.01)
    assert r.filled_qty_base == pytest.approx(0.01)
    assert r.commission_asset == "USDT"


def test_bybit_spot_min_notional_from_min_order_amt():
    c = _StubBybitClient(min_amt="5")
    ex = _bybit_with(c)
    assert ex.get_spot_min_notional("BTCUSDT") == 5.0


def test_bybit_spot_step_size_is_base_precision():
    c = _StubBybitClient(base_precision="0.000001")
    ex = _bybit_with(c)
    assert ex.get_spot_step_size("BTCUSDT") == 0.000001


def test_bybit_spot_free_balance():
    c = _StubBybitClient()
    ex = _bybit_with(c)
    assert ex.get_spot_balance("BTC") == 0.4   # availableToWithdraw, not walletBalance


def test_bybit_spot_below_min_qty_raises_into_result():
    c = _StubBybitClient(min_qty="1")  # require >= 1 BTC
    ex = _bybit_with(c)
    r = ex.spot_market_order("BTCUSDT", "buy", 0.01)
    assert not r.success and "minOrderQty" in r.error_message


def test_bybit_spot_instrument_cache_keyed_by_category():
    # Linear and spot BTCUSDT must NOT collide in the cache.
    c = _StubBybitClient()
    linear_inst = {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}

    def get_instruments_info(category, symbol):
        return {"result": {"list": [linear_inst if category == "linear" else c._inst]}}

    c.get_instruments_info = get_instruments_info
    ex = _bybit_with(c)
    assert ex.get_step_size("BTCUSDT") == 0.001        # linear qtyStep
    assert ex.get_spot_step_size("BTCUSDT") == 0.000001  # spot basePrecision
    assert ("linear", "BTCUSDT") in ex._instrument_cache
    assert ("spot", "BTCUSDT") in ex._instrument_cache


# --- 3. real Hyperliquid spot adapter against a stubbed spotMeta -------------

_SPOT_META = {
    "tokens": [
        {"name": "USDC", "index": 0, "szDecimals": 8},
        {"name": "UBTC", "index": 197, "szDecimals": 5},
        {"name": "UETH", "index": 221, "szDecimals": 4},
    ],
    "universe": [
        {"name": "@142", "tokens": [197, 0], "index": 142},  # UBTC/USDC
        {"name": "@151", "tokens": [221, 0], "index": 151},  # UETH/USDC
    ],
}


class _StubHLInfo:
    def __init__(self):
        self.mids = {"@142": "50000.0"}
        self.fills = []

    def spot_meta(self):
        return _SPOT_META

    def all_mids(self):
        return self.mids

    def spot_user_state(self, addr):
        return {"balances": [
            {"coin": "UBTC", "total": "0.5", "hold": "0.1"},
            {"coin": "USDC", "total": "1000.0", "hold": "0.0"},
        ]}

    def user_fills(self, addr):
        return self.fills


class _StubHLOrderExchange:
    """Captures the SDK market_open call and returns an OK spot fill."""

    def __init__(self):
        self.calls = []

    def market_open(self, name, is_buy, sz, px=None, slippage=0.01):
        self.calls.append({"name": name, "is_buy": is_buy, "sz": sz, "slippage": slippage})
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"oid": 9, "totalSz": str(sz), "avgPx": "50000.0"}}]}}}


def _hl_real() -> HyperliquidExchange:
    ex = HyperliquidExchange(private_key="", account_address="0xabc", dry_run=False)
    # Inject stubs (bypass the network-touching SDK constructors).
    ex._info = _StubHLInfo()
    ex._exchange = _StubHLOrderExchange()
    ex._account_address = "0xabc"
    return ex


def test_hl_spot_resolves_unit_pair_and_sizes():
    ex = _hl_real()
    # 'UBTC/USDC' -> canonical '@142'; szDecimals 5 -> qty rounded to 5dp.
    r = ex.spot_market_order("UBTC/USDC", "buy", 0.123456789)
    assert r.success
    call = ex._exchange.calls[0]
    assert call["name"] == "@142"          # canonical id, not the readable name
    assert call["is_buy"] is True
    assert call["sz"] == 0.12346           # rounded to 5 decimals
    assert r.filled_qty_base == 0.12346    # quote-denominated fee: base is gross


def test_hl_spot_resolves_from_canonical_base():
    ex = _hl_real()
    r = ex.spot_market_order("BTC", "buy", 0.01)   # 'BTC' -> 'UBTC' -> '@142'
    assert r.success and ex._exchange.calls[0]["name"] == "@142"


def test_hl_spot_step_size_from_szdecimals():
    ex = _hl_real()
    assert ex.get_spot_step_size("UBTC/USDC") == 10 ** -5
    assert ex.get_spot_step_size("UETH/USDC") == 10 ** -4


def test_hl_spot_balance_free_is_total_minus_hold():
    ex = _hl_real()
    assert ex.get_spot_balance("BTC") == pytest.approx(0.4)   # 0.5 - 0.1
    assert ex.get_spot_balance("ETH") == 0.0                  # not held


def test_hl_spot_min_notional_is_ten():
    ex = _hl_real()
    assert ex.get_spot_min_notional("UBTC/USDC") == 10.0


def test_hl_spot_unknown_token_errors_into_result():
    ex = _hl_real()
    r = ex.spot_market_order("USOL/USDC", "buy", 1.0)   # USOL not in stub meta
    assert not r.success and "no" in r.error_message.lower()


def test_hl_spot_dry_run_short_circuits_no_network():
    # No private key -> _exchange is None -> simulated net-base result, no SDK call.
    ex = HyperliquidExchange(private_key="", account_address="", dry_run=True)
    r = ex.spot_market_order("UBTC/USDC", "buy", 0.0123456)
    assert r.success and r.exchange_order_id == "DRY_RUN"
    assert r.filled_qty_base == 0.01235    # canonical 5dp rounding offline


def test_hl_spot_rounds_to_zero_fails():
    ex = HyperliquidExchange(private_key="", account_address="", dry_run=True)
    r = ex.spot_market_order("UBTC/USDC", "buy", 0.0000001)  # < 1e-5 -> 0
    assert not r.success and "rounded to 0" in r.error_message


# --- spot defensive / error branches (both venues) --------------------------

def test_bybit_spot_dry_run_no_network():
    """DRY_RUN spot order returns a net-base result without placing."""
    c = _StubBybitClient()
    ex = BybitExchange(api_key="", api_secret="", dry_run=True)
    ex._client = c
    r = ex.spot_market_order("BTCUSDT", "buy", 0.0100007)
    assert r.success and r.exchange_order_id == "DRY_RUN"
    assert r.filled_qty_base == 0.01          # snapped to basePrecision
    assert c.placed == []                     # never reached place_order


def test_bybit_spot_place_order_retcode_failure():
    c = _StubBybitClient()
    c.place_order = lambda **kw: {"retCode": 10001, "retMsg": "bad"}
    ex = _bybit_with(c)
    r = ex.spot_market_order("BTCUSDT", "buy", 0.01)
    assert not r.success and "10001" in r.error_message


def test_hl_spot_order_error_status_into_result():
    ex = _hl_real()
    ex._exchange.market_open = lambda **kw: {"status": "err", "msg": "rejected"}
    r = ex.spot_market_order("UBTC/USDC", "buy", 0.01)
    assert not r.success


def test_hl_spot_per_status_error_into_result():
    ex = _hl_real()
    ex._exchange.market_open = lambda **kw: {
        "status": "ok",
        "response": {"data": {"statuses": [{"error": "insufficient margin"}]}}}
    r = ex.spot_market_order("UBTC/USDC", "buy", 0.01)
    assert not r.success and "insufficient" in r.error_message


def test_hl_spot_no_pair_listed_errors():
    """Token exists but no USDC pair -> the 'no pair listed' branch."""
    ex = _hl_real()
    meta_no_pair = {
        "tokens": [{"name": "USDC", "index": 0, "szDecimals": 8},
                   {"name": "UBTC", "index": 197, "szDecimals": 5}],
        "universe": [],   # no pairs at all
    }
    ex._info.spot_meta = lambda: meta_no_pair
    r = ex.spot_market_order("UBTC/USDC", "buy", 0.01)
    assert not r.success and "no UBTC/USDC" in r.error_message


def test_hl_spot_balance_no_address_returns_zero():
    ex = HyperliquidExchange(private_key="", account_address="", dry_run=True)
    assert ex.get_spot_balance("BTC") == 0.0   # no account address


def test_hl_spot_step_size_falls_back_on_meta_failure():
    ex = _hl_real()
    def boom():
        raise RuntimeError("meta down")
    ex._info.spot_meta = boom
    assert ex.get_spot_step_size("UBTC/USDC") == 10 ** -5   # canonical fallback
