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

import os
from typing import get_args

import pytest

from app.exchanges.bybit import BybitExchange
from app.exchanges.hyperliquid import HyperliquidExchange, _HL_SPOT_SZ_DECIMALS
from app.exchanges.symbols import (CANONICAL_STEP_SIZES, HYPERLIQUID_NATIVE_SPOT,
                                   SUPPORTED_BASE_ASSETS, hyperliquid_spot_token,
                                   spot_symbol_for, symbol_for)
from app.schemas import OrderResult
from app.schemas_arb import Asset


# --- spot symbol mapping -----------------------------------------------------

def test_spot_symbol_for_both_venues():
    assert spot_symbol_for("bybit", "BTC") == "BTCUSDT"
    assert spot_symbol_for("hyperliquid", "BTC") == "UBTC/USDC"
    assert spot_symbol_for("hyperliquid", "ETH") == "UETH/USDC"
    assert spot_symbol_for("hyperliquid", "SOL") == "USOL/USDC"
    # HL-native tokens trade under their BARE name (no 'U' prefix).
    assert spot_symbol_for("hyperliquid", "HYPE") == "HYPE/USDC"
    assert spot_symbol_for("hyperliquid", "PURR") == "PURR/USDC"
    assert spot_symbol_for("bybit", "HYPE") == "HYPEUSDT"
    with pytest.raises(ValueError):
        spot_symbol_for("hyperliquid", "DOGE")
    with pytest.raises(ValueError):
        spot_symbol_for("kraken", "BTC")


def test_hl_spot_base_token_native_vs_unit():
    """HL-native tokens (HYPE/PURR) resolve to their BARE token; Unit majors keep the
    'U' prefix. This is what lets 'HYPE/USDC' resolve to its real pair instead of a
    non-existent 'UHYPE'."""
    ex = HyperliquidExchange(private_key="", account_address="", dry_run=True)
    assert ex._spot_base_token("UBTC/USDC") == "UBTC"
    assert ex._spot_base_token("HYPE/USDC") == "HYPE"     # not 'UHYPE'
    assert ex._spot_base_token("PURR/USDC") == "PURR"     # not 'UPURR'
    assert ex._spot_base_token("BTC") == "UBTC"           # bare canonical -> Unit token
    assert ex._spot_base_token("HYPE") == "HYPE"          # bare native stays native


def test_hl_canonical_spot_decimals_native():
    from app.exchanges.hyperliquid import _canonical_spot_decimals
    assert _canonical_spot_decimals("UBTC/USDC") == 5     # Unit major, unchanged
    assert _canonical_spot_decimals("HYPE/USDC") == 2
    assert _canonical_spot_decimals("PURR/USDC") == 0     # whole units


def test_hl_spot_dry_run_native_tokens():
    """DRY_RUN spot orders for the native tokens round to their szDecimals (HYPE 2dp,
    PURR whole units), no network."""
    ex = HyperliquidExchange(private_key="", account_address="", dry_run=True)
    r = ex.spot_market_order("HYPE/USDC", "buy", 1.239)
    assert r.success and r.filled_qty_base == 1.24
    r2 = ex.spot_market_order("PURR/USDC", "buy", 33.7)
    assert r2.success and r2.filled_qty_base == 34.0


def test_arb_open_request_accepts_native_assets():
    """The arb API accepts the HL-native assets HYPE/PURR (and still rejects junk)."""
    from app.schemas_arb import ArbOpenRequest
    for a in ("BTC", "ETH", "SOL", "HYPE", "PURR"):
        ArbOpenRequest(idempotency_key="k", asset=a, size_mode="min")
    with pytest.raises(Exception):
        ArbOpenRequest(idempotency_key="k", asset="DOGE", size_mode="min")


# --- adding a new arb coin must stay fully wired (regression: the PURR rollout) ----

@pytest.mark.parametrize("asset", get_args(Asset))
def test_every_arb_asset_is_fully_wired(asset):
    """Every coin in the arb `Asset` enum must have its HL perp + spot symbols, a
    canonical step size, and an offline spot-szDecimals entry. Adding a coin to the
    enum without wiring these fails HERE, not at order time on the box."""
    assert asset in SUPPORTED_BASE_ASSETS
    assert symbol_for("hyperliquid", asset)                  # perp symbol resolves
    assert spot_symbol_for("hyperliquid", asset)             # spot symbol resolves
    assert CANONICAL_STEP_SIZES.get(asset, 0) > 0            # sizing step present
    assert asset in _HL_SPOT_SZ_DECIMALS                     # offline spot szDecimals present


@pytest.mark.parametrize("asset", get_args(Asset))
def test_arb_asset_native_unit_spot_consistent(asset):
    """The adapter must recover the SAME spot base token the symbols module encodes, so
    a native coin (HYPE/PURR → bare token) and a Unit-bridged coin (BTC → UBTC) can't
    drift. A native coin NOT marked native would resolve to a bogus 'U'+name — the
    original PURR/HYPE bug — and fail this."""
    ex = HyperliquidExchange(private_key="", account_address="", dry_run=True)
    spot_sym = spot_symbol_for("hyperliquid", asset)         # 'HYPE/USDC' | 'UBTC/USDC'
    assert ex._spot_base_token(spot_sym) == hyperliquid_spot_token(asset)
    if asset in HYPERLIQUID_NATIVE_SPOT:
        assert hyperliquid_spot_token(asset) == asset and not spot_sym.startswith("U")
    else:
        assert hyperliquid_spot_token(asset) == "U" + asset


