"""Background poller that records funding payments into the `funding_events`
table, so the performance page can sum funding without an exchange round-trip per
request. Idempotent: a UNIQUE(exchange, symbol, funding_time) + insert-or-ignore
means re-scanning an overlapping window never double-counts.

Mirrors `retry_worker`: an async loop that offloads the blocking SDK calls to a
thread so the event loop stays free.
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import get_settings
from .db import session_scope
from .exchanges.registry import get_registry
from .models import (AppMeta, ArbEquitySnapshot, ArbFundingEvent, ArbLeg, ArbPosition,
                     EquitySnapshot, FundingEvent)

log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 3600                     # hourly is ample (funding is ≤ hourly)
_LOOKBACK_MS = 3 * 24 * 3600 * 1000          # re-scan 3 days each poll; dedup absorbs overlap

# Arb perp legs live on a non-closed ArbPosition (closed pairs are flat — no more
# settlements accrue). Matches the per-arb attribution window in funding_arb.py.
_ARB_NON_CLOSED = ("opening", "open", "closing", "error")


def _hour_floor(now: datetime | None = None) -> datetime:
    """The top-of-hour (HH:00:00 UTC) for `now` (default: current) — the canonical
    snapshot timestamp and the single definition of the hourly boundary."""
    return (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)


def _seconds_to_next_hour() -> float:
    """Seconds until the next HH:00:00 UTC, so the loop wakes on the hour."""
    now = datetime.now(timezone.utc)
    nxt = _hour_floor(now) + timedelta(hours=1)
    return max(1.0, (nxt - now).total_seconds())


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


def write_equity_snapshot(router, captured_at=None) -> bool:
    """Capture the current TRUE total PnL as one `EquitySnapshot` row — the points the
    equity curve plots. Reuses the exact `_performance` headline math, so every
    snapshot equals the page's Total PnL (realized + unrealized + funding − commission).
    `captured_at` pins the timestamp — the loop passes the top of the hour so points
    land on HH:00; defaults to now. Best-effort — True when a row was written."""
    from .routes.dashboard import (_performance, _by_exchange_totals,   # local: avoid import cycle
                                   _by_strategy_totals, _clear_equity_cache)
    try:
        with session_scope() as db:
            perf = _performance(db, router)
            t = perf["totals"]
            by_ex = _by_exchange_totals(perf)
            by_strat = _by_strategy_totals(perf)        # {strategy: realized+unrealized−commission}
            db.add(EquitySnapshot(
                captured_at=captured_at or datetime.now(timezone.utc),
                total_pnl=t["total"], realized=t["realized"], unrealized=t["unrealized"],
                funding=t["funding"], commission=t["commission"],
                by_exchange=json.dumps(by_ex), by_strategy=json.dumps(by_strat)))
        _clear_equity_cache()           # a fresh point is persisted -> don't serve the stale dataset
        return True
    except Exception:
        log.exception("equity snapshot failed")
        return False


def write_arb_equity_snapshot(captured_at=None) -> bool:
    """Capture the funding-arb book's current NET (funding − commission + directional
    MTM) as one `ArbEquitySnapshot` row — the /funding-arb equity curve's points.
    Reuses the SAME `_arb_performance` roll-up as the report page, so the snapshot
    equals the page headline (the live MTM is marked at capture time; historical points
    pre-dating this can't be re-marked, but directional ≈0 on a neutral book). Writes
    ONLY when arb positions exist (the curve builds forward from the first arb) and ONLY
    to the arb table — never `equity_snapshots` (the isolation invariant). `captured_at`
    pins the timestamp (HH:00 from the loop). Best-effort."""
    from .routes.funding_arb import (_arb_performance, _arb_by_venue,
                                     _clear_arb_equity_cache)
    try:
        with session_scope() as db:
            if db.query(ArbPosition.id).first() is None:
                return False                      # no arb book yet -> no curve point
            report = _arb_performance(db)
            t = report["totals"]
            db.add(ArbEquitySnapshot(
                captured_at=captured_at or datetime.now(timezone.utc),
                net=t["net"], funding=t["funding"], commission=t["commission"],
                by_venue=json.dumps(_arb_by_venue(report)), source="live"))
        _clear_arb_equity_cache()
        return True
    except Exception:
        log.exception("arb equity snapshot failed")
        return False


# Bump when the backfill LOGIC changes: the fingerprint shifts, so the one-time
# backfill re-runs once on the next boot (replacing its rows) instead of being locked
# out by the rows it already wrote — no manual DB surgery on the box. Ordinary reboots
# (same version + start) don't re-run it.
_BACKFILL_VERSION = 2     # v2: current-day point floored to the hour (was the off-hour run time)


def _maybe_backfill_equity() -> None:
    """ONE-TIME historical equity backfill from each exchange's OWN records, in the
    background after startup, so the curve shows history with no manual trigger.
    EQUITY_BACKFILL_START sets the start date (blank = skip).

    Guarded by a fingerprint (version + start) in `app_meta`, NOT merely by "backfill
    rows exist": that keeps it re-runnable after a code fix (bump _BACKFILL_VERSION)
    without hand-deleting rows on the box, while staying one-time across normal
    reboots. The marker is written ONLY after a CLEAN run (rows written, no venue
    errors), so a partial/failed pull retries next boot instead of locking in bad
    data (e.g. one venue's history transiently unavailable)."""
    start = (get_settings().equity_backfill_start or "").strip()
    if not start:
        return
    fingerprint = f"v{_BACKFILL_VERSION}:{start}"
    with session_scope() as db:
        m = db.get(AppMeta, "equity_backfill")
        if m is not None and m.value == fingerprint:
            return                                  # already done for this version+start
    try:
        from .equity_backfill import backfill_equity
        sdt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        s = backfill_equity(int(sdt.timestamp() * 1000))
        log.info("equity backfill from %s: %d rows (bybit %d, hl %d) errors=%s",
                 start, s["rows"], s["bybit_events"], s["hl_points"], s["errors"])
        if s["rows"] > 0 and not s["errors"]:       # mark done only on a clean run
            with session_scope() as db:
                db.merge(AppMeta(key="equity_backfill", value=fingerprint,
                                 updated_at=datetime.now(timezone.utc)))
            from .routes.dashboard import _clear_equity_cache
            _clear_equity_cache()                   # new history rows -> drop the cached dataset
    except Exception:
        log.exception("startup equity backfill failed")


_OFFHOUR_CLEANUP_VERSION = 1     # bump to re-sweep


def _maybe_cleanup_offhour_live() -> None:
    """ONE-TIME: drop pre-hour-align startup snapshots — `source="live"` rows NOT
    stamped on HH:00:00. Before the hour-align fix each redeploy wrote a snapshot ~90s
    after boot (off-hour); the loop now writes ONLY on the hour, so any off-hour live
    row is leftover cruft. Touches ONLY off-hour `source="live"` rows — never backfill,
    never an on-hour row. Guarded by an app_meta marker so it runs once."""
    fingerprint = f"v{_OFFHOUR_CLEANUP_VERSION}"
    with session_scope() as db:
        m = db.get(AppMeta, "offhour_live_cleanup")
        if m is not None and m.value == fingerprint:
            return
    try:
        n = 0
        with session_scope() as db:
            for r in db.query(EquitySnapshot).filter(EquitySnapshot.source == "live").all():
                ts = r.captured_at
                if ts is not None and (ts.minute or ts.second or ts.microsecond):
                    db.delete(r)
                    n += 1
            db.merge(AppMeta(key="offhour_live_cleanup", value=fingerprint,
                             updated_at=datetime.now(timezone.utc)))
        if n:
            log.info("equity: swept %d off-hour (pre-hour-align) live snapshots", n)
            from .routes.dashboard import _clear_equity_cache
            _clear_equity_cache()
    except Exception:
        log.exception("off-hour live snapshot cleanup failed")


async def _startup_backfill(delay: float, stop_event) -> None:
    """One-time, shortly after boot (not AT boot — keeps startup network-free):
    backfill the equity curve from exchange history. Runs as its OWN task so it never
    delays the funding polls. No off-hour snapshot is written here — the curve stays
    current via the live-anchored right edge + persisted history, and the hourly
    snapshots land on HH:00."""
    try:
        await asyncio.sleep(delay)
        if stop_event is not None and stop_event.is_set():
            return
        await asyncio.to_thread(_maybe_cleanup_offhour_live)   # one-time: drop pre-fix off-hour rows
        await asyncio.to_thread(_maybe_backfill_equity)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("funding_worker startup backfill error")


async def funding_loop(router, *, poll_interval_sec: float = _POLL_INTERVAL_SEC,
                       stop_event: asyncio.Event | None = None,
                       align_hour: bool = True) -> None:
    """Record funding + one equity snapshot per hour, aligned to the top of the hour
    (HH:00) by default — so points are evenly spaced regardless of when the app started
    or how often it's redeployed. Blocking SDK work runs in a thread. align_hour=False
    uses poll_interval_sec directly (tests)."""
    log.info("funding_worker started (align_hour=%s, poll=%.0fs)", align_hour, poll_interval_sec)
    # One-time backfill runs as a SEPARATE task so it never gates the funding polls.
    cap_task = asyncio.create_task(
        _startup_backfill(min(90.0, poll_interval_sec), stop_event))
    try:
        while True:
            # Sleep to the next HH:00 (or poll_interval_sec in tests). Sleeping FIRST
            # keeps app startup network-free.
            try:
                await asyncio.sleep(_seconds_to_next_hour() if align_hour else poll_interval_sec)
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
            # Dedicated ARB poll — same loop, AFTER the directional poll, own
            # session_scope; writes only ArbFundingEvent (directional stays blind),
            # and its failure can't abort the directional poll above.
            try:
                n = await asyncio.to_thread(poll_arb_once)
                if n:
                    log.info("funding_worker: stored %d new ARB funding events", n)
            except Exception:
                log.exception("funding_worker arb loop error")
            # One true total-PnL point for the equity curve, stamped on the hour (HH:00)
            # so the curve is evenly spaced. (AFTER funding is recorded.)
            try:
                cap = _hour_floor() if align_hour else None
                if await asyncio.to_thread(write_equity_snapshot, router, cap):
                    log.info("funding_worker: wrote equity snapshot")
            except Exception:
                log.exception("funding_worker equity snapshot error")
            # Arb equity point (own table, isolated) — same HH:00, after the arb poll.
            try:
                if await asyncio.to_thread(write_arb_equity_snapshot, cap):
                    log.info("funding_worker: wrote arb equity snapshot")
            except Exception:
                log.exception("funding_worker arb equity snapshot error")
    finally:
        cap_task.cancel()
