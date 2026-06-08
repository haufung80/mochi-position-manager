"""Backfill the equity curve from each exchange's OWN history — the honest source,
never reconstructed from our truncated ledger (which would fabricate PnL).

Builds daily per-exchange + aggregate EquitySnapshot rows from a start date:
- Bybit: cumulative realized closed-PnL + funding. (No historical unrealized — an
  approximation; the curve's going-forward live points include unrealized.)
- Hyperliquid: cumulative PnL from the portfolio endpoint (reflects account value,
  i.e. includes unrealized).

Both rebased to 0 at the start, so the curve reads "PnL since <start>". Idempotent:
re-running replaces ONLY source="backfill" rows, never the live hourly snapshots.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone

from .db import session_scope
from .exchanges.registry import get_registry
from .models import EquitySnapshot

log = logging.getLogger(__name__)
_DAY_MS = 86_400_000


def _build_daily_rows(bybit_events, hl_points, start_ms: int, now_ms: int):
    """Pure: -> [(day_ms, bybit_cum, hl_cum_since_start)] for each UTC day in
    [start, now]. `bybit_events` = [(ts_ms, pnl_delta)] (realized + funding); summed
    cumulatively from `start`. `hl_points` = [(ts_ms, cumulative_pnl)] (already
    cumulative); carried forward per day and rebased to its value at `start`."""
    bybit_events = sorted(bybit_events, key=lambda ev: ev[0])
    hl_points = sorted(hl_points, key=lambda p: p[0])
    hl_base = 0.0
    for ts, v in hl_points:               # cumulative value at/just before start
        if ts <= start_ms:
            hl_base = v
        else:
            break
    day0 = (start_ms // _DAY_MS) * _DAY_MS
    last = (now_ms // _DAY_MS) * _DAY_MS
    rows: list[tuple[int, float, float]] = []
    bi, bcum, hi, hcum = 0, 0.0, 0, hl_base
    d = day0
    while d <= last:
        d_end = d + _DAY_MS - 1
        while bi < len(bybit_events) and bybit_events[bi][0] <= d_end:
            if bybit_events[bi][0] >= start_ms:
                bcum += bybit_events[bi][1]
            bi += 1
        while hi < len(hl_points) and hl_points[hi][0] <= d_end:
            hcum = hl_points[hi][1]
            hi += 1
        rows.append((d, round(bcum, 6), round(hcum - hl_base, 6)))
        d += _DAY_MS
    return rows


def backfill_equity(start_ms: int, now_ms: int | None = None) -> dict:
    """Pull each venue's history, build daily aggregate snapshots, persist them
    (replacing prior backfill rows). Returns a summary dict for the admin UI."""
    reg = get_registry()
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    summary = {"start_ms": start_ms, "now_ms": now_ms,
               "bybit_events": 0, "hl_points": 0, "rows": 0, "errors": []}

    bybit_events: list[tuple[int, float]] = []
    try:
        bx = reg.get("bybit")
        bybit_events = list(bx.get_closed_pnl(start_ms, now_ms))
        bybit_events += list(bx.get_account_funding(start_ms, now_ms))
    except Exception as e:                                  # noqa: BLE001 — best-effort import
        summary["errors"].append(f"bybit: {e}")
        log.warning("equity backfill: bybit history failed: %s", e)
    summary["bybit_events"] = len(bybit_events)

    hl_points: list[tuple[int, float]] = []
    try:
        hl_points = list(reg.get("hyperliquid").get_pnl_history())
    except Exception as e:                                  # noqa: BLE001
        summary["errors"].append(f"hyperliquid: {e}")
        log.warning("equity backfill: hyperliquid history failed: %s", e)
    summary["hl_points"] = len(hl_points)

    if not bybit_events and not hl_points:
        summary["errors"].append("no data from either venue — existing backfill left untouched")
        return summary

    rows = _build_daily_rows(bybit_events, hl_points, start_ms, now_ms)
    with session_scope() as db:
        db.query(EquitySnapshot).filter(EquitySnapshot.source == "backfill").delete()
        for d, b, h in rows:
            cap_ms = min(d + _DAY_MS // 2, now_ms)         # noon UTC, never in the future
            db.add(EquitySnapshot(
                captured_at=datetime.fromtimestamp(cap_ms / 1000, tz=timezone.utc),
                total_pnl=b + h, realized=0.0, unrealized=0.0, funding=0.0, commission=0.0,
                by_exchange=json.dumps({"bybit": b, "hyperliquid": h}),
                source="backfill"))
    summary["rows"] = len(rows)
    log.info("equity backfill: wrote %d daily rows (bybit %d events, hl %d points)",
             len(rows), summary["bybit_events"], summary["hl_points"])
    return summary
