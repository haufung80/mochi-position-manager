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
from .models import ArbFundingEvent, ArbLeg, ArbPosition, FundingEvent

log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 3600                     # hourly is ample (funding is ≤ hourly)
_LOOKBACK_MS = 3 * 24 * 3600 * 1000          # re-scan 3 days each poll; dedup absorbs overlap

# Arb perp legs live on a non-closed ArbPosition (closed pairs are flat — no more
# settlements accrue). Matches the per-arb attribution window in funding_arb.py.
_ARB_NON_CLOSED = ("opening", "open", "closing", "error")


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


def _arb_perp_legs() -> list[tuple[str, str, str]]:
    """Distinct (exchange, account, symbol) over PERP legs of non-closed arbs.

    Spot legs are excluded — funding accrues only on the perp leg. Distinct so two
    concurrent arbs that (by the A.5 symbol-exclusivity) can't share a symbol still
    collapse to one poll per (exchange, account, symbol); the `ArbFundingEvent`
    UNIQUE(exchange, account, symbol, funding_time) makes a repeated/overlapping
    scan insert-or-ignore, so one account-wide settlement is recorded exactly once.
    """
    with session_scope() as db:
        rows = (
            db.query(ArbLeg.exchange, ArbLeg.account, ArbLeg.symbol)
            .join(ArbPosition, ArbPosition.id == ArbLeg.arb_id)
            .filter(ArbLeg.product == "perp",
                    ArbPosition.status.in_(_ARB_NON_CLOSED))
            .distinct()
            .all()
        )
    return sorted({(ex, acct, sym) for ex, acct, sym in rows})


def poll_arb_once() -> int:
    """DEDICATED arb funding poll — independent of the directional `_venue_pairs`
    scan (the arb book has no `strategies.yaml` entry). Iterates the perp legs of
    every non-closed ArbPosition across BOTH exchanges via the dedicated arb
    account (`get("bybit","arb")` / `get("hyperliquid","arb")`) and writes
    `ArbFundingEvent` keyed by `(exchange, account, symbol, funding_time)`
    (insert-or-ignore). Returns the number of newly-inserted rows.

    Writes ONLY `ArbFundingEvent`; never `FundingEvent`. The directional funding /
    `_performance` / `_equity_curve` queries are physically blind to this table, so
    arb funding can't bleed into the directional headline or equity curve. Resilient
    per leg — one venue's failure doesn't abort the rest.
    """
    registry = get_registry()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - _LOOKBACK_MS
    inserted = 0
    for exchange, account, symbol in _arb_perp_legs():
        try:
            ex = registry.get(exchange, account)   # fail loud on mis-config -> skip
            events = ex.get_funding(symbol, start_ms, now_ms)
        except Exception:
            log.exception("arb funding poll failed for %s:%s/%s",
                          exchange, account, symbol)
            continue
        for ev in events:
            ts_ms = int(ev.get("time_ms") or 0)
            amount = float(ev.get("amount") or 0.0)
            if not ts_ms:
                continue
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            with session_scope() as db:
                res = db.execute(
                    sqlite_insert(ArbFundingEvent)
                    .values(exchange=exchange, account=account, symbol=symbol,
                            funding_time=ts, amount=amount,
                            created_at=datetime.now(timezone.utc))
                    .on_conflict_do_nothing(
                        index_elements=["exchange", "account", "symbol",
                                        "funding_time"])
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
        # Dedicated ARB poll — runs in the SAME hourly loop, AFTER the directional
        # poll, in its OWN session_scope (poll_arb_once). It writes the separate
        # ArbFundingEvent table only, so the directional funding/equity stay blind
        # to it; its failure can't abort the directional poll above.
        try:
            n = await asyncio.to_thread(poll_arb_once)
            if n:
                log.info("funding_worker: stored %d new ARB funding events", n)
        except Exception:
            log.exception("funding_worker arb loop error")
