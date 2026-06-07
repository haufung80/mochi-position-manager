"""`/funding-arb/*` — the funding-arbitrage execution API.

**Phase 0 (contract gate).** These handlers are thin TYPED STUBS: they enforce
the auth contract and return schema-valid canned data so `app.openapi()` emits a
contract-grade spec for review. There is deliberately **no** executor / model /
adapter / DB behaviour here yet — the real bodies land in a later phase.

Auth precedence (explicit, implemented in `require_arb_secret`):
  1. ``funding_arb_secret == ""``        → **503** (an unconfigured arb API must
                                            never imply it works), regardless of
                                            the header.
  2. header missing or ``!= secret``     → **401**.
  3. otherwise                           → proceed.

The secret is an ``X-Arb-Secret`` API-key header (``APIKeyHeader``) so it surfaces
in the OpenAPI ``securitySchemes`` as ``ArbSecret`` and is attached to the write
routes. The signal app is our own code and can send headers (unlike TradingView).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader

from ..config import Settings, get_settings
from ..schemas_arb import (
    ArbCloseRequest,
    ArbCloseResponse,
    ArbLegView,
    ArbOpenRequest,
    ArbOpenResponse,
    ArbPnL,
    ArbPositionView,
    SizedLeg,
)
from ..exchanges.symbols import symbol_for

log = logging.getLogger(__name__)

router = APIRouter(prefix="/funding-arb", tags=["funding-arb"])

# auto_error=False so we control the precedence (503-before-401) ourselves rather
# than letting APIKeyHeader 403 on a missing header. The name drives the OpenAPI
# security scheme id below (security_scheme.scheme_name == "ArbSecret").
arb_secret_header = APIKeyHeader(name="X-Arb-Secret", auto_error=False, scheme_name="ArbSecret")

# Documented write-route error responses (surface in the OpenAPI `responses{}`).
_OPEN_RESPONSES = {
    401: {"description": "Missing or incorrect X-Arb-Secret."},
    503: {"description": "Funding-arb API not configured (FUNDING_ARB_SECRET unset)."},
}
_CLOSE_RESPONSES = {
    **_OPEN_RESPONSES,
    404: {"description": "No arb with that id."},
    409: {"description": "Arb already closed."},
}
_GET_RESPONSES = {
    401: {"description": "Missing or incorrect X-Arb-Secret."},
    503: {"description": "Funding-arb API not configured (FUNDING_ARB_SECRET unset)."},
}


def require_arb_secret(
    x_arb_secret: str | None = Depends(arb_secret_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce the 503-before-401 auth precedence for every arb route."""
    if settings.funding_arb_secret == "":
        # Unconfigured: never imply the API works, even with a header present.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="funding-arb API not configured",
        )
    if x_arb_secret is None or x_arb_secret != settings.funding_arb_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad arb secret")


# --- Stub data (schema-valid canned responses) ------------------------------

def _stub_default_legs(asset: str) -> list[SizedLeg]:
    """The default single-venue Hyperliquid cash-and-carry pair, sized to a
    placeholder qty. Used only to make the Phase-0 stubs schema-valid."""
    sym = symbol_for("hyperliquid", asset)
    return [
        SizedLeg(exchange="hyperliquid", account="arb", product="spot",
                 symbol=sym, side="buy", target_qty=0.01),
        SizedLeg(exchange="hyperliquid", account="arb", product="perp",
                 symbol=sym, side="sell", target_qty=0.01),
    ]


def _stub_position_view(arb_id: int, asset: str = "BTC") -> ArbPositionView:
    sym = symbol_for("hyperliquid", asset)
    legs = [
        ArbLegView(exchange="hyperliquid", account="arb", product="spot",
                   symbol=sym, side="buy", target_qty=0.01, filled_qty=0.01,
                   avg_fill=0.0, funding=0.0, status="success"),
        ArbLegView(exchange="hyperliquid", account="arb", product="perp",
                   symbol=sym, side="sell", target_qty=0.01, filled_qty=0.01,
                   avg_fill=0.0, funding=0.0, status="success"),
    ]
    pnl = ArbPnL(
        funding_total=0.0,
        funding_by_leg={f"hyperliquid:arb:{sym}": 0.0},
        commission_total=0.0,
        spot_unrealized=0.0,
        perp_unrealized=0.0,
        directional_net=0.0,
        net=0.0,
    )
    return ArbPositionView(
        arb_id=arb_id, asset=asset, status="open", neutral=True,
        neutrality_skew=0.0, legs=legs, pnl=pnl,
        opened_at=None, closed_at=None, error_message=None,
    )


# --- Routes -----------------------------------------------------------------

@router.post(
    "/open",
    response_model=ArbOpenResponse,
    responses=_OPEN_RESPONSES,
    summary="Open a delta-neutral funding-arb position",
)
def open_arb(
    req: ArbOpenRequest,
    _auth: None = Depends(require_arb_secret),
) -> ArbOpenResponse:
    """STUB (Phase 0): validates the request + auth, returns schema-valid canned
    data. No legs are placed and nothing is persisted."""
    legs = (
        [SizedLeg(exchange=l.exchange, account=l.account, product=l.product,
                  symbol=symbol_for(l.exchange, req.asset), side=l.side,
                  target_qty=l.target_qty or 0.01)
         for l in req.legs]
        if req.legs is not None
        else _stub_default_legs(req.asset)
    )
    return ArbOpenResponse(
        status="accepted",
        arb_id=1,
        idempotency_key=req.idempotency_key,
        legs=legs,
    )


@router.post(
    "/close",
    response_model=ArbCloseResponse,
    responses=_CLOSE_RESPONSES,
    summary="Close an open funding-arb position",
)
def close_arb(
    req: ArbCloseRequest,
    _auth: None = Depends(require_arb_secret),
) -> ArbCloseResponse:
    """STUB (Phase 0): returns ``closing`` for any positive id. The real handler
    will 404 unknown ids and 409 already-closed arbs (documented in `responses`)."""
    return ArbCloseResponse(status="closing", arb_id=req.arb_id)


@router.get(
    "/positions",
    response_model=list[ArbPositionView],
    responses=_GET_RESPONSES,
    summary="List funding-arb positions",
)
def list_positions(
    response: Response,
    _auth: None = Depends(require_arb_secret),
) -> list[ArbPositionView]:
    """STUB (Phase 0): returns an empty list (no DB yet)."""
    response.headers["Cache-Control"] = "no-store"
    return []


@router.get(
    "/positions/{arb_id}",
    response_model=ArbPositionView,
    responses={**_GET_RESPONSES, 404: {"description": "No arb with that id."}},
    summary="Get one funding-arb position",
)
def get_position(
    arb_id: int,
    response: Response,
    _auth: None = Depends(require_arb_secret),
) -> ArbPositionView:
    """STUB (Phase 0): returns a canned open position so the schema is exercised."""
    response.headers["Cache-Control"] = "no-store"
    return _stub_position_view(arb_id)


@router.get(
    "",
    response_class=HTMLResponse,
    summary="Funding-arb reporting page (HTML)",
)
def arb_report_page(_auth: None = Depends(require_arb_secret)) -> HTMLResponse:
    """STUB (Phase 0): placeholder reporting page. The real dark-theme report
    lands with the executor/funding phases."""
    return HTMLResponse(
        "<!doctype html><html><head><title>Funding Arb</title></head>"
        "<body><h1>Funding Arbitrage</h1>"
        "<p>Reporting page placeholder (Phase 0 stub).</p></body></html>"
    )
