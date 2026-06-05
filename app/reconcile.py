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
from .exchanges.symbols import base_asset_of, symbol_for
from .executor import _fill_math
from .models import Alert, Order, StrategyPosition

log = logging.getLogger(__name__)

# Bybit is the price oracle for kline backfill (Hyperliquid coins share the price).
_ORACLE_EXCHANGE = "bybit"


def _oracle_symbol(exchange: str, symbol: str) -> str:
    """Map any venue's symbol to the Bybit USDT-perp ticker used for kline lookup."""
    try:
        return symbol_for(_ORACLE_EXCHANGE, base_asset_of(exchange, symbol))
    except Exception:
        return symbol if symbol.endswith("USDT") else f"{symbol}USDT"


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


def backfill_pnl_from_klines(router) -> dict:
    """Recompute `realized_pnl` and `avg_entry_price` for positions built from fills
    that weren't recorded with a price (`fill_price` 0/None — pre-execution-quality
    history), by replaying each strategy's own success fills through the shared fill
    math and pricing any unpriced fill with the market close at that fill's minute
    (a market order fills ~there).

    Two different guards, because the two figures mean different things:
    - `realized_pnl` is a HISTORICAL fact — the cumulative PnL booked on closes. It's
      valid whenever the fills are priced, regardless of the current net, so we always
      rewrite it. (This is what made round-trips on unpriced legs read as $0 — e.g. a
      short covered for a profit showing realized 0.)
    - `avg_entry_price` describes the CURRENT open position, so we only rewrite it when
      the replayed net matches the stored net (the fills fully explain it); a manually
      set / drifted net keeps its entry.

    Going forward fills are priced, so the live ledger stays correct without this.
    """
    oracle = get_registry().get("bybit")
    updated: list[dict] = []
    skipped: list[dict] = []
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        for sp in db.query(StrategyPosition).all():
            tag = {"strategy_id": sp.strategy_id, "exchange": sp.exchange, "symbol": sp.symbol}
            osym = _oracle_symbol(sp.exchange, sp.symbol)
            fills = (db.query(Order).join(Alert, Order.alert_id == Alert.id)
                       .filter(Alert.strategy_id == sp.strategy_id,
                               Order.exchange == sp.exchange, Order.symbol == sp.symbol,
                               Order.status == "success")
                       .order_by(Order.created_at).all())
            if not fills:
                continue
            net = avg = realized = 0.0
            ok = True
            for o in fills:
                px = o.fill_price if (o.fill_price and o.fill_price > 0) else oracle.get_kline_close(
                    osym, int(o.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000))
                if not px or not o.qty_base:
                    ok = False
                    break
                signed = o.qty_base * (1.0 if o.side == "buy" else -1.0)
                net, avg, rd = _fill_math(net, avg, signed, o.qty_base, px)
                realized += rd
            if not ok:
                skipped.append({**tag, "reason": "price lookup failed for a fill"})
                continue
            changes: dict = {}
            if abs(realized - (sp.realized_pnl or 0.0)) > 0.01:
                changes["realized"] = {"old": round(sp.realized_pnl or 0.0, 4), "new": round(realized, 4)}
                sp.realized_pnl = realized
            net_matches = abs(net - sp.net_qty_base) <= max(1e-6, abs(sp.net_qty_base) * 0.01)
            if net_matches and avg > 0 and abs(avg - (sp.avg_entry_price or 0.0)) > 0.01:
                changes["entry"] = {"old": round(sp.avg_entry_price or 0.0, 4), "new": round(avg, 4)}
                sp.avg_entry_price = avg
            if changes:
                sp.updated_at = now
                updated.append({**tag, **changes})
            elif not net_matches:
                skipped.append({**tag, "reason":
                                f"net {net:.4f} != ledger {sp.net_qty_base:.4f} (entry kept)"})
    log.info("backfill_pnl_from_klines: %d updated, %d skipped", len(updated), len(skipped))
    return {"updated": updated, "skipped": skipped}


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
