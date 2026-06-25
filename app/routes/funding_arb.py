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

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request,
                     Response, status)
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .. import arb_executor
from ..arb_pnl import LegPnLInput, compute_arb_pnl
from ..arb_funding import leg_funding
from ..config import Settings, get_settings
from ..db import session_scope
from ..risk import get_risk_settings
from ..exchanges.registry import get_registry
from ..exchanges.symbols import spot_symbol_for, symbol_for
from ..models import ArbEquitySnapshot, ArbFundingEvent, ArbLeg, ArbOrder, ArbPosition
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

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
# Reuse the dashboard's presentation filters (usd/qty/fee/when) so the arb report
# formats numbers identically to /performance — single source of truth, no drift.
from .dashboard import (  # noqa: E402
    _fmt_fee, _fmt_qty, _fmt_usd, _fmt_when,
    # Pure equity-curve render helpers (operate on passed-in data — they never read
    # the directional `equity_snapshots`, so reusing them keeps the isolation intact).
    _equity_series, _equity_chart_payload, _equity_metrics, _resolve_window,
    _EQUITY_WINDOWS, _EQUITY_DEFAULT_WINDOW)
templates.env.filters["usd"] = _fmt_usd
templates.env.filters["qty"] = _fmt_qty
templates.env.filters["fee"] = _fmt_fee
templates.env.filters["when"] = _fmt_when

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
    503: {"description": "Funding-arb API not configured (FUNDING_ARB_SECRET unset), "
                         "or the global kill-switch is ON."},
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


# Per-leg funding attribution now lives in app/arb_funding.py so the close-alert net
# (arb_executor) computes funding the SAME way as this report — they can't drift.
_leg_funding = leg_funding


def _perp_mark(leg: ArbLeg) -> float:
    """Live mark for a PERP leg from its OWN venue/account (display path — never
    raises; 0.0 on any failure so the PnL math contributes 0 unrealized)."""
    try:
        d = get_registry().get(leg.exchange, leg.account).get_position_detail(leg.symbol)
        return float(d.get("mark") or 0.0)
    except Exception:                       # noqa: BLE001 — display path, never raise
        return 0.0


def _spot_mark(leg: ArbLeg) -> float:
    """Live mark for a SPOT leg (its venue spot price). Never raises; 0.0 on
    failure. Falls back to the recorded ``avg_fill`` so a flat unrealized (not a
    bogus one) is shown when the live read is unavailable."""
    try:
        px = float(get_registry().get(leg.exchange, leg.account).get_price(leg.symbol) or 0.0)
        return px if px > 0 else float(leg.avg_fill or 0.0)
    except Exception:                       # noqa: BLE001 — display path, never raise
        return float(leg.avg_fill or 0.0)


def _leg_pnl_inputs(db, arb: ArbPosition, legs: list[ArbLeg]) -> list[LegPnLInput]:
    """Assemble each leg's already-fetched PnL inputs (funding + mark) so the pure
    ``compute_arb_pnl`` helper does the arithmetic with no exchange access."""
    inputs: list[LegPnLInput] = []
    for lg in legs:
        funding = _leg_funding(db, arb, lg)
        # A CLOSED leg is flat -> no unrealized MTM. Feed mark = avg_fill so
        # directional_unrealized is 0 (its directional P&L was BOOKED into
        # `realized_pnl` at close); a live mark on a gone position would drift forever.
        # Per-LEG (not per-arb) so a half-closed "error" arb marks its still-open leg
        # live while the flattened leg shows its realized. Open legs mark to the real
        # price so net carries their directional MTM.
        if lg.status == "closed":
            mark = lg.avg_fill
        else:
            mark = _perp_mark(lg) if lg.product == "perp" else _spot_mark(lg)
        inputs.append(LegPnLInput(
            exchange=lg.exchange, account=lg.account, product=lg.product,
            symbol=lg.symbol, side=lg.side, filled=lg.filled_qty,
            avg_fill=lg.avg_fill, mark=mark, funding=funding,
            commission=lg.commission,
        ))
    return inputs


