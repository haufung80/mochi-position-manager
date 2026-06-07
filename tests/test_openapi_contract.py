"""Phase-0 contract gate for the /funding-arb API.

Two kinds of assertion:

1. **Contract shape** — the funding-arb paths/schemas/security scheme are present
   and carry the right fields, AND the load-bearing design change holds:
   ``(exchange=hyperliquid, product=spot)`` is ACCEPTED (no 422). Also exercises
   the stub routes + the 503/401 auth branches for coverage.
2. **Staleness** — regenerating the contract-grade schema in a FRESH process must
   reproduce the committed ``docs/openapi-funding-arb.json`` byte-for-byte, so a
   schema change that isn't re-dumped fails CI.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import get_settings
from app.main import app
from app.schemas_arb import ArbOpenRequest, LegSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNDING_ARB_JSON = REPO_ROOT / "docs" / "openapi-funding-arb.json"

WRITE_PATHS = ("/funding-arb/open", "/funding-arb/close")
ALL_ARB_API_PATHS = (
    "/funding-arb/open",
    "/funding-arb/close",
    "/funding-arb/positions",
    "/funding-arb/positions/{arb_id}",
)


@pytest.fixture
def schema():
    """The freshly-generated full OpenAPI schema (memoization cleared)."""
    app.openapi_schema = None
    s = app.openapi()
    app.openapi_schema = None  # don't leak a memoized schema to other tests
    return s


# --- app.openapi() succeeds + all paths present -----------------------------

def test_openapi_generates(schema):
    assert schema["openapi"].startswith("3.")
    assert "paths" in schema and "components" in schema


def test_all_funding_arb_paths_present(schema):
    for p in ALL_ARB_API_PATHS:
        assert p in schema["paths"], f"missing path {p}"
    # the HTML reporting root is also mounted
    assert "/funding-arb" in schema["paths"]


# --- open/close request+response schemas with the right fields --------------

def test_open_request_response_schemas(schema):
    schemas = schema["components"]["schemas"]
    open_req = schemas["ArbOpenRequest"]["properties"]
    for field in ("idempotency_key", "asset", "notional", "size_mode",
                  "strategy_tag", "legs"):
        assert field in open_req, f"ArbOpenRequest missing {field}"
    # size_mode is the new field — assert explicitly (per the brief).
    assert "size_mode" in open_req

    open_resp = schemas["ArbOpenResponse"]["properties"]
    for field in ("status", "arb_id", "idempotency_key", "legs"):
        assert field in open_resp

    close_req = schemas["ArbCloseRequest"]["properties"]
    assert "arb_id" in close_req
    close_resp = schemas["ArbCloseResponse"]["properties"]
    assert {"status", "arb_id"} <= set(close_resp)


def test_position_view_and_pnl_schemas(schema):
    schemas = schema["components"]["schemas"]
    pos = schemas["ArbPositionView"]["properties"]
    for field in ("arb_id", "asset", "status", "neutral", "neutrality_skew",
                  "legs", "pnl", "opened_at", "closed_at", "error_message"):
        assert field in pos
    pnl = schemas["ArbPnL"]["properties"]
    for field in ("funding_total", "funding_by_leg", "commission_total",
                  "spot_unrealized", "perp_unrealized", "directional_net", "net"):
        assert field in pnl
    leg = schemas["ArbLegView"]["properties"]
    for field in ("exchange", "account", "product", "symbol", "side",
                  "target_qty", "filled_qty", "avg_fill", "funding", "status"):
        assert field in leg


def test_asset_is_btc_eth_sol(schema):
    asset = schema["components"]["schemas"]["ArbOpenRequest"]["properties"]["asset"]
    assert asset["enum"] == ["BTC", "ETH", "SOL"]


# --- ArbSecret security scheme exists + applied to the write routes ---------

def test_arb_secret_scheme_present_and_applied(schema):
    schemes = schema["components"].get("securitySchemes", {})
    assert "ArbSecret" in schemes
    assert schemes["ArbSecret"]["type"] == "apiKey"
    assert schemes["ArbSecret"]["in"] == "header"
    assert schemes["ArbSecret"]["name"] == "X-Arb-Secret"

    for path in WRITE_PATHS:
        post = schema["paths"][path]["post"]
        assert {"ArbSecret": []} in post["security"], f"{path} missing ArbSecret security"


def test_write_routes_document_error_responses(schema):
    open_resp = schema["paths"]["/funding-arb/open"]["post"]["responses"]
    # 409 added for symbol-exclusivity (a leg already held by a non-closed arb).
    assert {"401", "409", "503"} <= set(open_resp)
    close_resp = schema["paths"]["/funding-arb/close"]["post"]["responses"]
    assert {"401", "404", "409", "503"} <= set(close_resp)


# --- the load-bearing design change: HL spot is ACCEPTED, not 422 -----------

def test_hyperliquid_spot_leg_is_accepted():
    """The plan's {hyperliquid, spot} -> 422 rejection is REMOVED. A LegSpec with
    that combo must validate (HL spot is first-class in the contract)."""
    leg = LegSpec(exchange="hyperliquid", product="spot", side="buy")
    assert leg.exchange == "hyperliquid" and leg.product == "spot"

    # And an explicit HL-spot + HL-perp delta-neutral pair must validate too.
    req = ArbOpenRequest(
        idempotency_key="k1",
        asset="BTC",
        notional=1000.0,
        legs=[
            LegSpec(exchange="hyperliquid", product="spot", side="buy"),
            LegSpec(exchange="hyperliquid", product="perp", side="sell"),
        ],
    )
    assert len(req.legs) == 2


def test_default_combo_example_present(schema):
    """The default single-venue HL cash-and-carry example must be in the spec."""
    examples = schema["components"]["schemas"]["ArbOpenRequest"].get("examples", [])
    assert examples, "ArbOpenRequest has no examples"
    # default example: legs omitted (HL cash-and-carry default), notional sizing.
    default_ex = examples[0]
    assert "legs" not in default_ex
    assert default_ex["asset"] in ("BTC", "ETH", "SOL")
    assert default_ex["size_mode"] == "notional"
    # the explicit Bybit combo example is also present.
    assert any("legs" in ex and ex["legs"][0]["exchange"] == "bybit" for ex in examples)


# --- request-model validation rules (size_mode / delta-neutral shape) -------

def test_size_mode_notional_requires_positive_notional():
    with pytest.raises(ValidationError):
        ArbOpenRequest(idempotency_key="k", asset="BTC", size_mode="notional")
    with pytest.raises(ValidationError):
        ArbOpenRequest(idempotency_key="k", asset="BTC", size_mode="notional", notional=0)


def test_size_mode_min_makes_notional_optional():
    req = ArbOpenRequest(idempotency_key="k", asset="BTC", size_mode="min")
    assert req.notional is None and req.size_mode == "min"


def test_legs_must_be_delta_neutral_pair():
    # two buys -> rejected
    with pytest.raises(ValidationError):
        ArbOpenRequest(
            idempotency_key="k", asset="BTC", notional=100.0,
            legs=[LegSpec(exchange="bybit", product="spot", side="buy"),
                  LegSpec(exchange="bybit", product="perp", side="buy")],
        )
    # unequal explicit targets -> rejected
    with pytest.raises(ValidationError):
        ArbOpenRequest(
            idempotency_key="k", asset="BTC", notional=100.0,
            legs=[LegSpec(exchange="bybit", product="spot", side="buy", target_qty=1.0),
                  LegSpec(exchange="bybit", product="perp", side="sell", target_qty=2.0)],
        )
    # wrong leg count -> rejected
    with pytest.raises(ValidationError):
        ArbOpenRequest(
            idempotency_key="k", asset="BTC", notional=100.0,
            legs=[LegSpec(exchange="bybit", product="spot", side="buy")],
        )


# --- auth precedence + route behaviour (TestClient; covers the stub bodies) -

@pytest.fixture
def client_secret_unset(monkeypatch):
    """funding_arb_secret == "" -> every arb route 503s."""
    monkeypatch.setenv("FUNDING_ARB_SECRET", "")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def client_secret_set(monkeypatch):
    monkeypatch.setenv("FUNDING_ARB_SECRET", "s3cret")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_secret_unset_returns_503_even_with_header(client_secret_unset):
    # 503 regardless of header presence (an unconfigured arb API must not imply it works).
    r = client_secret_unset.post("/funding-arb/open",
                                 json={"idempotency_key": "k", "asset": "BTC", "notional": 100})
    assert r.status_code == 503
    r2 = client_secret_unset.post("/funding-arb/open",
                                  headers={"X-Arb-Secret": "anything"},
                                  json={"idempotency_key": "k", "asset": "BTC", "notional": 100})
    assert r2.status_code == 503
    assert client_secret_unset.get("/funding-arb/positions").status_code == 503
    assert client_secret_unset.get("/funding-arb").status_code == 503


def test_missing_or_bad_secret_returns_401(client_secret_set):
    assert client_secret_set.post(
        "/funding-arb/open",
        json={"idempotency_key": "k", "asset": "BTC", "notional": 100},
    ).status_code == 401
    assert client_secret_set.post(
        "/funding-arb/open", headers={"X-Arb-Secret": "wrong"},
        json={"idempotency_key": "k", "asset": "BTC", "notional": 100},
    ).status_code == 401


def test_correct_secret_open_close_and_status(client_secret_set, arb_registry):
    h = {"X-Arb-Secret": "s3cret"}
    # default combo (legs omitted) -> accepted with the HL cash-and-carry pair
    r = client_secret_set.post(
        "/funding-arb/open", headers=h,
        json={"idempotency_key": "k1", "asset": "BTC", "notional": 1000},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert {leg["exchange"] for leg in body["legs"]} == {"hyperliquid"}
    assert sorted(leg["side"] for leg in body["legs"]) == ["buy", "sell"]

    # explicit Bybit combo
    r2 = client_secret_set.post(
        "/funding-arb/open", headers=h,
        json={"idempotency_key": "k2", "asset": "ETH", "notional": 500,
              "legs": [{"exchange": "bybit", "product": "spot", "side": "buy"},
                       {"exchange": "bybit", "product": "perp", "side": "sell"}]},
    )
    assert r2.status_code == 200
    assert {leg["exchange"] for leg in r2.json()["legs"]} == {"bybit"}

    # size_mode=min makes notional optional
    r3 = client_secret_set.post(
        "/funding-arb/open", headers=h,
        json={"idempotency_key": "k3", "asset": "SOL", "size_mode": "min"},
    )
    assert r3.status_code == 200

    # close
    rc = client_secret_set.post("/funding-arb/close", headers=h, json={"arb_id": 1})
    assert rc.status_code == 200 and rc.json()["status"] == "closing"

    # status list + single
    assert client_secret_set.get("/funding-arb/positions", headers=h).status_code == 200
    one = client_secret_set.get("/funding-arb/positions/1", headers=h)
    assert one.status_code == 200 and one.json()["arb_id"] == 1
    assert one.headers["cache-control"] == "no-store"

    # HTML report page
    page = client_secret_set.get("/funding-arb", headers=h)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]


def test_hl_spot_leg_accepted_over_http(client_secret_set, arb_registry):
    """The API must NOT 422 a {hyperliquid, spot} leg (the key design change)."""
    r = client_secret_set.post(
        "/funding-arb/open", headers={"X-Arb-Secret": "s3cret"},
        json={"idempotency_key": "hlspot", "asset": "BTC", "notional": 100,
              "legs": [{"exchange": "hyperliquid", "product": "spot", "side": "buy"},
                       {"exchange": "hyperliquid", "product": "perp", "side": "sell"}]},
    )
    assert r.status_code == 200, r.text


# --- fresh-import staleness: committed file == freshly generated -------------

def test_committed_funding_arb_schema_is_fresh():
    """Regenerate the contract-grade schema in a FRESH process and assert it
    equals the committed docs/openapi-funding-arb.json byte-for-byte. Catches a
    schema edit that wasn't re-dumped (`make openapi`)."""
    assert FUNDING_ARB_JSON.exists(), "run `make openapi` and commit the artifact"

    # Regenerate to a temp dir in a subprocess (clean import state), then read the
    # contract-grade slice the script produced. We compute it the same way the
    # script does, but in isolation, by importing the script's slicer freshly.
    code = (
        "import json, sys;"
        "sys.path.insert(0, r'%s');"
        "import os;"
        "os.environ['DRY_RUN']='1';"
        "os.environ.setdefault('WEBHOOK_SECRET','openapi-dump-secret');"
        "os.environ.setdefault('FUNDING_ARB_SECRET','openapi-dump-arb-secret');"
        "os.environ.setdefault('DATABASE_URL','sqlite:///./data/openapi-dump.db');"
        "import scripts.dump_openapi as d;"
        "full=d._full_schema();"
        "arb=d._funding_arb_slice(full);"
        "sys.stdout.write(json.dumps(arb, indent=2, sort_keys=True) + chr(10))"
    ) % str(REPO_ROOT)
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert out.returncode == 0, f"regen failed: {out.stderr}"
    regenerated = out.stdout
    committed = FUNDING_ARB_JSON.read_text(encoding="utf-8")
    assert regenerated == committed, (
        "docs/openapi-funding-arb.json is stale — run `make openapi` and commit."
    )
    # sanity: it parses and carries the funding-arb paths
    parsed = json.loads(committed)
    assert "/funding-arb/open" in parsed["paths"]
