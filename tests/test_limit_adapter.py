"""P0 — resting limit-order adapter surface (limit_order / cancel_order / order_status).

Three layers, mirroring test_arb_spot_adapter.py:
  1. FakeExchange (conftest) through the registry — place → working, scripted fill
     trajectory via state.limit_orders, cancel.
  2. Real BybitExchange against a stubbed pybit client — GTC Limit placement with
     tickSize-rounded price + orderLinkId, open→history status, cancel idempotency.
  3. Real HyperliquidExchange against stubbed _exchange/_info — price grid rounding
     (incl. passing the SDK's own float_to_wire gate), resting vs marketable, cancel,
     status from query_order_by_oid + user_fills, cloid conversion.
"""
from __future__ import annotations

import pytest

from app.exchanges.bybit import BybitExchange
from app.exchanges.hyperliquid import HyperliquidExchange
from app.schemas import (OrderStatus, ORDER_STATE_WORKING, ORDER_STATE_PARTIAL,
                         ORDER_STATE_FILLED, ORDER_STATE_CANCELLED, ORDER_STATE_UNKNOWN)


# --- 1. FakeExchange through the registry -----------------------------------

def test_fake_limit_lifecycle(stub_exchange):
    r = stub_exchange.limit_order("SOLUSDT", "buy", 2.0, 69.8, client_order_id="0xabc")
    assert r.success and r.filled_qty_base == 0.0
    oid = r.exchange_order_id
    # resting: working, nothing filled
    st = stub_exchange.order_status("SOLUSDT", oid)
    assert st.state == "working" and st.filled_qty_base == 0.0
    # script a partial fill, then a full fill, by mutating shared state
    stub_exchange.limit_orders[oid].update(filled=1.0, avg=69.8, state="partially_filled")
    st = stub_exchange.order_status("SOLUSDT", oid)
    assert st.state == "partially_filled" and st.filled_qty_base == 1.0
    # cancel flips it terminal
    assert stub_exchange.cancel_order("SOLUSDT", oid) is True
    assert stub_exchange.order_status("SOLUSDT", oid).state == "cancelled"


def test_fake_order_status_unknown_id(stub_exchange):
    assert stub_exchange.order_status("SOLUSDT", "nope").state == "unknown"


# --- 2. real Bybit adapter against a stubbed pybit client -------------------

class _StubBybitLimitClient:
    def __init__(self, tick="0.1", qty_step="0.01"):
        self._inst = {"lotSizeFilter": {"qtyStep": qty_step, "minOrderQty": "0"},
                      "priceFilter": {"tickSize": tick}}
        self.placed: list = []
        self.cancelled: list = []
        self._open: dict = {}
        self._hist: dict = {}
        self.cancel_retcode = 0

    def get_instruments_info(self, category, symbol):
        return {"result": {"list": [self._inst]}}

    def set_leverage(self, **kw):
        return {"retCode": 0}

    def place_order(self, **kw):
        self.placed.append(kw)
        oid = "OID1"
        self._open[oid] = {"orderId": oid, "orderStatus": "New", "cumExecQty": "0",
                           "avgPrice": "", "cumExecFee": "0",
                           "orderLinkId": kw.get("orderLinkId", "")}
        return {"retCode": 0, "result": {"orderId": oid, "orderLinkId": kw.get("orderLinkId", "")}}

    def cancel_order(self, category, symbol, orderId):
        self.cancelled.append(orderId)
        return {"retCode": self.cancel_retcode,
                "retMsg": "order not exists" if self.cancel_retcode else "OK",
                "result": {"orderId": orderId}}

    def get_open_orders(self, category, symbol, orderId=None):
        rows = [r for oid, r in self._open.items() if orderId in (None, oid)]
        return {"result": {"list": rows}}

    def get_order_history(self, category, symbol, orderId=None):
        rows = [r for oid, r in self._hist.items() if orderId in (None, oid)]
        return {"result": {"list": rows}}


def _bybit_with(client) -> BybitExchange:
    ex = BybitExchange(api_key="k", api_secret="s", dry_run=False)
    ex._client = client
    return ex


def test_bybit_round_price_to_tick():
    ex = BybitExchange(api_key="", api_secret="", dry_run=True)
    ex._instrument_cache[("linear", "SOLUSDT")] = {"priceFilter": {"tickSize": "0.1"}}
    assert ex._round_price("SOLUSDT", 69.84) == "69.8"
    assert ex._round_price("SOLUSDT", 69.86) == "69.9"      # nearest tick