def _leg_view(leg: ArbLeg, funding: float | None = None) -> ArbLegView:
    return ArbLegView(
        exchange=leg.exchange, account=leg.account, product=leg.product,
        symbol=leg.symbol, side=leg.side, target_qty=leg.target_qty,
        filled_qty=leg.filled_qty, avg_fill=leg.avg_fill,
        funding=leg.funding if funding is None else funding, status=leg.status,
    )


def _coarse_grid_step(legs: list[ArbLeg]) -> float:
    """Largest base grid step across a pair's legs — the floor on how tightly the two
    legs can be matched (you can't hedge finer than the coarser leg's grid).

    Prefers the step PERSISTED on each leg at open (``ArbLeg.grid_step``): that is the
    open-time grid the hedge was actually snapped to, it needs NO read-path exchange
    call, and it can't flip with a live adapter being down / re-tiered. Only legacy legs
    that predate that column (``grid_step == 0``) fall back to a best-effort live lookup
    (guarded — a flaky adapter must never break a reporting read); 0.0 if even that is
    unavailable, which makes the caller use a near-exact tolerance."""
    stored = max((lg.grid_step or 0.0) for lg in legs) if legs else 0.0
    if stored > 0:
        return stored
    reg = get_registry()                         # legacy legs only: live fallback
    step = 0.0
    for lg in legs:
        try:
            ex = reg.get(lg.exchange, lg.account)
            s = (ex.get_spot_step_size(lg.symbol) if lg.product == "spot"
                 else ex.get_step_size(lg.symbol))
            step = max(step, float(s or 0.0))
        except Exception:
            continue
    return step


def _pair_neutral(legs: list[ArbLeg], skew: float, all_success: bool) -> bool:
    """A pair is delta-neutral when both legs filled AND the leg imbalance is within
    the grid resolution (``|skew| <= coarse_step/2`` — the tightest a NEAREST-snapped
    hedge can achieve). Sub-grid dust between the spot fill and the coarser perp grid
    is NOT a real directional position, so it must not read as "skewed"; a genuine
    half-hedge (skew far above one step) still does. Falls back to a near-exact check
    when no grid is resolvable."""
    if not all_success:
        return False
    step = _coarse_grid_step(legs)
    tol = (step / 2.0 + step * 1e-9) if step > 0 else 1e-9
    return abs(skew) <= tol


