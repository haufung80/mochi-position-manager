from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from . import arb_executor
from .db import session_scope
from .models import Alert, ArbLeg, ArbOrder, Order
from .executor import execute_order
from .routing import VenueRoute

log = logging.getLogger(__name__)


def _run_due_retries() -> None:
    """Pick up due `status=retrying` orders (directional Order AND arb ArbOrder)
    and re-run them. SYNCHRONOUS (blocking exchange calls) — the caller runs this
    in a thread so the event loop stays free for inbound webhooks / health checks.

    Two INDEPENDENT scans, each in its OWN `session_scope` with its own `.limit`:
      1. directional `Order` -> `executor.execute_order` (unchanged; still owns the
         `_LEDGER_LOCK` ledger path + the orphan guard).
      2. arb `ArbOrder` -> `arb_executor.execute_leg` (writes ArbLeg/ArbOrder only,
         takes NO `_LEDGER_LOCK`, re-resolves the adapter from the ArbOrder's OWN
         `exchange`+`account` so an arb retry can never fall back to the default
         account).
    The two scans are isolated so neither can corrupt the other on a shared pass.
    """
    _run_due_order_retries()
    _run_due_arb_retries()


def _run_due_order_retries() -> None:
    """Directional `Order` retry scan (the original behaviour, unchanged)."""
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


def _run_due_arb_retries() -> None:
    """Arb `ArbOrder` retry scan, in its OWN session_scope and limit.

    Dispatches to `arb_executor.execute_leg`, which re-resolves the exact
    `(exchange, account)` adapter from the leg (fail-loud on mis-config — NEVER the
    default account) and writes ArbLeg/ArbOrder only. Does NOT take `_LEDGER_LOCK`,
    so it can't lengthen the directional ledger critical section.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        due = (
            db.query(ArbOrder)
            .filter(ArbOrder.status == "retrying", ArbOrder.next_retry_at <= now)
            .limit(50)
            .all()
        )
        for order in due:
            leg = db.get(ArbLeg, order.arb_leg_id)
            if leg is None:
                log.error("retry_worker: orphan arb order id=%s (no leg)", order.id)
                order.status = "dead"
                continue
            log.info("retry_worker: replaying arb order id=%s leg=%s attempt=%s",
                     order.id, leg.id, order.attempts + 1)
            arb_executor.execute_leg(db, leg, existing_order=order)


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