def test_bybit_limit_places_gtc_with_link_and_rounded_price():
    c = _StubBybitLimitClient(tick="0.1")
    ex = _bybit_with(c)
    r = ex.limit_order("SOLUSDT", "buy", 2.007, 69.84, client_order_id="0xfeed")
    assert r.success and r.exchange_order_id == "OID1" and r.filled_qty_base == 0.0
    p = c.placed[0]
    assert p["orderType"] == "Limit" and p["timeInForce"] == "GTC"
    assert p["side"] == "Buy" and p["qty"] == "2"        # snapped to 0.01 step
    assert p["price"] == "69.8"                          # snapped to 0.1 tick
    assert p["orderLinkId"] == "0xfeed"


def test_bybit_order_status_open_partial():
    c = _StubBybitLimitClient()
    ex = _bybit_with(c)
    ex.limit_order("SOLUSDT", "buy", 2.0, 69.8, client_order_id="x")
    c._open["OID1"].update(orderStatus="PartiallyFilled", cumExecQty="0.5",
                           avgPrice="69.8", cumExecFee="0.01")
    st = ex.order_status("SOLUSDT", "OID1")
    assert st.state == ORDER_STATE_PARTIAL
    assert st.filled_qty_base == 0.5 and st.avg_price == 69.8 and st.commission == 0.01


def test_bybit_order_status_falls_back_to_history_when_terminal():
    """A Filled order leaves the open book — order_status must read get_order_history."""
    c = _StubBybitLimitClient()
    ex = _bybit_with(c)
    ex.limit_order("SOLUSDT", "buy", 2.0, 69.8, client_order_id="x")
    del c._open["OID1"]                                   # no longer live
    c._hist["OID1"] = {"orderId": "OID1", "orderStatus": "Filled", "cumExecQty": "2.0",
                       "avgPrice": "69.8", "cumExecFee": "0.05"}
    st = ex.order_status("SOLUSDT", "OID1")
    assert st.state == ORDER_STATE_FILLED and st.filled_qty_base == 2.0


def test_bybit_cancel_ok_and_idempotent():
    c = _StubBybitLimitClient()
    ex = _bybit_with(c)
    assert ex.cancel_order("SOLUSDT", "OID1") is True
    assert c.cancelled == ["OID1"]
    c.cancel_retcode = 110001                             # already gone
    assert ex.cancel_order("SOLUSDT", "OID1") is True     # idempotent success


def test_bybit_limit_dry_run_no_network():
    c = _StubBybitLimitClient()
    ex = BybitExchange(api_key="", api_secret="", dry_run=True)
    ex._client = c
    r = ex.limit_order("SOLUSDT", "buy", 2.0, 69.84, client_order_id="0xfeed")
    assert r.success and r.exchange_order_id == "0xfeed" and r.filled_qty_base == 0.0
    assert c.placed == []                                 # never reached place_order


# --- 3. real Hyperliquid adapter against stubbed _exchange/_info ------------

class _StubHLLimitExchange:
    def __init__(self, resting_oid=42):
        self.orders: list = []
        self.cancels: list = []
        self._resp = {"status": "ok", "response": {"data": {"statuses": [
            {"resting": {"oid": resting_oid}}]}}}

    def update_leverage(self, lev, name):
        return {"status": "ok"}

    def order(self, **kw):
        self.orders.append(kw)
        return self._resp

    def cancel(self, name, oid):
        self.cancels.append((name, oid))
        return {"status": "ok", "response": {"data": {"statuses": ["success"]}}}


class _StubHLLimitInfo:
    def __init__(self):
        self._meta = {"universe": [{"name": "SOL", "szDecimals": 2},
                                   {"name": "BTC", "szDecimals": 5}]}
        self.fills: list = []
        self.order_state = {"status": "order", "order": {"status": "open", "order": {}}}

    def meta(self):
        return self._meta

    def user_fills(self, addr):
        return self.fills

    def query_order_by_oid(self, addr, oid):
        return self.order_state


def _hl_limit() -> HyperliquidExchange:
    ex = HyperliquidExchange(private_key="", account_address="0xabc", dry_run=False)
    ex._info = _StubHLLimitInfo()
    ex._exchange = _StubHLLimitExchange()
    ex._account_address = "0xabc"
    return ex


