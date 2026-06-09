"""Funding-arb leg execution + pair sizing.

A SIBLING to ``executor.py`` (which is hard-wired to ``Alert`` + ``VenueRoute``).
This module fires the two legs of a delta-neutral arb pair, drives each leg's
``ArbOrder`` through the SAME ``retrying -> dead`` ladder as the directional
executor, and records the fill onto the ``ArbLeg`` row.

**Isolation is structural and load-bearing.** This module REUSES only low-level
pieces from ``executor.py``:

  * ``OrderResult`` (the normalized adapter return),
  * ``_next_retry_delay`` (the backoff schedule), and
  * the ``retrying -> dead`` state ladder (re-implemented as a thin query here).

It NEVER calls ``_apply_fill_to_position``, NEVER takes ``_LEDGER_LOCK``, and
NEVER writes ``Position`` / ``StrategyPosition`` / ``Order``. It writes ``ArbLeg``
and ``ArbOrder`` only. An arb fill therefore can't reach any directional ledger,
query, or dashboard panel — the arb book is invisible to the directional side by
construction (separate tables + this writer boundary).

Two-leg open flow (``_run_open``):
  1. Fire the THINNER-liquidity leg first to minimize the naked window (for the
     default single-venue HL cash-and-carry: SPOT before PERP — HL Unit spot is
     thinner than the HL perp; documented in ``_open_order`` below).
  2. HEDGE THE ACTUAL FILL: after leg-1 fills ``f1`` (net of base fee for a Bybit
     spot buy; gross base for HL spot whose fee is quote-denominated), re-derive
     leg-2's target from ``f1`` snapped to leg-2's own grid (a true delta hedge of
     what filled, not the original target). If ``f1`` can't be hedged within one
     step at leg-2's venue, the arb is marked ``error`` + ``neutral=false`` — no
     silent residual.
  3. ONE ``session_scope()`` per leg (like ``_run_fan_out``) so one leg's failure
     can't roll back the other's recorded fill; then ``_finalize_open_status``.

``_run_open`` / ``_run_close`` are PLAIN SYNC functions (run in the
``BackgroundTasks`` threadpool, exactly like ``_run_fan_out``) so they are
unit-tested directly with no async machinery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .exchanges.registry import get_registry
from .executor import _next_retry_delay   # REUSED: backoff schedule only
from .models import ArbLeg, ArbOrder, ArbPosition
from .notifier import get_notifier
from .portfolio import compute_managed_qty
from .schemas import OrderResult

log = logging.getLogger(__name__)

# Below this |base qty| a leg is treated as flat / a hedge mismatch is dust.
_QTY_EPS: float = 1e-12

# Headroom over a venue's MINIMUM order value when sizing size_mode="min" orders, so a
# coarse-step / low-price asset (e.g. PURR ~ $0.088, whole-unit steps) clears the floor
# with margin instead of landing exactly on it and being rejected when the price ticks
# down between sizing and fill (or vs the venue's own valuation price). "min" is a tiny
# paper order either way, so over-sizing it slightly is harmless.
_MIN_NOTIONAL_BUFFER: float = 1.2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Grid helpers (own home — never reuse the linear `_round_qty`, which is the
# wrong grid for spot, and keep the arithmetic exact via Decimal like
# `compute_managed_qty` does).
# ---------------------------------------------------------------------------

def _snap_down(qty: float, step: float) -> float:
    """Largest multiple of `step` <= `qty` (exact, Decimal-floored)."""
    if qty <= 0 or step <= 0:
        return 0.0
    q = Decimal(str(qty))
    s = Decimal(str(step))
    units = (q / s).to_integral_value(rounding=ROUND_DOWN)
    return float(units * s)


def _min_qty_for_notional(min_notional: float, price: float, step: float) -> float:
    """Smallest multiple of `step` whose notional (qty*price) >= `min_notional`
    (rounds UP). 0.0 if any input is non-positive."""
    if min_notional <= 0 or price <= 0 or step <= 0:
        return 0.0
    need = Decimal(str(min_notional)) / Decimal(str(price))
    s = Decimal(str(step))
    units = (need / s).to_integral_value(rounding=ROUND_UP)
    return float(units * s)


def _leg_step(ex, leg: ArbLeg) -> float:
    """The base-asset grid for one leg, on ITS OWN product surface."""
    if leg.product == "spot":
        return ex.get_spot_step_size(leg.symbol)
    return ex.get_step_size(leg.symbol)


def _leg_min_notional(ex, leg: ArbLeg) -> float:
    """The quote-side minimum order value for one leg, on its own surface."""
    if leg.product == "spot":
        return ex.get_spot_min_notional(leg.symbol)
    return ex.get_min_notional(leg.symbol)


def _leg_ref_price(ex, leg: ArbLeg) -> float:
    """Reference price for one leg from its own venue (mark on Bybit, mid on HL)."""
    return ex.get_price(leg.symbol)


# ---------------------------------------------------------------------------
# Position-level Telegram alerts (best-effort: NEVER raise into the executor's
# session_scope — a flaky notifier must not roll back a recorded fill/close).
# These wrap `app/notifier.py`'s helpers; the notifier already swallows network
# failures, so we only guard against a missing/odd notifier object here.
# ---------------------------------------------------------------------------

def _neutrality_skew(legs: list[ArbLeg]) -> float | None:
    """Signed base-qty imbalance of a 2-leg pair = |long filled| − |short filled|
    (0.0 == delta-neutral). Returns None when it can't be computed (not a clean
    2-leg pair), so the alert just omits the skew line rather than lying."""
    if len(legs) != 2:
        return None
    signed = 0.0
    for lg in legs:
        signed += lg.filled_qty if lg.side == "buy" else -lg.filled_qty
    return signed


def _realized_net(legs: list[ArbLeg]) -> float | None:
    """Readily-available realized net for a close alert = Σ leg funding − Σ leg
    commissions (both already recorded on the legs). None when no leg carries
    either figure (nothing meaningful to show -> 'just closed')."""
    if not any((lg.funding or lg.commission) for lg in legs):
        return None
    funding = sum(lg.funding or 0.0 for lg in legs)
    commission = sum(lg.commission or 0.0 for lg in legs)
    return funding - commission


def _arb_opened_alert(arb_id: int, asset: str, qty: float, legs: str) -> None:
    try:
        get_notifier().arb_opened(arb_id, asset, qty, legs)
    except Exception as e:  # best-effort: never break the executor
        log.warning("arb_executor: arb_opened alert failed: %s", e)


def _arb_error_alert(arb_id: int, asset: str, reason: str,
                     skew: float | None) -> None:
    try:
        get_notifier().arb_error(arb_id, asset, reason, skew)
    except Exception as e:
        log.warning("arb_executor: arb_error alert failed: %s", e)


def _arb_closed_alert(arb_id: int, asset: str, net: float | None) -> None:
    try:
        get_notifier().arb_closed(arb_id, asset, net)
    except Exception as e:
        log.warning("arb_executor: arb_closed alert failed: %s", e)


# ---------------------------------------------------------------------------
# Pair sizing
# ---------------------------------------------------------------------------

class SizingError(ValueError):
    """A pair can't be sized into a delta-neutral equal-base-qty order on both
    grids (notional too small for one venue's min, or below one grid step)."""


def size_pair(legs: list[ArbLeg], *, size_mode: str,
              notional: float | None) -> float:
    """Return the SINGLE base qty both legs will be ordered at (delta-neutral).

    **ref_price** is the PERP leg's mark/mid (``get_price`` on its own venue); the
    perp is the 1x short whose notional the signal app sized against, so it's the
    natural reference. (For a perp-perp combo, leg-1's perp price is used.) The
    spot leg then mirrors that same base quantity, snapped to the coarser grid.

    ``size_mode="notional"``: one target base qty via
    ``compute_managed_qty(notional, ref_price, coarser_step)``; clamp to the
    COARSER of the two legs' grids (``max(stepA, stepB)``) so BOTH legs can hold
    the SAME quantity; then re-check EACH leg's own-venue min-notional and REJECT
    (raise ``SizingError`` — never silently shrink one leg) if either fails.

    ``size_mode="min"`` (paper): ignore ``notional`` and pick the qty that clears
    BOTH legs' OWN exchange minimum order size:
    ``qty = snap_up(max over legs of min_notional_leg/ref_price, coarser_grid)``,
    then verify it clears each leg's min. Both legs equal.
    """
    reg = get_registry()
    adapters = [reg.get(leg.exchange, leg.account) for leg in legs]
    steps = [_leg_step(ex, leg) for ex, leg in zip(adapters, legs)]
    if any(s <= 0 for s in steps):
        raise SizingError("a leg reported a non-positive grid step")
    coarse = max(steps)

    # Reference price: prefer the perp leg's own venue; fall back to leg-1.
    perp_idx = next((i for i, leg in enumerate(legs) if leg.product == "perp"), 0)
    ref_price = _leg_ref_price(adapters[perp_idx], legs[perp_idx])
    if ref_price <= 0:
        raise SizingError(
            f"reference price unavailable for {legs[perp_idx].exchange} "
            f"{legs[perp_idx].symbol}"
        )

    if size_mode == "min":
        # qty must clear EACH leg's own min-notional at its own ref price, PADDED by
        # _MIN_NOTIONAL_BUFFER so a low-price / coarse-step asset clears the floor with
        # margin (PURR at ~$0.088 in whole units would otherwise size to exactly $10 and
        # be rejected on a downtick). The re-check below still uses the TRUE minimum.
        per_leg_min_qty = [
            _min_qty_for_notional(_leg_min_notional(ex, leg) * _MIN_NOTIONAL_BUFFER,
                                  _leg_ref_price(ex, leg) or ref_price, coarse)
            for ex, leg in zip(adapters, legs)
        ]
        qty = _snap_down(max(per_leg_min_qty + [coarse]), coarse)
        # max() above already rounds up via _min_qty_for_notional; snap to grid.
        qty = max(qty, coarse)
    else:  # "notional"
        if notional is None or notional <= 0:
            raise SizingError("notional must be > 0 when size_mode='notional'")
        qty = compute_managed_qty(notional, ref_price, coarse)
        if qty <= 0:
            raise SizingError(
                f"notional ${notional:g} is below one coarse step "
                f"({coarse:g}) at ref price {ref_price:g}"
            )

    # Re-check each leg's own-venue min-notional and REJECT on failure.
    for ex, leg in zip(adapters, legs):
        leg_price = _leg_ref_price(ex, leg) or ref_price
        leg_min = _leg_min_notional(ex, leg)
        if leg_min > 0 and qty * leg_price < leg_min - _QTY_EPS:
            raise SizingError(
                f"{leg.exchange} {leg.product} {leg.symbol}: qty {qty:g} @ "
                f"{leg_price:g} = ${qty * leg_price:g} is below the venue minimum "
                f"${leg_min:g} (size_mode={size_mode})"
            )
    return qty


# ---------------------------------------------------------------------------
# Single-leg execution (shared by open + retry)
# ---------------------------------------------------------------------------

def _place_leg_order(ex, leg: ArbLeg) -> OrderResult:
    """Route to the correct product surface. Perp legs go at 1x leverage
    (``market_order(..., leverage=1.0)``, the LOCKED perp leverage); spot legs go
    to ``spot_market_order``."""
    if leg.product == "spot":
        return ex.spot_market_order(leg.symbol, leg.side, leg.target_qty)  # type: ignore[arg-type]
    return ex.market_order(leg.symbol, leg.side, leg.target_qty, leverage=1.0)  # type: ignore[arg-type]


def _arb_order_for(db: Session, leg: ArbLeg,
                   existing_order: ArbOrder | None) -> ArbOrder:
    """Create (or reuse) the leg's ArbOrder. side/qty are FROZEN at fire time;
    account/product are persisted so a retry re-resolves the EXACT adapter and
    BTCUSDT spot vs linear never collide."""
    if existing_order is not None:
        return existing_order
    order = ArbOrder(
        arb_leg_id=leg.id,
        exchange=leg.exchange,
        account=leg.account,
        product=leg.product,
        symbol=leg.symbol,
        side=leg.side,
        qty_base=leg.target_qty,
        status="pending",
        attempts=0,
    )
    db.add(order)
    db.flush()
    return order


def _on_leg_success(leg: ArbLeg, order: ArbOrder, result: OrderResult) -> None:
    """Record the NET fill on both the ArbOrder and the ArbLeg.

    ``filled_qty_base`` is already the actually-held base coming out of the
    adapter (Bybit spot buy = net of the base-coin fee; HL spot = gross because
    its fee is quote-denominated; perp = filled contracts). We store that as the
    hedgeable quantity neutrality + close are measured against."""
    filled = result.filled_qty_base or order.qty_base
    order.status = "success"
    order.exchange_order_id = result.exchange_order_id
    order.qty_base = filled
    order.avg_fill = result.avg_price or None
    order.commission = result.commission or 0.0
    order.commission_asset = result.commission_asset or ""
    order.error_message = ""
    order.next_retry_at = None
    order.updated_at = _utcnow()

    leg.filled_qty = filled
    leg.avg_fill = result.avg_price or 0.0
    leg.commission = result.commission or 0.0
    leg.commission_asset = result.commission_asset or ""
    leg.status = "success"
    leg.error_message = ""
    leg.updated_at = _utcnow()


def _on_leg_failure(leg: ArbLeg, order: ArbOrder, result: OrderResult) -> None:
    """Walk the SAME retrying -> dead ladder as the directional executor (reusing
    its ``_next_retry_delay`` schedule). On the terminal attempt the leg is marked
    ``dead`` (an unrecoverable leg); the pair is finalized ``error`` upstream."""
    settings = get_settings()
    order.error_message = result.error_message
    leg.error_message = result.error_message
    order.updated_at = leg.updated_at = _utcnow()

    if order.attempts >= settings.retry_max_attempts:
        order.status = "dead"
        order.next_retry_at = None
        leg.status = "dead"
        get_notifier().order_dead(
            f"arb:{leg.exchange}:{leg.account}", leg.exchange, leg.symbol,
            leg.side, order.qty_base, order.attempts, result.error_message,
        )
        log.error("ArbLeg DEAD leg=%s ex=%s acct=%s sym=%s err=%s",
                  leg.id, leg.exchange, leg.account, leg.symbol, result.error_message)
        return

    order.status = "retrying"
    leg.status = "retrying"
    delay = _next_retry_delay(order.attempts)
    order.next_retry_at = _utcnow() + timedelta(seconds=delay)
    get_notifier().order_failed(
        f"arb:{leg.exchange}:{leg.account}", leg.exchange, leg.symbol,
        leg.side, order.qty_base, order.attempts, result.error_message,
    )
    log.warning("ArbLeg retrying in %ss leg=%s ex=%s err=%s",
                delay, leg.id, leg.exchange, result.error_message)


def execute_leg(db: Session, leg: ArbLeg,
                existing_order: ArbOrder | None = None) -> ArbOrder:
    """Place (or RETRY) one arb leg. Resolves the adapter from the leg's OWN
    ``(exchange, account)`` — an arb leg/retry can never fall back to the default
    account (it would net against the directional book); a mis-config raises.

    Creates/reuses an ``ArbOrder`` (side/qty frozen; account/product persisted),
    fires the correct product surface, records the net fill on success, or walks
    the ``retrying -> dead`` ladder on failure. Writes ``ArbOrder`` + ``ArbLeg``
    only — never any directional ledger, never ``_LEDGER_LOCK``.
    """
    ex = get_registry().get(leg.exchange, leg.account)  # fail loud on mis-config

    order = _arb_order_for(db, leg, existing_order)
    order.attempts += 1
    order.updated_at = _utcnow()
    db.flush()

    ref = _leg_ref_price(ex, leg)               # mid at order time, recorded for slippage
    result = _place_leg_order(ex, leg)
    if result.success:
        _on_leg_success(leg, order, result)
        if ref > 0:
            leg.ref_price = ref
    else:
        _on_leg_failure(leg, order, result)
    return order


# ---------------------------------------------------------------------------
# Open / close orchestration (plain sync — run in the BackgroundTasks threadpool)
# ---------------------------------------------------------------------------

def _open_order(legs: list[ArbLeg]) -> list[ArbLeg]:
    """Order the legs so the THINNER-liquidity leg fires FIRST (minimizing the
    naked window between leg-1 and its hedge).

    Rule: **spot before perp**. For the default single-venue HL cash-and-carry the
    HL Unit spot book is thinner than the HL perp, so the spot buy goes first and
    the perp short hedges the ACTUAL spot fill. For a perp-perp combo (no spot
    leg) the BUY leg is fired first by convention (stable, deterministic ordering).
    """
    return sorted(legs, key=lambda lg: (lg.product != "spot", lg.side != "buy"))


def _hedge_qty(first_fill: float, leg2: ArbLeg) -> float:
    """Re-derive leg-2's hedge target from leg-1's ACTUAL net fill, snapped DOWN
    to leg-2's OWN grid. Returns 0.0 if it can't be hedged within one step (the
    snapped qty is 0), which the caller treats as an un-hedgeable partial fill."""
    ex = get_registry().get(leg2.exchange, leg2.account)
    step = _leg_step(ex, leg2)
    if step <= 0:
        return 0.0
    return _snap_down(first_fill, step)


def _finalize_open_status(arb_id: int) -> None:
    """Set the ArbPosition to ``open`` (both legs filled + balanced within one
    leg-2 step) or ``error`` (any leg not ``success`` or an unhedged skew).
    Records ``opened_at`` on success and an ``error_message`` otherwise."""
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        if arb is None:
            log.error("arb_executor: arb %s vanished before finalize", arb_id)
            return
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        all_success = bool(legs) and all(lg.status == "success" for lg in legs)
        if all_success:
            arb.status = "open"
            arb.opened_at = arb.opened_at or _utcnow()
            arb.error_message = ""
            # Snapshot for the alert while still inside the session.
            asset = arb.asset
            qty = max((lg.filled_qty for lg in legs), default=0.0)
            legs_desc = ", ".join(
                f"{lg.exchange}:{lg.product} {lg.side} {lg.filled_qty:g}"
                for lg in legs
            )
            _arb_opened_alert(arb_id, asset, qty, legs_desc)
        else:
            arb.status = "error"
            bad = [lg for lg in legs if lg.status != "success"]
            arb.error_message = (
                "one or more legs did not fill: "
                + ", ".join(f"{lg.exchange}:{lg.product}:{lg.status}" for lg in bad)
            ) or "no legs"
            asset = arb.asset
            reason = arb.error_message
            skew = _neutrality_skew(legs)
            # Existing dead-letter alert stays (executor control flow unchanged);
            # ADD the position-level not-neutral / leg-risk alert.
            get_notifier().order_dead(
                f"arb:{arb_id}", asset, "", "", 0.0, 0, reason,
            )
            _arb_error_alert(arb_id, asset, reason, skew)
        arb.updated_at = _utcnow()


def _run_open(arb_id: int) -> None:
    """Open both legs of an arb pair. PLAIN SYNC — runs in the BackgroundTasks
    threadpool (like ``_run_fan_out``). ONE ``session_scope`` per leg so one
    leg's failure can't roll back the other's recorded fill.

    Fires the thinner leg first, then HEDGES THE ACTUAL FILL: leg-2's target is
    re-derived from leg-1's net fill snapped to leg-2's grid. If leg-1 didn't fill
    (failed/dead) or its fill can't be hedged within one step, the pair finalizes
    ``error`` (``neutral=false``) — no silent residual.
    """
    # Snapshot the leg ordering + identities up front (own txn, read-only).
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        if arb is None:
            log.error("arb_executor: _run_open arb %s missing", arb_id)
            return
        ordered = _open_order(db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all())
        leg_ids = [lg.id for lg in ordered]
    if len(leg_ids) != 2:
        log.error("arb_executor: arb %s expected 2 legs, got %s", arb_id, len(leg_ids))
        _finalize_open_status(arb_id)
        return

    leg1_id, leg2_id = leg_ids

    # --- Leg 1 (thinner): fire at its own target, in its OWN txn. ---
    first_fill = 0.0
    leg1_ok = False
    with session_scope() as db:
        leg1 = db.get(ArbLeg, leg1_id)
        execute_leg(db, leg1)
        leg1_ok = leg1.status == "success"
        first_fill = leg1.filled_qty

    # --- Leg 2 (hedge the ACTUAL fill): re-derive target from leg-1's fill. ---
    with session_scope() as db:
        leg2 = db.get(ArbLeg, leg2_id)
        if not leg1_ok:
            # Leg-1 never filled — do NOT place a naked hedge. Mark leg-2 error.
            leg2.status = "error"
            leg2.error_message = "leg-1 did not fill; hedge not placed"
            leg2.updated_at = _utcnow()
        else:
            hedge = _hedge_qty(first_fill, leg2)
            if hedge <= _QTY_EPS:
                leg2.status = "error"
                leg2.error_message = (
                    f"leg-1 fill {first_fill:g} cannot be hedged within one step "
                    f"at {leg2.exchange} {leg2.symbol} (would leave a naked residual)"
                )
                leg2.updated_at = _utcnow()
            else:
                leg2.target_qty = hedge
                execute_leg(db, leg2)

    _finalize_open_status(arb_id)


def _run_close(arb_id: int) -> None:
    """Close both legs of an arb pair. PLAIN SYNC (BackgroundTasks threadpool).

    Perp leg -> ``close_position(symbol)``: a WHOLE-coin close is safe because the
    arb book owns a DEDICATED account (it can't touch a directional position).
    Spot leg -> ``spot_market_order(sell, qty=filled)`` CLAMPED to the live
    ``get_spot_balance`` so the sell never exceeds what is actually held.

    ONE ``session_scope`` per leg (independent failure domains). On full success
    the pair is set ``closed``; otherwise ``error`` with the per-leg reason.
    """
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        if arb is None:
            log.error("arb_executor: _run_close arb %s missing", arb_id)
            return
        leg_ids = [lg.id for lg in
                   db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()]

    errors: list[str] = []
    for leg_id in leg_ids:
        try:
            with session_scope() as db:
                leg = db.get(ArbLeg, leg_id)
                _close_leg(db, leg, errors)
        except Exception as e:  # one leg's failure can't abort the other
            log.exception("arb_executor: close leg %s failed", leg_id)
            errors.append(f"leg {leg_id}: {type(e).__name__}: {e}")

    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        if arb is None:
            return
        if errors:
            arb.status = "error"
            arb.error_message = "; ".join(errors)
            asset = arb.asset
            reason = arb.error_message
            get_notifier().order_dead(
                f"arb:{arb_id}", asset, "", "", 0.0, 0, reason,
            )
            _arb_error_alert(arb_id, asset, reason, None)
        else:
            arb.status = "closed"
            arb.closed_at = _utcnow()
            arb.error_message = ""
            asset = arb.asset
            # Readily-available realized net = Σ leg funding − Σ leg commissions
            # (already recorded on the legs); None if no leg carries either.
            legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
            net = _realized_net(legs)
            _arb_closed_alert(arb_id, asset, net)
        arb.updated_at = _utcnow()


def _close_leg(db: Session, leg: ArbLeg, errors: list[str]) -> None:
    """Flatten ONE leg. Perp -> whole-coin ``close_position``; spot ->
    ``spot_market_order`` SELL of the held qty clamped to the live free balance."""
    ex = get_registry().get(leg.exchange, leg.account)  # fail loud on mis-config
    if leg.product == "perp":
        result = ex.close_position(leg.symbol)
    else:
        base_asset = _base_asset_of_leg(leg)
        free = ex.get_spot_balance(base_asset)
        # CLAMP to the live free balance (plan §4): never sell more than is
        # actually held. free == 0 -> nothing to flatten (already flat).
        qty = _snap_down(min(leg.filled_qty, free), _leg_step(ex, leg))
        if qty <= _QTY_EPS:
            leg.status = "closed"
            leg.updated_at = _utcnow()
            return
        result = ex.spot_market_order(leg.symbol, "sell", qty)

    if result.success:
        leg.status = "closed"
        leg.error_message = ""
    else:
        leg.status = "error"
        leg.error_message = result.error_message
        errors.append(f"{leg.exchange}:{leg.product}:{leg.symbol} "
                      f"close failed: {result.error_message}")
    leg.updated_at = _utcnow()


def _base_asset_of_leg(leg: ArbLeg) -> str:
    """Recover the base asset to query a free spot balance for closing.

    Bybit spot symbol is ``<BASE>USDT`` -> strip ``USDT``. HL Unit spot symbol is
    ``U<BASE>/USDC`` and ``get_spot_balance`` maps the canonical base to the Unit
    token itself, so pass the canonical base (strip the ``U`` prefix + quote)."""
    sym = leg.symbol.upper()
    if "/" in sym:                      # HL Unit spot: 'UBTC/USDC' -> 'BTC'
        token = sym.split("/", 1)[0]
        return token[1:] if token.startswith("U") else token
    if sym.endswith("USDT"):            # Bybit spot: 'BTCUSDT' -> 'BTC'
        return sym[:-4]
    if sym.endswith("USDC"):
        return sym[:-4]
    return sym
