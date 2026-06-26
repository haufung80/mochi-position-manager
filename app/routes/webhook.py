"""POST /webhook/tradingview — the only inbound endpoint.

The handler must answer FAST: TradingView's webhook delivery times out after
a few seconds, so a slow response is reported as a failure even when the order
actually filled. We therefore do only the cheap, must-be-synchronous work in
the request path — parse, authorise, dedup-gate, and persist the alert (all
local, sub-millisecond) — and hand the slow exchange round-trips to a
background task. TradingView gets an immediate "accepted"; the orders fill a
beat later and show up in the logs / dashboard exactly as before.

Why background tasks (not inline): the exchange SDKs (pybit, hyperliquid) make
blocking HTTP calls. Run inline in this async handler they (a) make the
response too slow for TradingView and (b) freeze the event loop. FastAPI runs
*sync* background functions in a threadpool, which solves both.
"""
from __future__ import annotations
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from .. import portfolio
from ..config import Settings, get_settings
from ..db import session_scope
from ..dedup import idempotency_key
from ..exchanges.registry import get_registry
from ..executor import (execute_order, record_rejected_order, make_client_order_id,
                        book_limit_fill_delta, _utcnow)
from ..models import Alert, Order
from ..notifier import get_notifier
from ..portfolio import Decision
from ..schemas import TradingViewAlert, ORDER_STATE_UNKNOWN

log = logging.getLogger(__name__)
router = APIRouter()


async def _parse_body(request: Request) -> dict:
    """TradingView sometimes sets content-type to text/plain. Tolerate both."""
    if "application/json" in request.headers.get("content-type", ""):
        return await request.json()
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid json: {e}") from e


def _validate(body: dict) -> TradingViewAlert:
    try:
        return TradingViewAlert(**body)
    except Exception as e:
        log.warning("Rejected payload (schema): %s :: body=%s", e, body)
        raise HTTPException(status_code=422, detail=f"schema: {e}") from e


def _authorise(alert: TradingViewAlert, secret: str, source_ip: str) -> None:
    if alert.secret != secret:
        log.warning("Rejected payload: bad secret (ip=%s strat=%s)",
                    source_ip, alert.strategy_id)
        raise HTTPException(status_code=401, detail="bad secret")


def _persist_alert(db, alert: TradingViewAlert, body: dict,
                   key: str, source_ip: str) -> Alert:
    row = Alert(
        idempotency_key=key,
        strategy_id=alert.strategy_id,
        action=alert.action,
        raw_payload=json.dumps(body),
        signal_price=alert.price,
        source_ip=source_ip,
    )
    db.add(row)
    db.flush()  # raises IntegrityError on duplicate
    return row


def _run_fan_out(alert_id: int, route, quantity: float) -> None:
    """Runs AFTER the HTTP response is sent (FastAPI BackgroundTask → threadpool,
    so the blocking exchange SDK calls never touch the event loop).

    One independent transaction per venue, so a failure on one exchange can't
    roll back another's fill. For each venue the portfolio manager decides the
    order intent (managed sizing for sar=false; the alert's quantity for sar=true),
    then `execute_order` persists + drives the Order through success/retrying/dead.
    """
    for venue in route.enabled_venues():
        try:
            with session_scope() as db:
                alert = db.get(Alert, alert_id)
                if alert is None:
                    log.error("background fan-out: alert %s vanished", alert_id)
                    return
                _dispatch_venue(db, alert, route, venue, quantity)
        except Exception:
            log.exception("fan-out: order failed alert=%s ex=%s",
                          alert_id, venue.exchange)


def _find_working_entry(db, strategy_id: str, exchange: str, symbol: str) -> Order | None:
    """The (most recent) resting limit ENTRY for this (strategy, venue, symbol), if any.
    Order carries no strategy_id, so join Alert to scope it to the strategy."""
    return (db.query(Order)
            .join(Alert, Alert.id == Order.alert_id)
            .filter(Order.status == "working", Order.order_type == "limit",
                    Order.exchange == exchange, Order.symbol == symbol,
                    Alert.strategy_id == strategy_id)
            .order_by(Order.id.desc())
            .first())


def _cancel_working_entry(db, order: Order, strategy_id: str) -> None:
    """Cancel a resting limit entry on its venue, BOOKING any fill that raced the cancel
    (via the shared `book_limit_fill_delta`, so a partial isn't lost), then mark it
    cancelled. Pure de-risking — runs before `decide`, so the kill-switch never blocks it."""
    adapter = get_registry().get(order.exchange)
    handle = order.exchange_order_id or order.client_order_id
    try:
        adapter.cancel_order(order.symbol, handle)
    except Exception:
        log.exception("cancel-on-close: cancel_order failed order=%s", order.id)
    try:
        st = adapter.order_status(order.symbol, handle)
        if st.state != ORDER_STATE_UNKNOWN:
            book_limit_fill_delta(db, order, strategy_id, st)
    except Exception:
        log.exception("cancel-on-close: status/book failed order=%s", order.id)
    order.status = "cancelled"
    order.updated_at = _utcnow()


