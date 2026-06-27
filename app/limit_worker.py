"""Background fill-poller for resting limit orders (the limit-entry feature).

A market order fills synchronously in `execute_order`; a GTC limit can rest, so its
fills are observed asynchronously here. Every few seconds this scans `Order.status
== "working"` and, per order, asks the venue for the order's status — booking any NEW
fill delta to the ledger (reusing `executor._apply_fill_to_position`, which is already
partial-fill aware and takes `_LEDGER_LOCK`) and transitioning the row on a terminal
state. Because it scans PERSISTED `working` rows, it resumes across restarts for free.

Cancel-on-close (cancel a resting entry when the close signal arrives) is P2 and lives
in the webhook fan-out, not here. See docs/limit-entry-plan.md.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import timedelta, timezone

from .config import get_settings
from .db import session_scope
from .exchanges.registry import get_registry
from .executor import book_limit_fill_delta, _utcnow
from .models import Alert, Order
from .notifier import get_notifier
from .schemas import (ORDER_STATE_FILLED, ORDER_STATE_CANCELLED, ORDER_STATE_REJECTED,
                      ORDER_STATE_UNKNOWN, FEE_SOURCE_EXCHANGE)

log = logging.getLogger(__name__)


def _as_utc(dt):
    """SQLite returns naive datetimes even for tz-aware columns; coerce to UTC so the
    staleness arithmetic never mixes naive + aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _maybe_stale_alert(order: Order, alert: Alert) -> None:
    """Re-ping Telegram every `limit_stale_alert_hours` while an entry rests unfilled
    (notify-only — NEVER cancels, D2). Stamps `last_stale_alert_at` so it fires once per
    window, not every poll."""
    hours = get_settings().limit_stale_alert_hours
    if not hours:
        return
    since = _as_utc(order.last_stale_alert_at or order.created_at)
    now = _utcnow()
    if since is None or now - since < timedelta(hours=hours):
        return
    age = (now - (_as_utc(order.created_at) or now)).total_seconds() / 3600.0
    get_notifier().limit_order_stale(
        alert.strategy_id, order.exchange, order.symbol, order.side,
        order.limit_price or 0.0, order.qty_base, order.qty_base_filled or 0.0, age)
    order.last_stale_alert_at = now
    log.info("limit-entry: staleness ping order=%s %s/%s (~%.0fh resting)",
             order.id, alert.strategy_id, order.symbol, age)


def _poll_working_orders() -> None:
    """One pass over resting limit orders. SYNCHRONOUS (blocking SDK calls) — the caller
    runs it in a thread so the event loop stays free. One `session_scope`; per-order
    failures are isolated so one bad venue call can't wedge the rest of the scan."""
    with session_scope() as db:
        working = (db.query(Order)
                   .filter(Order.status == "working")
                   .order_by(Order.id).limit(50).all())
        for order in working:
            try:
                _poll_one(db, order)
            except Exception:
                log.exception("limit_worker: poll failed order=%s", order.id)


def _poll_one(db, order: Order) -> None:
    alert = db.get(Alert, order.alert_id)
    if alert is None:
        log.error("limit_worker: orphan working order id=%s (no alert)", order.id)
        order.status = "dead"
        return

    adapter = get_registry().get(order.exchange)
    handle = order.exchange_order_id or order.client_order_id
    st = adapter.order_status(order.symbol, handle)
    if st.state == ORDER_STATE_UNKNOWN:
        return   # transient lookup miss — retry next pass (a genuinely stuck order is a P3 alert)

    newly = book_limit_fill_delta(db, order, alert.strategy_id, st)   # atomic + no double-count
    if newly > 0:
        log.info("limit fill order=%s strat=%s %s %s +%g @ %.6f (cum %g)",
                 order.id, alert.strategy_id, order.symbol, order.side,
                 newly, order.fill_price or 0.0, order.qty_base_filled)

    if st.state == ORDER_STATE_FILLED:
        order.status = "success"
        order.qty_base = order.qty_base_filled           # final fill = qty_base (market-path parity)
        order.fee_source = FEE_SOURCE_EXCHANGE
        get_notifier().limit_order_filled(alert.strategy_id, order.exchange, order.symbol,
                                          order.side, order.qty_base_filled, order.fill_price or 0.0)
    elif st.state in (ORDER_STATE_CANCELLED, ORDER_STATE_REJECTED):
        order.status = "cancelled"                       # any partial already booked above
        # (cancel-on-close alerts in the webhook; the poller only sees EXTERNAL cancels — left
        # silent to avoid a double-alert race with cancel-on-close)
    else:
        _maybe_stale_alert(order, alert)                 # still resting -> notify-only staleness (D2)
    order.updated_at = _utcnow()


def reconcile_on_boot() -> None:
    """One-shot reconcile of persisted resting orders on startup — books any fill/cancel
    that landed while the app was down (a deploy restart) before the normal poll cadence.
    The orders are DB-persisted, so this fully covers the routine auto-deploy restart."""
    with session_scope() as db:
        n = db.query(Order).filter(Order.status == "working").count()
    if n:
        log.info("limit-entry boot reconcile: re-checking %d resting order(s)", n)
    _poll_working_orders()


async def limit_loop(router, *, poll_interval_sec: float = 4.0,
                     stop_event: asyncio.Event | None = None) -> None:
    """Background coroutine: poll resting limit orders for fills/cancels. Offloads the
    blocking work to a thread (like retry_loop). `router` is accepted for signature
    parity but unused — each order carries its own venue/symbol."""
    log.info("limit_worker started (poll=%.1fs)", poll_interval_sec)
    while True:
        if stop_event is not None and stop_event.is_set():
            log.info("limit_worker stopping (stop_event set)")
            return
        try:
            await asyncio.to_thread(_poll_working_orders)
        except Exception:
            log.exception("limit_worker loop error")
        try:
            await asyncio.sleep(poll_interval_sec)
        except asyncio.CancelledError:
            log.info("limit_worker cancelled")
            return