def test_hl_round_price_sig_figs_and_decimals():
    ex = _hl_limit()
    # SOL szDecimals 2 -> price decimals = 6-2 = 4; 5 sig figs.
    assert ex._round_price("SOL", 69.80123) == 69.801
    # BTC szDecimals 5 -> 1 decimal; 5 sig figs snaps 105432.7 -> 105430.0
    assert ex._round_price("BTC", 105432.7) == 105430.0


def test_hl_round_price_passes_sdk_float_to_wire():
    """The rounded price must satisfy HL's OWN client-side gate (float_to_wire raises
    on an off-grid price) — the real failure mode if rounding is wrong."""
    from hyperliquid.utils.signing import float_to_wire
    ex = _hl_limit()
    for sym, px in [("SOL", 69.80123), ("SOL", 0.0123456), ("BTC", 105432.7),
                    ("BTC", 50000.0), ("SOL", 142.0)]:
        float_to_wire(ex._round_price(sym, px))           # must not raise


def test_hl_limit_resting_returns_oid():
    ex = _hl_limit()
    r = ex.limit_order("SOL", "buy", 1.0, 69.8, client_order_id="0x" + "a" * 32)
    assert r.success and r.exchange_order_id == "42" and r.filled_qty_base == 0.0
    o = ex._exchange.orders[0]
    assert o["order_type"] == {"limit": {"tif": "Gtc"}} and o["is_buy"] is True
    assert "cloid" in o                                   # valid hex client id -> cloid attached


def test_hl_limit_marketable_immediate_fill():
    ex = _hl_limit()
    ex._exchange._resp = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"oid": 7, "totalSz": "1.0", "avgPx": "69.5"}}]}}}
    r = ex.limit_order("SOL", "buy", 1.0, 69.8)
    assert r.success and r.filled_qty_base == 1.0 and r.avg_price == 69.5


def test_hl_limit_error_status_into_result():
    ex = _hl_limit()
    ex._exchange._resp = {"status": "ok", "response": {"data": {"statuses": [
        {"error": "insufficient margin"}]}}}
    r = ex.limit_order("SOL", "buy", 1.0, 69.8)
    assert not r.success and "insufficient" in r.error_message


def test_hl_cancel_by_int_oid():
    ex = _hl_limit()
    assert ex.cancel_order("SOL", "42") is True
    assert ex._exchange.cancels == [("SOL", 42)]          # oid cast to int


def test_hl_order_status_open_no_fills():
    ex = _hl_limit()
    st = ex.order_status("SOL", "42")
    assert st.state == ORDER_STATE_WORKING and st.filled_qty_base == 0.0


def test_hl_order_status_partial_from_fills():
    ex = _hl_limit()
    ex._info.fills = [{"oid": 42, "sz": "0.5", "px": "69.8", "fee": "0.01", "feeToken": "USDC"}]
    st = ex.order_status("SOL", "42")
    assert st.state == ORDER_STATE_PARTIAL
    assert st.filled_qty_base == 0.5 and st.avg_price == 69.8 and st.commission == 0.01


def test_hl_order_status_filled():
    ex = _hl_limit()
    ex._info.order_state = {"status": "order", "order": {"status": "filled", "order": {}}}
    ex._info.fills = [{"oid": 42, "sz": "1.0", "px": "69.8", "fee": "0.02", "feeToken": "USDC"}]
    st = ex.order_status("SOL", "42")
    assert st.state == ORDER_STATE_FILLED and st.filled_qty_base == 1.0


def test_hl_order_status_unknown_oid():
    ex = _hl_limit()
    ex._info.order_state = {"status": "unknownOid"}
    assert ex.order_status("SOL", "999").state == ORDER_STATE_UNKNOWN


def test_hl_to_cloid_valid_and_invalid():
    assert HyperliquidExchange._to_cloid("0x" + "a" * 32) is not None
    assert HyperliquidExchange._to_cloid("mochi-1") is None      # not 16-byte hex
    assert HyperliquidExchange._to_cloid("") is None


def test_hl_limit_dry_run_no_network():
    ex = HyperliquidExchange(private_key="", account_address="", dry_run=True)
    ex._info = _StubHLLimitInfo()
    r = ex.limit_order("SOL", "buy", 1.0, 69.8, client_order_id="0xdead")
    assert r.success and r.exchange_order_id == "0xdead" and r.filled_qty_base == 0.0
