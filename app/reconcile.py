"""Re-baseline the stored per-strategy ledger to live exchange positions.

The exchange only knows per-(exchange, symbol) totals — it has no concept of
which strategy a position belongs to. So we attribute a symbol's live position
to the single configured strategy that trades it. If more than one configured
strategy maps to the same (exchange, symbol), we can't split the aggregate and
skip those, reporting them back so the operator knows.

This is the manual counterpart to the eventual target-state self-heal: run it
on demand to clear stale residue (e.g. cutover artifacts) and re-baseline the
dashboard to reality.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from .db import session_scope
from .exchanges.registry import get_registry
from .models import StrategyPosition

log = logging.getLogger(__name__)


def venue_claims(router) -> dict[tuple[str, str], list[str]]:
    """Map each (exchange, symbol) to the strategy_ids that trade it."""
    claims: dict[tuple[str, str], list[str]] = {}
    for route in router.all():
        for v in route.venues:
            claims.setdefault((v.exchange, v.symbol), []).append(route.strategy_id)
    return claims


def single_owner_map(router) -> dict[tuple[str, str], str]:
    """(exchange, symbol) -> the sole strategy that trades it. Pairs traded by
    more than one strategy are omitted — an aggregate can't be attributed to one
    strategy. Used for position sync and per-strategy funding attribution."""
    return {k: ids[0] for k, ids in venue_claims(router).items() if len(ids) == 1}


def sync_strategy_positions(router) -> dict:
    """Set each configured strategy's ledger to its live exchange position.
    Returns {"synced": [...], "skipped": [...]} for display."""
    registry = get_registry()

    # Detect (exchange, symbol) claimed by more than one strategy — can't
    # attribute an aggregate exchange position to a single strategy.
    claims = venue_claims(router)

    synced: list[dict] = []
    skipped: list[dict] = []
    now = datetime.now(timezone.utc)

    with session_scope() as db:
        for route in router.all():
            for v in route.venues:
                tag = {"strategy_id": route.strategy_id,
                       "exchange": v.exchange, "symbol": v.symbol}
                if len(claims[(v.exchange, v.symbol)]) > 1:
                    skipped.append({**tag, "reason": "symbol shared by multiple strategies"})
                    continue
                try:
                    qty, price = registry.get(v.exchange).get_position(v.symbol)
                except Exception as e:
                    log.exception("sync: get_position failed for %s/%s",
                                  v.exchange, v.symbol)
                    skipped.append({**tag, "reason": f"read failed: {type(e).__name__}"})
                    continue

                row = (
                    db.query(StrategyPosition)
                    .filter_by(strategy_id=route.strategy_id,
                               exchange=v.exchange, symbol=v.symbol)
                    .one_or_none()
                )
                if row is None:
                    row = StrategyPosition(strategy_id=route.strategy_id,
                                           exchange=v.exchange, symbol=v.symbol)
                    db.add(row)
                row.net_qty_base = qty
                row.net_qty_usd = qty * price
                row.last_price = price
                row.avg_entry_price = price   # re-baseline: true entry unknown -> use mark
                row.updated_at = now
                synced.append({**tag, "net_qty_base": qty, "net_qty_usd": qty * price})

    log.info("sync_strategy_positions: %d synced, %d skipped",
             len(synced), len(skipped))
    return {"synced": synced, "skipped": skipped}
