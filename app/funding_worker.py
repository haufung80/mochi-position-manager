"""Background poller that records funding payments into the `funding_events`
table, so the performance page can sum funding without an exchange round-trip per
request. Idempotent: a UNIQUE(exchange, symbol, funding_time) + insert-or-ignore
means re-scanning an overlapping window never double-counts.

Mirrors `retry_worker`: an async loop that offloads the blocking SDK calls to a
thread so the event loop stays free.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import session_scope
from .exchanges.registry import get_registry
from .models import FundingEvent

log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 3600                     # hourly is ample (funding is ≤ hourly)
_LOOKBACK_MS = 3 * 24 * 3600 * 1000          # re-scan 3 days each poll; dedup absorbs overlap


def _venue_pairs(router) -> list[tuple[str, str]]:
    """Distinct (exchange, symbol) across all enabled venues."""
    seen: set[tuple[str, str]] = set()
    for route in router.all():
        for v in route.enabled_venues():
            seen.add((v.exchange, v.symbol))
    return sorted(seen)


def poll_once(router) -> int:
    """Fetch + store new funding events for every configured venue. Returns the
    number of newly-inserted rows. Resilient per pair — one venue's failure does
    not abort the rest."""
    registry = get_registry()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - _LOOKBACK_MS
    inserted = 0
    for exchange, symbol in _venue_pairs(router):
        try:
            events = registry.get(exchange).get_funding(symbol, start_ms, now_ms)
        except Exception:
            log.exception("funding poll failed for %s/%s", exchange, symbol)
            continue
        for ev in events:
            ts_ms = int(ev.get("time_ms") or 0)
            amount = float(ev.get("amount") or 0.0)
            if not ts_ms:
                continue
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            with session_scope() as db:
                res = db.execute(
                    sqlite_insert(FundingEvent)
                    .values(exchange=exchange, symbol=symbol, funding_time=ts,
                            amount=amount, created_at=datetime.now(timezone.utc))
                    .on_conflict_do_nothing(
                        index_elements=["exchange", "symbol", "funding_time"])
                )
                inserted += res.rowcount or 0
    return inserted


async def funding_loop(router, *, poll_interval_sec: float = _POLL_INTERVAL_SEC,
                       stop_event: asyncio.Event | None = None) -> None:
    """Periodically record funding events. Blocking SDK work runs in a thread."""
    log.info("funding_worker started (poll=%.0fs, first scan after one interval)",
             poll_interval_sec)
    while True:
        # Sleep FIRST: funding accrues slowly, and this keeps app startup
        # network-free (no exchange round-trips on boot / during tests).
        try:
            await asyncio.sleep(poll_interval_sec)
        except asyncio.CancelledError:
            log.info("funding_worker cancelled")
            return
        if stop_event is not None and stop_event.is_set():
            log.info("funding_worker stopping (stop_event set)")
            return
        try:
            n = await asyncio.to_thread(poll_once, router)
            if n:
                log.info("funding_worker: stored %d new funding events", n)
        except Exception:
            log.exception("funding_worker loop error")