def _dispatch_venue(db, alert: Alert, route, venue, alert_quantity: float) -> None:
    """Apply the portfolio manager's decision for ONE venue."""
    action = alert.action.lower()
    notifier = get_notifier()

    # Cancel-on-close (limit-entry safety): a resting limit ENTRY for this
    # (strategy, venue, symbol) is cancelled by an OPPOSITE signal (its close); a
    # SAME-direction signal is ignored (the entry is already pending, don't double-place).
    # An UNFILLED entry → the close is a no-op (NO short — this is the fix); a PARTIAL fill
    # → book it, then fall through so `decide` CLOSES the partial. Runs BEFORE decide, so
    # the kill-switch can't block the de-risking cancel.
    working = _find_working_entry(db, route.strategy_id, venue.exchange, venue.symbol)
    if working is not None:
        if action == working.side:
            log.info("limit-entry: repeat %s for %s/%s ignored (entry already resting)",
                     action, route.strategy_id, venue.symbol)
            return
        _cancel_working_entry(db, working, route.strategy_id)
        net = portfolio._net_qty(db, route.strategy_id, venue.exchange, venue.symbol)
        if abs(net) <= portfolio._FLAT_EPS:
            log.info("cancel-on-close: %s/%s entry was unfilled → flat, no order placed",
                     route.strategy_id, venue.symbol)
            return
        log.info("cancel-on-close: %s/%s entry partially filled (net %+g) → closing the partial",
                 route.strategy_id, venue.symbol, net)
        # fall through: decide() now sees the partial position and CLOSES it (market)

    sizing = portfolio.decide(db, route, venue, action, alert_quantity=alert_quantity)
    if sizing.decision is Decision.REJECT:
        record_rejected_order(db, alert, venue, action, sizing.reason)
        notifier.order_rejected(route.strategy_id, venue.exchange, venue.symbol,
                                action, sizing.reason)
        log.info("portfolio REJECT %s/%s/%s: %s",
                 route.strategy_id, venue.exchange, venue.symbol, sizing.reason)
        return
    if sizing.paper:
        notifier.paper_trade(route.strategy_id, venue.exchange, venue.symbol,
                             action, sizing.qty)
    # Limit-entry: a managed OPEN rests a GTC limit at the alert price (the CLOSE stays a
    # market order for fill certainty; sar/ALERT_DRIVEN is unaffected). Fall back to market
    # if the alert carried no usable price, so a signal is never silently dropped.
    if (route.entry == "limit" and sizing.decision is Decision.OPEN
            and (alert.signal_price or 0) > 0):
        cloid = make_client_order_id(alert.id, venue.exchange, venue.symbol)
        execute_order(db, alert, venue, quantity=sizing.qty, order_type="limit",
                      limit_price=alert.signal_price, client_order_id=cloid)
    else:
        execute_order(db, alert, venue, quantity=sizing.qty)


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request,
                              background_tasks: BackgroundTasks,
                              settings: Settings = Depends(get_settings)):
    body = await _parse_body(request)
    alert = _validate(body)
    source_ip = request.client.host if request.client else ""
    _authorise(alert, settings.webhook_secret, source_ip)

    key = idempotency_key(alert)
    route = request.app.state.strategy_router.get(alert.strategy_id)
    notifier = get_notifier()

    # Alert-driven (sar=true) strategies MUST carry a quantity; managed
    # (sar=false) strategies size themselves, so quantity is optional/ignored there.
    if route is not None and route.sar and not alert.quantity:
        raise HTTPException(status_code=422,
                            detail="quantity required for sar=true (alert-driven) strategy")

    # --- fast + synchronous: dedup-gate + persist the alert in a short txn ---
    try:
        with session_scope() as db:
            alert_row = _persist_alert(db, alert, body, key, source_ip)
            alert_id = alert_row.id
    except IntegrityError:
        log.info("Duplicate alert ignored key=%s strat=%s", key, alert.strategy_id)
        background_tasks.add_task(notifier.duplicate_alert, alert.strategy_id, key)
        return {"status": "duplicate", "idempotency_key": key}

    if route is None:
        background_tasks.add_task(notifier.unknown_strategy, alert.strategy_id)
        return {"status": "skipped", "reason": "unknown_strategy",
                "alert_id": alert_id, "idempotency_key": key}

    if not route.enabled_venues():
        background_tasks.add_task(notifier.disabled_strategy, alert.strategy_id)
        return {"status": "skipped", "reason": "no_enabled_venues",
                "alert_id": alert_id, "idempotency_key": key,
                "strategy_id": alert.strategy_id}

    # --- slow exchange round-trips happen AFTER we answer TradingView ---
    background_tasks.add_task(_run_fan_out, alert_id, route, alert.quantity or 0.0)
    return {
        "status": "accepted",
        "alert_id": alert_id,
        "idempotency_key": key,
        "strategy_id": alert.strategy_id,
        "venues": [v.exchange for v in route.enabled_venues()],
    }