@pytest.mark.skipif(not os.getenv("HL_LIVE_TESTS"),
                    reason="opt-in live check; run `HL_LIVE_TESTS=1 pytest -k live_arb_asset` when adding a coin")
@pytest.mark.parametrize("asset", get_args(Asset))
def test_live_arb_asset_has_hl_perp_and_spot(asset):
    """DEFINITIVE check against HL's public API (opt-in): every arb asset has an HL perp
    AND a USDC spot pair under the token name our code resolves — i.e. the native/Unit
    classification is actually correct on HL. RUN THIS WHEN ADDING A COIN."""
    import requests
    info = "https://api.hyperliquid.xyz/info"
    perps = {u["name"] for u in requests.post(info, json={"type": "meta"}, timeout=15).json()["universe"]}
    assert symbol_for("hyperliquid", asset) in perps, f"{asset}: no HL perp"
    sm = requests.post(info, json={"type": "spotMeta"}, timeout=15).json()
    idx = {t["name"].upper(): t["index"] for t in sm["tokens"]}
    tok = hyperliquid_spot_token(asset).upper()
    assert tok in idx, f"{asset}: spot token {tok} not on HL (native/Unit misclassified?)"
    usdc = idx.get("USDC")
    assert any(len(p["tokens"]) == 2 and p["tokens"][0] == idx[tok] and p["tokens"][1] == usdc
               for p in sm["universe"]), f"{asset}: no {tok}/USDC spot pair on HL"


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


def test_bybit_round_qty_dust_tolerant(monkeypatch):
    """A managed-close abs(net) a hair below a step multiple (0.34 stored as
    0.33999999999999997 by the RMW ledger) must snap to 0.34, not drop to 0.33 — that
    under-closes and leaves a residual. A genuine sub-step (0.335) still floors."""
    ex = BybitExchange(api_key="k", api_secret="s", dry_run=True)
    monkeypatch.setattr(ex, "_instrument",
                        lambda symbol: {"lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0"}})
    assert ex._round_qty("BNBUSDT", 0.34) == "0.34"
    assert ex._round_qty("BNBUSDT", 0.33999999999999997) == "0.34"   # float dust -> snap up
    assert ex._round_qty("BNBUSDT", 0.335) == "0.33"                 # genuine sub-step -> floor
    assert ex._round_qty("BNBUSDT", 0.33) == "0.33"


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
        # PERP 'BTC' keys directly; SPOT 'UBTC/USDC' is keyed by its canonical
        # pair name '@142' (verified live — all_mids has '@142', not 'UBTC').
        self.mids = {"@142": "50000.0", "BTC": "50010.0"}
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


def test_hl_spot_buy_nets_base_denominated_fee(monkeypatch):
    """HL spot BUY fee is charged in the base coin → filled_qty_base is NET of it (the
    hedgeable held base, like Bybit); a SELL fee is USDC → the base sold stays gross."""
    ex = _hl_real()
    monkeypatch.setattr(ex, "_fill_fee", lambda oid: (0.0001, "UBTC"))   # base-denominated
    rb = ex.spot_market_order("UBTC/USDC", "buy", 0.02)
    assert rb.success and rb.commission_asset == "UBTC"
    assert rb.filled_qty_base == pytest.approx(0.02 - 0.0001)            # net of the base fee
    monkeypatch.setattr(ex, "_fill_fee", lambda oid: (0.05, "USDC"))     # quote-denominated
    rs = ex.spot_market_order("UBTC/USDC", "sell", 0.02)
    assert rs.success and rs.filled_qty_base == pytest.approx(0.02)      # base sold stays gross


def test_hl_get_price_spot_pair_returns_spot_mid_no_warning(caplog):
    """A spot pair ('UBTC/USDC') resolves to its canonical '@142' and reads THAT
    key's mid from all_mids — with NO warning logged (the noisy 'symbol not
    found' that arb sizing used to emit per leg)."""
    ex = _hl_real()
    with caplog.at_level("WARNING", logger="app.exchanges.hyperliquid"):
        px = ex.get_price("UBTC/USDC")
    assert px == 50000.0                       # all_mids['@142'], the spot mid
    assert "@142" not in caplog.text           # never logged as a failure
    assert not any(r.levelno >= 30 for r in caplog.records)   # no WARNING+


def test_hl_get_price_perp_unchanged(caplog):
    """A perp symbol ('BTC') still keys all_mids directly — no spot resolution,
    no warning."""
    ex = _hl_real()
    with caplog.at_level("WARNING", logger="app.exchanges.hyperliquid"):
        px = ex.get_price("BTC")
    assert px == 50010.0                       # all_mids['BTC']
    assert not any(r.levelno >= 30 for r in caplog.records)


def test_hl_get_price_unknown_perp_still_warns_and_zeros(caplog):
    """Genuine miss (perp not in all_mids) keeps the existing 0.0 + warning
    contract — the spot-awareness change must not swallow real failures."""
    ex = _hl_real()
    with caplog.at_level("WARNING", logger="app.exchanges.hyperliquid"):
        px = ex.get_price("DOGE")
    assert px == 0.0
    assert "get_price failed" in caplog.text


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
