from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from .db import session_scope
from .models import Alert, Order
from .executor import execute_order
from .routing import VenueRoute

log = logging.getLogger(__name__)


def _run_due_retries() -> None:
    """Pick up `status=retrying` orders whose `next_retry_at` is past-due and
    re-run them. SYNCHRONOUS (blocking exchange calls) — the caller runs this in
    a thread so the event loop stays free for inbound webhooks / health checks.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        due_orders = (
            db.query(Order)
            .filter(Order.status == "retrying", Order.next_retry_at <= now)
            .limit(50)
            .all()
        )
        for order in due_orders:
            alert = db.get(Alert, order.alert_id)
            if alert is None:
                log.error("retry_worker: orphan order id=%s (no alert)", order.id)
                order.status = "dead"
                continue
            # Reconstruct the venue from the order's frozen-at-fire-time fields,
            # so retries survive strategy reconfigurations between original fire
            # and retry attempt.
            venue = VenueRoute(
                exchange=order.exchange,
                symbol=order.symbol,
                enabled=True,
            )
            log.info("retry_worker: replaying order id=%s alert=%s attempt=%s",
                     order.id, alert.id, order.attempts + 1)
            execute_order(db, alert, venue,
                          quantity=order.qty_base,
                          existing_order=order)


async def retry_loop(router, *, poll_interval_sec: float = 5.0,
                     stop_event: asyncio.Event | None = None) -> None:
    """Background coroutine that periodically replays due retrying orders.

    The actual work is offloaded to a thread (`asyncio.to_thread`) because the
    exchange SDKs block; running them inline here would freeze the whole event
    loop on every poll. `router` is accepted for signature stability but the
    work reconstructs venues from each order's own fields.
    """
    log.info("retry_worker started (poll=%.1fs)", poll_interval_sec)
    while True:
        if stop_event is not None and stop_event.is_set():
            log.info("retry_worker stopping (stop_event set)")
            return
        try:
            await asyncio.to_thread(_run_due_retries)
        except Exception:
            log.exception("retry_worker loop error")
        try:
            await asyncio.sleep(poll_interval_sec)
        except asyncio.CancelledError:
            log.info("retry_worker cancelled")
            return
