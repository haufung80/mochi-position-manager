"""`/funding-arb/*` — the funding-arbitrage execution API.

The signal app (`mochi-funding-signal`) POSTs "open this arb / close arb N" and
polls the status endpoints; this app fires both legs (fire-and-forget via
``BackgroundTasks``), drives each leg's ``ArbOrder`` through the retry ladder,
tracks the pair as one ``ArbPosition``, and reports funding-minus-fees PnL.

Auth precedence (explicit, implemented in `require_arb_secret`):
  1. ``funding_arb_secret == ""``        → **503** (an unconfigured arb API must
                                            never imply it works), regardless of
                                            the header.
  2. header missing or ``!= secret``     → **401**.
  3. otherwise                           → proceed.

The secret is an ``X-Arb-Secret`` API-key header (``APIKeyHeader``) so it surfaces
in the OpenAPI ``securitySchemes`` as ``ArbSecret`` and is attached to the write
routes. The signal app is our own code and can send headers (unlike TradingView).

Open-time **symbol exclusivity** (plan §1): the open endpoint REJECTS (409) a new
arb whose leg ``(exchange, account, symbol)`` is already held by a non-closed
``ArbPosition``. This keeps account-wide funding attribution unambiguous (HL
``get_funding`` is account-wide; two concurrent BTC arbs on one HL arb account
would otherwise double-count the same settlement).
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from sqlalchemy.exc import IntegrityError

from .. import arb_executor
from ..config import Settings, get_settings
from ..db import session_scope
from ..exchanges.registry import get_registry
from ..exchanges.symbols import spot_symbol_for, symbol_for
from ..models import ArbLeg, ArbOrder, ArbPosition
from ..schemas_arb import (
    ArbCloseRequest,
    ArbCloseResponse,
    ArbLegView,
    ArbOpenRequest,
    ArbOpenResponse,
    ArbPnL,
    ArbPositionView,
    LegSpec,
    SizedLeg,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/funding-arb", tags=["funding-arb"])

# auto_error=False so we control the precedence (503-before-401) ourselves rather
# than letting APIKeyHeader 403 on a missing header. The name drives the OpenAPI
# security scheme id below (security_scheme.scheme_name == "ArbSecret").
arb_secret_header = APIKeyHeader(name="X-Arb-Secret", auto_error=False, scheme_name="ArbSecret")

# Statuses that still "hold" a symbol for exclusivity (anything not terminal-closed).
_NON_CLOSED_STATUSES = ("opening", "open", "closing", "error")

# Documented write-route error responses (surface in the OpenAPI `responses{}`).
_OPEN_RESPONSES = {
    401: {"description": "Missing or incorrect X-Arb-Secret."},
    409: {"description": "A leg symbol is already held by a non-closed arb."},
    503: {"description": "Funding-arb API not configured (FUNDING_ARB_SECRET unset)."},
}
_CLOSE_RESPONSES = {
    401: {"description": "Missing or incorrect X-Arb-Secret."},
    404: {"description": "No arb with that id."},
    409: {"description": "Arb already closed."},
    503: {"description": "Funding-arb API not configured (FUNDING_ARB_SECRET unset)."},
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


# --- Combo resolution -------------------------------------------------------

def _default_combo() -> list[LegSpec]:
    """The DEFAULT combo when ``legs`` is omitted: single-venue Hyperliquid
    cash-and-carry — long HL spot + short HL perp on the same dedicated HL ``arb``
    account, perp at 1x."""
    return [
        LegSpec(exchange="hyperliquid", account="arb", product="spot", side="buy"),
        LegSpec(exchange="hyperliquid", account="arb", product="perp", side="sell"),
    ]


def _leg_symbol(spec: LegSpec, asset: str) -> str:
    """Resolve a leg's exchange-native symbol: spot legs use the spot mapping
    (Bybit ``BTCUSDT`` / HL ``UBTC/USDC``), perp legs use the perp mapping."""
    if spec.product == "spot":
        return spot_symbol_for(spec.exchange, asset)
    return symbol_for(spec.exchange, asset)


def _resolve_specs(req: ArbOpenRequest) -> list[LegSpec]:
    return req.legs if req.legs is not None else _default_combo()


# --- View serialization -----------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _leg_view(leg: ArbLeg) -> ArbLegView:
    return ArbLegView(
        exchange=leg.exchange, account=leg.account, product=leg.product,
        symbol=leg.symbol, side=leg.side, target_qty=leg.target_qty,
        filled_qty=leg.filled_qty, avg_fill=leg.avg_fill,
        funding=leg.funding, status=leg.status,
    )


def _position_view(arb: ArbPosition, legs: list[ArbLeg]) -> ArbPositionView:
    """Build the status payload. PnL funding reads off the stored per-leg funding
    (0 until A.6 attribution runs — documented as fine in the contract)."""
    long_filled = sum(lg.filled_qty for lg in legs if lg.side == "buy")
    short_filled = sum(lg.filled_qty for lg in legs if lg.side == "sell")
    skew = long_filled - short_filled
    all_success = bool(legs) and all(lg.status == "success" for lg in legs)
    neutral = all_success and abs(skew) <= 1e-9

    funding_by_leg = {
        f"{lg.exchange}:{lg.account}:{lg.symbol}": lg.funding for lg in legs
    }
    funding_total = sum(lg.funding for lg in legs)
    commission_total = sum(lg.commission for lg in legs)
    pnl = ArbPnL(
        funding_total=funding_total,
        funding_by_leg=funding_by_leg,
        commission_total=commission_total,
        spot_unrealized=0.0,
        perp_unrealized=0.0,
        directional_net=0.0,
        net=funding_total - commission_total,
    )
    return ArbPositionView(
        arb_id=arb.id, asset=arb.asset, status=arb.status,
        neutral=neutral, neutrality_skew=skew,
        legs=[_leg_view(lg) for lg in legs], pnl=pnl,
        opened_at=_iso(arb.opened_at), closed_at=_iso(arb.closed_at),
        error_message=arb.error_message or None,
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
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_arb_secret),
) -> ArbOpenResponse:
    """Authorise → resolve combo (explicit ``legs`` or the default HL combo) →
    reject (409) if any leg's ``(exchange, account, symbol)`` is already held by a
    non-closed arb → size the pair → persist ``ArbPosition`` + ``ArbLeg``s in ONE
    short txn (dedup via ``IntegrityError`` on ``idempotency_key`` → ``duplicate``)
    → return ``accepted`` + sized legs → schedule ``_run_open``.
    """
    specs = _resolve_specs(req)
    # Resolve each leg's symbol once (raises ValueError -> 422 on a bad asset).
    try:
        symbols = [_leg_symbol(s, req.asset) for s in specs]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Eagerly resolve every adapter so a mis-configured (exchange, account) — e.g.
    # the HL arb book sharing the directional address — fails the OPEN BEFORE any
    # order reaches an exchange (the registry guard raises here).
    try:
        for spec in specs:
            get_registry().get(spec.exchange, spec.account)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=f"arb account not available: {e}")

    arb_id: int
    sized_legs: list[SizedLeg]
    with session_scope() as db:
        # Idempotency FIRST: a repeat of the SAME key returns 'duplicate' (even if
        # its own legs would otherwise trip symbol-exclusivity below). This mirrors
        # the webhook dedup and makes a retried open safe.
        dup = (
            db.query(ArbPosition)
            .filter(ArbPosition.idempotency_key == req.idempotency_key)
            .one_or_none()
        )
        if dup is not None:
            return ArbOpenResponse(
                status="duplicate", arb_id=dup.id,
                idempotency_key=req.idempotency_key,
                legs=_sized_from_legs(
                    db.query(ArbLeg).filter(ArbLeg.arb_id == dup.id).all()
                ),
            )

        # Symbol exclusivity: refuse if any leg's (exchange, account, symbol) is
        # already held by a non-closed arb (plan §1).
        held = (
            db.query(ArbLeg)
            .join(ArbPosition, ArbPosition.id == ArbLeg.arb_id)
            .filter(ArbPosition.status.in_(_NON_CLOSED_STATUSES))
            .all()
        )
        held_keys = {(lg.exchange, lg.account, lg.symbol) for lg in held}
        clash = next(
            ((s.exchange, s.account, sym)
             for s, sym in zip(specs, symbols)
             if (s.exchange, s.account, sym) in held_keys),
            None,
        )
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"leg {clash[0]}:{clash[1]}:{clash[2]} already held by a "
                       "non-closed arb (symbol exclusivity)",
            )

        # Persist the position; the UNIQUE(idempotency_key) is the dedup gate.
        arb = ArbPosition(
            idempotency_key=req.idempotency_key,
            asset=req.asset,
            strategy_tag=req.strategy_tag,
            notional_target=req.notional,
            status="opening",
        )
        db.add(arb)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(ArbPosition)
                .filter(ArbPosition.idempotency_key == req.idempotency_key)
                .one()
            )
            return ArbOpenResponse(
                status="duplicate", arb_id=existing.id,
                idempotency_key=req.idempotency_key,
                legs=_sized_from_legs(
                    db.query(ArbLeg).filter(ArbLeg.arb_id == existing.id).all()
                ),
            )

        legs = [
            ArbLeg(arb_id=arb.id, exchange=s.exchange, account=s.account,
                   product=s.product, symbol=sym, side=s.side,
                   target_qty=s.target_qty or 0.0, status="pending")
            for s, sym in zip(specs, symbols)
        ]
        for leg in legs:
            db.add(leg)
        db.flush()

        # Size the pair (equal base qty on both grids). Reject -> mark error +
        # 422 (the request can't form a delta-neutral order at these prices).
        try:
            qty = arb_executor.size_pair(
                legs, size_mode=req.size_mode, notional=req.notional)
        except arb_executor.SizingError as e:
            arb.status = "error"
            arb.error_message = f"sizing failed: {e}"
            for leg in legs:
                leg.status = "error"
                leg.error_message = str(e)
            db.flush()
            raise HTTPException(status_code=422, detail=f"cannot size pair: {e}")

        for leg in legs:
            leg.target_qty = qty
        db.flush()

        arb_id = arb.id
        sized_legs = _sized_from_legs(legs)

    background_tasks.add_task(arb_executor._run_open, arb_id)
    return ArbOpenResponse(
        status="accepted", arb_id=arb_id,
        idempotency_key=req.idempotency_key, legs=sized_legs,
    )


def _sized_from_legs(legs: list[ArbLeg]) -> list[SizedLeg]:
    return [
        SizedLeg(exchange=lg.exchange, account=lg.account, product=lg.product,
                 symbol=lg.symbol, side=lg.side, target_qty=lg.target_qty)
        for lg in legs
    ]


@router.post(
    "/close",
    response_model=ArbCloseResponse,
    responses=_CLOSE_RESPONSES,
    summary="Close an open funding-arb position",
)
def close_arb(
    req: ArbCloseRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_arb_secret),
) -> ArbCloseResponse:
    """Set ``status=closing`` and schedule ``_run_close``. **404** unknown id;
    **409** already closed (or closing)."""
    with session_scope() as db:
        arb = db.get(ArbPosition, req.arb_id)
        if arb is None:
            raise HTTPException(status_code=404, detail="no arb with that id")
        if arb.status in ("closed", "closing"):
            raise HTTPException(status_code=409, detail=f"arb already {arb.status}")
        arb.status = "closing"

    background_tasks.add_task(arb_executor._run_close, req.arb_id)
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
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as db:
        arbs = db.query(ArbPosition).order_by(ArbPosition.id.desc()).all()
        views = []
        for arb in arbs:
            legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb.id).all()
            views.append(_position_view(arb, legs))
        return views


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
    response.headers["Cache-Control"] = "no-store"
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        if arb is None:
            raise HTTPException(status_code=404, detail="no arb with that id")
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        return _position_view(arb, legs)


@router.get(
    "",
    response_class=HTMLResponse,
    summary="Funding-arb reporting page (HTML)",
)
def arb_report_page(_auth: None = Depends(require_arb_secret)) -> HTMLResponse:
    """Minimal reporting stub. The real dark-theme report (per-arb funding −
    fees, per-leg breakdown, neutrality) lands in A.6."""
    return HTMLResponse(
        "<!doctype html><html><head><title>Funding Arb</title></head>"
        "<body><h1>Funding Arbitrage</h1>"
        "<p>Reporting page (full report lands in A.6).</p></body></html>"
    )
