"""A.5 — the real `/funding-arb/*` API bodies (TestClient).

Auth precedence (503 unset > 401 bad header > 200), open/close/status behaviour,
idempotency, symbol-exclusivity (409), and the load-bearing shared-HL-address
refusal (the open is refused at registry construction, before any order reaches
an exchange).

The fake registry is installed via the `arb_registry` fixture (per-(name,account)
fakes sharing one `_SharedState`); the TestClient + secret come from a local
fixture. Background tasks run synchronously inside the TestClient context, so an
`open` that schedules `_run_open` actually fires the (fake) legs.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import ArbLeg, ArbOrder, ArbPosition

H = {"X-Arb-Secret": "s3cret"}


@pytest.fixture
def client_set(monkeypatch):
    monkeypatch.setenv("FUNDING_ARB_SECRET", "s3cret")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def client_unset(monkeypatch):
    monkeypatch.setenv("FUNDING_ARB_SECRET", "")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


# --- auth precedence --------------------------------------------------------

def test_secret_unset_503(client_unset):
    assert client_unset.post(
        "/funding-arb/open",
        json={"idempotency_key": "k", "asset": "BTC", "notional": 100},
    ).status_code == 503
    # 503 even WITH a header (unconfigured must not imply it works).
    assert client_unset.post(
        "/funding-arb/open", headers={"X-Arb-Secret": "x"},
        json={"idempotency_key": "k", "asset": "BTC", "notional": 100},
    ).status_code == 503
    assert client_unset.get("/funding-arb/positions").status_code == 503


def test_missing_or_bad_secret_401(client_set):
    assert client_set.post(
        "/funding-arb/open",
        json={"idempotency_key": "k", "asset": "BTC", "notional": 100},
    ).status_code == 401
    assert client_set.post(
        "/funding-arb/open", headers={"X-Arb-Secret": "wrong"},
        json={"idempotency_key": "k", "asset": "BTC", "notional": 100},
    ).status_code == 401


# --- open: accepted + sized legs --------------------------------------------

def test_open_default_combo_accepted_with_sized_legs(client_set, arb_registry):
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "k1", "asset": "BTC", "notional": 1000},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["arb_id"] >= 1
    # default = single-venue HL cash-and-carry: spot buy + perp sell, equal qty.
    assert {lg["exchange"] for lg in body["legs"]} == {"hyperliquid"}
    assert {lg["product"] for lg in body["legs"]} == {"spot", "perp"}
    assert sorted(lg["side"] for lg in body["legs"]) == ["buy", "sell"]
    qtys = {lg["target_qty"] for lg in body["legs"]}
    assert len(qtys) == 1 and qtys.pop() == pytest.approx(0.02)   # 1000/50000
    # persisted one ArbPosition + two ArbLegs.
    with session_scope() as db:
        assert db.query(ArbPosition).count() == 1
        assert db.query(ArbLeg).count() == 2


def test_open_explicit_bybit_combo(client_set, arb_registry):
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "kb", "asset": "ETH", "notional": 500,
              "legs": [{"exchange": "bybit", "product": "spot", "side": "buy"},
                       {"exchange": "bybit", "product": "perp", "side": "sell"}]},
    )
    assert r.status_code == 200, r.text
    legs = r.json()["legs"]
    assert {lg["exchange"] for lg in legs} == {"bybit"}
    # spot symbol is BTCUSDT-style, perp symbol too (Bybit shares the name).
    assert {lg["symbol"] for lg in legs} == {"ETHUSDT"}


def test_open_size_mode_min_optional_notional(client_set, arb_registry):
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "km", "asset": "SOL", "size_mode": "min"},
    )
    assert r.status_code == 200, r.text
    legs = r.json()["legs"]
    qtys = {lg["target_qty"] for lg in legs}
    assert len(qtys) == 1 and qtys.pop() > 0


# --- duplicate idempotency key ----------------------------------------------

def test_duplicate_idempotency_key_returns_duplicate(client_set, arb_registry):
    payload = {"idempotency_key": "dup", "asset": "BTC", "notional": 1000}
    r1 = client_set.post("/funding-arb/open", headers=H, json=payload)
    assert r1.status_code == 200 and r1.json()["status"] == "accepted"
    r2 = client_set.post("/funding-arb/open", headers=H, json=payload)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"
    assert r2.json()["arb_id"] == r1.json()["arb_id"]
    # exactly ONE ArbPosition for the key.
    with session_scope() as db:
        assert db.query(ArbPosition).filter(
            ArbPosition.idempotency_key == "dup").count() == 1


# --- symbol exclusivity (409) -----------------------------------------------

def test_symbol_already_open_rejected_409(client_set, arb_registry):
    r1 = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "x1", "asset": "BTC", "notional": 1000},
    )
    assert r1.status_code == 200
    # a SECOND arb on the same BTC HL legs (different idem key) must 409.
    r2 = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "x2", "asset": "BTC", "notional": 1000},
    )
    assert r2.status_code == 409, r2.text
    # only the first arb persisted.
    with session_scope() as db:
        assert db.query(ArbPosition).count() == 1


def test_closed_arb_frees_its_symbol(client_set, arb_registry):
    """A CLOSED arb no longer holds its symbol — a new arb on it is accepted."""
    r1 = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "c1", "asset": "BTC", "notional": 1000},
    )
    arb_id = r1.json()["arb_id"]
    # force it closed directly (close flow tested separately).
    with session_scope() as db:
        db.get(ArbPosition, arb_id).status = "closed"
    r2 = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "c2", "asset": "BTC", "notional": 1000},
    )
    assert r2.status_code == 200, r2.text


# --- status: legs + neutral -------------------------------------------------

def test_status_shows_legs_and_neutral(client_set, arb_registry):
    arb_registry.state.next_result = arb_registry.state.spot_result = (
        __import__("app.schemas", fromlist=["OrderResult"]).OrderResult(
            success=True, exchange_order_id="OK", filled_qty_base=0.02,
            avg_price=50000.0))
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "s1", "asset": "BTC", "notional": 1000},
    )
    arb_id = r.json()["arb_id"]   # background _run_open ran (both legs filled)
    view = client_set.get(f"/funding-arb/positions/{arb_id}", headers=H)
    assert view.status_code == 200
    body = view.json()
    assert body["status"] == "open"
    assert body["neutral"] is True
    assert body["neutrality_skew"] == pytest.approx(0.0)
    assert len(body["legs"]) == 2
    assert all(lg["status"] == "success" for lg in body["legs"])
    assert view.headers["cache-control"] == "no-store"
    # the list endpoint also returns it.
    lst = client_set.get("/funding-arb/positions", headers=H)
    assert lst.status_code == 200 and any(p["arb_id"] == arb_id for p in lst.json())


def test_get_unknown_position_404(client_set, arb_registry):
    assert client_set.get("/funding-arb/positions/999999", headers=H).status_code == 404


# --- close ------------------------------------------------------------------

def test_close_fires_per_leg_calls(client_set, arb_registry):
    arb_registry.state.next_result = arb_registry.state.spot_result = (
        __import__("app.schemas", fromlist=["OrderResult"]).OrderResult(
            success=True, exchange_order_id="OK", filled_qty_base=0.02,
            avg_price=50000.0))
    arb_registry.state.spot_balances["BTC"] = 0.02
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "cl1", "asset": "BTC", "notional": 1000},
    )
    arb_id = r.json()["arb_id"]
    arb_registry.state.calls.clear()   # focus on close-side calls

    rc = client_set.post("/funding-arb/close", headers=H, json={"arb_id": arb_id})
    assert rc.status_code == 200 and rc.json()["status"] == "closing"
    # background _run_close fired: perp close_position + spot sell.
    assert any(c[0] == "close" and c[1] == "BTC" for c in arb_registry.state.calls)
    assert any(c[0] == "spot_market" and c[2] == "sell" for c in arb_registry.state.calls)
    with session_scope() as db:
        assert db.get(ArbPosition, arb_id).status == "closed"


def test_close_unknown_id_404(client_set, arb_registry):
    assert client_set.post(
        "/funding-arb/close", headers=H, json={"arb_id": 999999}).status_code == 404


def test_close_already_closed_409(client_set, arb_registry):
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "cc1", "asset": "BTC", "notional": 1000},
    )
    arb_id = r.json()["arb_id"]
    with session_scope() as db:
        db.get(ArbPosition, arb_id).status = "closed"
    rc = client_set.post("/funding-arb/close", headers=H, json={"arb_id": arb_id})
    assert rc.status_code == 409


# --- HL spot leg accepted (no 422) ------------------------------------------

def test_hl_spot_leg_accepted(client_set, arb_registry):
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "hl1", "asset": "BTC", "notional": 1000,
              "legs": [{"exchange": "hyperliquid", "product": "spot", "side": "buy"},
                       {"exchange": "hyperliquid", "product": "perp", "side": "sell"}]},
    )
    assert r.status_code == 200, r.text
    legs = r.json()["legs"]
    spot = next(lg for lg in legs if lg["product"] == "spot")
    assert spot["symbol"] == "UBTC/USDC"


# --- shared HL address: open refused BEFORE any order reaches the fake -------

def test_shared_hl_address_open_refused_before_any_order(client_set, monkeypatch):
    """If the HL arb account resolves to the SAME address as the directional
    account, the registry guard raises at construction. The open must be refused
    (503) BEFORE any order is placed and BEFORE any ArbPosition is persisted."""
    from app.exchanges import hyperliquid as hl_mod
    from app.exchanges import registry as reg_mod

    # Build a REAL registry (guard active) with the HL SDK stubbed offline.
    _KEY = "0x0000000000000000000000000000000000000000000000000000000000000001"
    _ADDR = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"  # address of _KEY

    placed = {"orders": 0}

    class _StubHLExchange:
        def __init__(self, wallet, base_url, **kwargs):
            self.account_address = kwargs.get("account_address")
        def market_open(self, *a, **k):
            placed["orders"] += 1
            return {"status": "ok"}

    monkeypatch.setattr(hl_mod, "HLExchange", _StubHLExchange)
    monkeypatch.setattr(hl_mod, "Info", lambda *a, **kw: object())
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", _ADDR)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", _KEY)
    monkeypatch.setenv("HYPERLIQUID_ARB_PRIVATE_KEY", _KEY)      # derives _ADDR
    monkeypatch.setenv("HYPERLIQUID_ARB_ACCOUNT_ADDRESS", _ADDR)  # == directional
    get_settings.cache_clear()
    reg_mod.reset_registry()   # force the REAL guarded registry

    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "shared", "asset": "BTC", "notional": 1000},
    )
    # refused at construction -> 503, before any order or DB write.
    assert r.status_code == 503, r.text
    assert placed["orders"] == 0
    with session_scope() as db:
        assert db.query(ArbPosition).count() == 0
        assert db.query(ArbOrder).count() == 0

    reg_mod.reset_registry()
    get_settings.cache_clear()


# --- global kill-switch: halts opens, never closes --------------------------

def _set_kill(on: bool):
    from app.risk import update_risk_settings
    with session_scope() as db:
        update_risk_settings(db, kill_switch=on)


def test_kill_switch_blocks_open_503(client_set, arb_registry):
    _set_kill(True)
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "ks1", "asset": "BTC", "notional": 1000},
    )
    assert r.status_code == 503, r.text
    assert "kill-switch" in r.json()["detail"].lower()
    # refused before persisting — no NEW exposure created.
    with session_scope() as db:
        assert db.query(ArbPosition).count() == 0


def test_kill_switch_still_allows_close(client_set, arb_registry):
    # open with the switch OFF...
    r1 = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "kc1", "asset": "BTC", "notional": 1000},
    )
    assert r1.status_code == 200, r1.text
    arb_id = r1.json()["arb_id"]
    # ...flip it ON; CLOSE must still be accepted (de-risk is never gated).
    _set_kill(True)
    r2 = client_set.post("/funding-arb/close", headers=H, json={"arb_id": arb_id})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "closing"


def test_kill_switch_off_open_accepted(client_set, arb_registry):
    _set_kill(False)
    r = client_set.post(
        "/funding-arb/open", headers=H,
        json={"idempotency_key": "ko1", "asset": "BTC", "notional": 1000},
    )
    assert r.status_code == 200 and r.json()["status"] == "accepted"