def _position_view(arb: ArbPosition, legs: list[ArbLeg], db=None) -> ArbPositionView:
    """Build the status payload with REAL per-arb PnL (A.6).

    ``funding_total`` sums each perp leg's attributed ``ArbFundingEvent`` rows over
    the arb's window; spot legs are 0. ``commission_total`` = Σ leg commissions.
    ``spot_unrealized`` / ``perp_unrealized`` are cost-basis marks; ``net =
    funding_total − commission_total + directional_net + realized``. The math lives in
    the pure ``compute_arb_pnl`` helper (tested without live exchanges). When ``db`` is
    None (no session), funding/marks fall back to the stored per-leg values."""
    long_filled = sum(lg.filled_qty for lg in legs if lg.side == "buy")
    short_filled = sum(lg.filled_qty for lg in legs if lg.side == "sell")
    skew = long_filled - short_filled
    all_success = bool(legs) and all(lg.status == "success" for lg in legs)
    neutral = _pair_neutral(legs, skew, all_success)

    if db is not None:
        inputs = _leg_pnl_inputs(db, arb, legs)
        realized = sum(lg.realized_pnl or 0.0 for lg in legs)
        result = compute_arb_pnl(inputs, realized=realized)
        per_leg_funding = {
            (i.exchange, i.account, i.symbol): i.funding for i in inputs
        }
        pnl = ArbPnL(
            funding_total=result.funding_total,
            funding_by_leg=result.funding_by_leg,
            commission_total=result.commission_total,
            spot_unrealized=result.spot_unrealized,
            perp_unrealized=result.perp_unrealized,
            directional_net=result.directional_net,
            net=result.net,
        )
        leg_views = [
            _leg_view(lg, per_leg_funding.get((lg.exchange, lg.account, lg.symbol)))
            for lg in legs
        ]
    else:
        # No session (e.g. a hand-built view in a unit test): use stored funding.
        funding_by_leg = {
            f"{lg.exchange}:{lg.account}:{lg.symbol}": (lg.funding if lg.product == "perp" else 0.0)
            for lg in legs
        }
        funding_total = sum(v for v in funding_by_leg.values())
        commission_total = sum(lg.commission for lg in legs)
        realized = sum(lg.realized_pnl or 0.0 for lg in legs)
        pnl = ArbPnL(
            funding_total=funding_total, funding_by_leg=funding_by_leg,
            commission_total=commission_total, spot_unrealized=0.0,
            perp_unrealized=0.0, directional_net=0.0,
            net=funding_total - commission_total + realized,
        )
        leg_views = [_leg_view(lg) for lg in legs]

    return ArbPositionView(
        arb_id=arb.id, asset=arb.asset, status=arb.status,
        neutral=neutral, neutrality_skew=skew,
        legs=leg_views, pnl=pnl,
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

        # Global kill-switch (pre-trade risk gate): halt NEW opens. Checked AFTER
        # idempotency (a repeat key returned `duplicate` above — no NEW exposure) and
        # mirrors the directional gate in portfolio.decide; CLOSE is never gated, so a
        # halted book can always be de-risked.
        if get_risk_settings(db).kill_switch:
            raise HTTPException(
                status_code=503,
                detail="global kill-switch is ON — funding-arb opens halted",
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
            views.append(_position_view(arb, legs, db))
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
        return _position_view(arb, legs, db)


# --- Reporting page ---------------------------------------------------------

def _arb_performance(db) -> dict:
    """Roll up every ``ArbPosition`` into the reporting-page model: one row per arb
    with nested legs + per-leg PnL, plus portfolio headline totals.

    Headline = **funding harvested − fees** (Σ ``net`` across arbs). Each row's
    funding/marks come from the SAME real attribution as the status endpoint
    (``_leg_pnl_inputs`` → ``compute_arb_pnl``), so the page and the API agree."""
    rows: list[dict] = []
    tot_funding = tot_commission = tot_net = 0.0
    tot_spot_unreal = tot_perp_unreal = tot_realized = 0.0
    tot_basis = tot_slippage = 0.0
    tot_slippage_known = False
    open_count = 0

    arbs = db.query(ArbPosition).order_by(ArbPosition.id.desc()).all()
    for arb in arbs:
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb.id).all()
        inputs = _leg_pnl_inputs(db, arb, legs)
        # Realized directional P&L booked at close (0 on still-open legs); folded into
        # net alongside the open legs' unrealized MTM (never both for one leg).
        realized = sum(lg.realized_pnl or 0.0 for lg in legs)
        result = compute_arb_pnl(inputs, realized=realized)
        by_input = {(i.exchange, i.account, i.symbol): i for i in inputs}

        long_filled = sum(lg.filled_qty for lg in legs if lg.side == "buy")
        short_filled = sum(lg.filled_qty for lg in legs if lg.side == "sell")
        skew = long_filled - short_filled
        all_success = bool(legs) and all(lg.status == "success" for lg in legs)
        neutral = _pair_neutral(legs, skew, all_success)
        # Only an open-ish pair has a meaningful neutrality state; a closed/error arb
        # is flat (don't flag it as "skew").
        show_neutrality = arb.status in ("opening", "open", "closing")
        # Basis (entry spread captured) = Σ signed fill cash (sell +, buy −): for a
        # cash-and-carry, (perp_sell_avg − spot_buy_avg)·qty — the edge locked at entry.
        basis = sum((lg.avg_fill or 0.0) * (lg.filled_qty or 0.0)
                    * (1 if lg.side == "sell" else -1) for lg in legs)
        # Slippage cost vs the recorded mid at fill (buy above mid / sell below mid =
        # positive cost); only legs that captured a ref price count.
        slip_legs = [lg for lg in legs if (lg.ref_price or 0) > 0 and (lg.filled_qty or 0) > 0]
        slippage = sum(((lg.avg_fill - lg.ref_price) if lg.side == "buy"
                        else (lg.ref_price - lg.avg_fill)) * lg.filled_qty
                       for lg in slip_legs)
        slippage_known = bool(slip_legs)

        leg_rows = []
        for lg in legs:
            inp = by_input.get((lg.exchange, lg.account, lg.symbol))
            leg_unreal = inp.directional_unrealized if inp else 0.0
            leg_rows.append({
                "exchange": lg.exchange, "account": lg.account,
                "product": lg.product, "symbol": lg.symbol, "side": lg.side,
                "filled_qty": lg.filled_qty, "avg_fill": lg.avg_fill,
                "mark": inp.mark if inp else 0.0,
                # Funding is the point on the perp leg; a spot leg always shows 0.
                "funding": inp.funding if (inp and lg.product == "perp") else 0.0,
                "commission": lg.commission,
                "spot_unrealized": leg_unreal if lg.product == "spot" else 0.0,
                "perp_unrealized": leg_unreal if lg.product == "perp" else 0.0,
                "directional_net": leg_unreal,
                "realized_pnl": lg.realized_pnl or 0.0,
                "status": lg.status,
            })

        rows.append({
            "arb_id": arb.id, "asset": arb.asset, "status": arb.status,
            "strategy_tag": arb.strategy_tag, "neutral": neutral,
            "neutrality_skew": skew, "show_neutrality": show_neutrality,
            "opened_at": arb.opened_at, "closed_at": arb.closed_at,
            "error_message": arb.error_message or None,
            "funding_total": result.funding_total,
            "commission_total": result.commission_total,
            "basis": basis, "slippage": slippage, "slippage_known": slippage_known,
            "spot_unrealized": result.spot_unrealized,
            "perp_unrealized": result.perp_unrealized,
            "directional_net": result.directional_net,
            "realized": realized,
            "net": result.net,
            "legs": leg_rows,
        })
        tot_funding += result.funding_total
        tot_commission += result.commission_total
        tot_net += result.net
        tot_spot_unreal += result.spot_unrealized
        tot_perp_unreal += result.perp_unrealized
        tot_realized += realized
        tot_basis += basis
        tot_slippage += slippage
        tot_slippage_known = tot_slippage_known or slippage_known
        if arb.status in ("open", "opening", "closing"):
            open_count += 1

    totals = {
        "funding": tot_funding, "commission": tot_commission, "net": tot_net,
        "basis": tot_basis, "slippage": tot_slippage, "slippage_known": tot_slippage_known,
        "spot_unrealized": tot_spot_unreal, "perp_unrealized": tot_perp_unreal,
        "directional_net": tot_spot_unreal + tot_perp_unreal, "realized": tot_realized,
        "open_count": open_count, "total_count": len(arbs),
    }
    return {"arbs": rows, "totals": totals}


# --- arb equity curve: own snapshot table + cache, fully isolated from the
# --- directional /performance curve (which reads only `equity_snapshots`). ---
_ARB_EQ_CACHE: dict = {"at": 0.0, "snapshots": None, "report": None}
_ARB_EQ_CACHE_TTL = 30.0


def _arb_by_venue(report: dict) -> dict:
    """{exchange: net} = Σ(funding − commission + directional MTM + realized) per leg
    exchange, from an `_arb_performance` report — the SAME definition as the headline net
    (open legs carry MTM, closed legs carry realized), so the per-venue values sum to it.
    The curve's per-venue live tip; the snapshot writer stores the SAME map, so history
    and the live edge can't drift. Single-venue HL today → {"hyperliquid": net}."""
    by: dict[str, float] = {}
    for a in report["arbs"]:
        for lg in a["legs"]:
            by[lg["exchange"]] = (by.get(lg["exchange"], 0.0)
                                  + (lg["funding"] or 0.0) - (lg["commission"] or 0.0)
                                  + (lg["directional_net"] or 0.0)
                                  + (lg["realized_pnl"] or 0.0))
    return by


def _arb_capital_base(report: dict) -> float:
    """Capital denominator for the arb curve's return-% / APR. The configured
    `arb_capital_base` if set (>0, a pinned denominator); otherwise AUTO = the notional
    currently deployed across OPEN arbs — each arb's position notional ≈ a leg's
    filled_qty × avg_fill (both legs are ~equal, so take the max, not the sum). 0 when
    the book is flat AND unset → the curve falls back to $-only metrics."""
    cfg = get_settings().arb_capital_base
    if cfg and cfg > 0:
        return float(cfg)
    deployed = 0.0
    for a in report["arbs"]:
        if a["status"] in ("open", "opening", "closing") and a["legs"]:
            deployed += max((lg["filled_qty"] or 0.0) * (lg["avg_fill"] or 0.0)
                            for lg in a["legs"])
    return deployed


def _load_arb_snapshots(db) -> list[tuple]:
    """All `ArbEquitySnapshot` rows as (ts_utc, net, by_venue_dict), time-ordered."""
    out: list[tuple] = []
    for s in db.query(ArbEquitySnapshot).order_by(ArbEquitySnapshot.captured_at).all():
        ts = s.captured_at
        if ts is not None and ts.tzinfo is None:        # SQLite returns naive -> assume UTC
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            by = json.loads(s.by_venue or "{}")
        except (ValueError, TypeError):
            by = {}
        out.append((ts, float(s.net), by))
    return out


def _arb_equity_dataset(db, force: bool = False):
    """(arb_snapshots, arb_report) cached for _ARB_EQ_CACHE_TTL — so switching the
    curve's timeframe re-fetches neither the snapshot rows nor the live marks. A
    separate cache from the directional `_equity_dataset`."""
    now = time.time()
    if force or _ARB_EQ_CACHE["snapshots"] is None or now - _ARB_EQ_CACHE["at"] > _ARB_EQ_CACHE_TTL:
        _ARB_EQ_CACHE["snapshots"] = _load_arb_snapshots(db)
        _ARB_EQ_CACHE["report"] = _arb_performance(db)
        _ARB_EQ_CACHE["at"] = now
    return _ARB_EQ_CACHE["snapshots"], _ARB_EQ_CACHE["report"]


def _clear_arb_equity_cache() -> None:
    """Drop the cached arb dataset (snapshot write / forced refresh / test isolation)."""
    _ARB_EQ_CACHE.update(at=0.0, snapshots=None, report=None)


@router.get(
    "",
    response_class=HTMLResponse,
    summary="Funding-arb reporting page (HTML)",
)
def arb_report_page(request: Request,
                    equity_window: str = Query(_EQUITY_DEFAULT_WINDOW),
                    refresh: bool = Query(False)) -> HTMLResponse:
    """Dark-theme funding-arb report: one row per ``ArbPosition`` with nested legs,
    headline = funding harvested − fees, per-leg funding (spot 0) / spot+perp
    unrealized / directional-net (≈0 health check) / neutrality skew.

    **AUTH (deliberate, documented):** this is an HTML page reached by BROWSER
    NAVIGATION, which cannot attach a custom ``X-Arb-Secret`` header (a fetch can,
    a ``<a href>`` click can't). It is therefore gated EXACTLY like the existing
    ``/performance`` page — currently OPEN (no auth dependency) — and NOT behind
    ``require_arb_secret``. The JSON ``/funding-arb/{open,close,positions}`` API
    routes that the signal app calls programmatically stay behind ``X-Arb-Secret``
    as-is. (If ``/performance`` is later gated by ``WEBHOOK_SECRET``, gate this the
    same way for parity — they share the same browser-nav threat model.)
    """
    wsel, wdelta = _resolve_window(equity_window)
    with session_scope() as db:
        # Cached (snapshots + live report) so timeframe switches re-fetch nothing within
        # the TTL; ?refresh=true forces it. The equity curve plots arb NET over time —
        # per venue + aggregate, tipped to the live headline (same as /performance).
        snapshots, report = _arb_equity_dataset(db, force=refresh)
        series = _equity_series(snapshots, wdelta, report["totals"]["net"],
                                _arb_by_venue(report))
        capital = _arb_capital_base(report)              # config, else deployed notional
        equity = _equity_chart_payload(series, capital_base=capital)   # ECharts payload (app.js draws it)
        metrics = _equity_metrics(series.get("Total", []), capital_base=capital)
    resp = templates.TemplateResponse("funding_arb.html", {
        "request": request, "report": report, "equity": equity, "metrics": metrics,
        "equity_windows": [w for w, _ in _EQUITY_WINDOWS], "equity_window": wsel,
        "arb_capital_auto": not (get_settings().arb_capital_base > 0),
    })
    # Live trading data — never serve a stale arb report from a browser/proxy cache.
    resp.headers["Cache-Control"] = "no-store"
    return resp
