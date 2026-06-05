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
    """Reconstruct `avg_entry_price` for OPEN positions built from fills that weren't
    recorded with a price (`fill_price` 0/None — pre-execution-quality history), by
    replaying each strategy's own success fills and pricing any unpriced fill with the
    market close at that fill's minute (a market order fills ~there).

    Realized PnL is deliberately NEVER touched here. Replaying realized requires the
    COMPLETE closed history from a known-flat start, which pre-tracking data doesn't
    give us: a strategy whose first recorded fill is a *close* of a position opened
    before the database existed would have that close misread as opening the opposite
    side, fabricating profit (this happened to BTC_HLD: a long opened pre-DB, the
    recorded sell read as a short open → a bogus +$64). And clearing realized whenever
    *any* unpriced fill exists would wipe legitimately-booked realized that coexists
    with one old pre-capture fill. So realized is owned solely by the live executor;
    `audit_pnl` surfaces a caveated estimate for the operator to judge.

    `avg_entry_price` is safe to rebuild because it describes the CURRENT open position
    (priced by its own fills); we only rewrite it when the replayed net matches the
    stored net (the fills fully explain it), so a manual/drifted net keeps its entry.
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
            net = avg = 0.0
            ok = True
            for o in fills:
                px = o.fill_price if (o.fill_price and o.fill_price > 0) else oracle.get_kline_close(
                    osym, int(o.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000))
                if not px or not o.qty_base:
                    ok = False
                    break
                signed = o.qty_base * (1.0 if o.side == "buy" else -1.0)
                net, avg, _ = _fill_math(net, avg, signed, o.qty_base, px)
            if not ok:
                skipped.append({**tag, "reason": "price lookup failed for a fill"})
                continue
            # Realized PnL is owned by the live executor and is NEVER touched here:
            # reconstructing it from a truncated history fabricates profit (the BTC_HLD
            # case), and clearing on "any unpriced fill" would wipe legitimately-booked
            # realized that coexists with one old pre-capture fill. We only rebuild the
            # current open position's avg entry, and only when the fills explain the net.
            changes: dict = {}
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


def audit_pnl(router) -> dict:
    """Read-only reconciliation — catches PnL/position drift without changing anything.

    Two independent checks:
    1. Per strategy: replay its own fills (kline-reconstructed for unpriced ones) and
       compare the result to the stored ledger. Flags realized PnL that the ledger
       didn't compute (the unpriced-round-trip bug) and a net that the fills can't
       reproduce (manual / drifted).
    2. Per (exchange, symbol): compare the netted ledger to the LIVE exchange position.
       Flags manual trades or unrecorded fills.

    Returns {"strategy_issues", "exchange_drift", "clean"}; run it from the admin UI
    (or a monitor) so this whole class of bug surfaces the moment it happens instead
    of waiting to be eyeballed."""
    oracle = get_registry().get("bybit")
    registry = get_registry()
    strat_issues: list[dict] = []
    by_symbol: dict[tuple[str, str], float] = {}
    with session_scope() as db:
        for sp in db.query(StrategyPosition).all():
            by_symbol[(sp.exchange, sp.symbol)] = (
                by_symbol.get((sp.exchange, sp.symbol), 0.0) + sp.net_qty_base)
            tag = {"strategy_id": sp.strategy_id, "exchange": sp.exchange, "symbol": sp.symbol}
            osym = _oracle_symbol(sp.exchange, sp.symbol)
            fills = (db.query(Order).join(Alert, Order.alert_id == Alert.id)
                       .filter(Alert.strategy_id == sp.strategy_id,
                               Order.exchange == sp.exchange, Order.symbol == sp.symbol,
                               Order.status == "success")
                       .order_by(Order.created_at).all())
            if not fills:
                continue
            has_unpriced = any(not (o.fill_price and o.fill_price > 0) for o in fills)
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
                strat_issues.append({**tag, "issue": "a fill has no usable price"})
                continue
            r_drift = realized - (sp.realized_pnl or 0.0)
            n_drift = net - sp.net_qty_base
            # The replay assumes the strategy was flat before its first recorded fill and
            # that every fill is priced. A sell-first fill may be closing a position opened
            # before the DB existed, and unpriced fills are kline-estimated — so on a
            # suspect/unpriced history BOTH the realized and net replay are ESTIMATES, not
            # truth. Report them for visibility, but they must NOT flip the audit red (the
            # backfill can't "fix" them — realized is executor-owned). Only a fully-priced,
            # non-truncated replay yields actionable drift; exchange drift is always actionable.
            suspect = fills[0].side == "sell"
            replay_reliable = not has_unpriced and not suspect
            actionable = replay_reliable and (abs(r_drift) > 0.5 or abs(n_drift) > 1e-4)
            if abs(r_drift) > 0.5 or abs(n_drift) > 1e-4:
                strat_issues.append({
                    **tag,
                    "ledger_realized": round(sp.realized_pnl or 0.0, 4), "replay_realized": round(realized, 4),
                    "ledger_net": round(sp.net_qty_base, 6), "replay_net": round(net, 6),
                    "realized_drift": round(r_drift, 4), "net_drift": round(n_drift, 6),
                    "replay_suspect_truncated": suspect, "history_unpriced": has_unpriced,
                    "actionable": actionable})

    exchange_drift: list[dict] = []
    for (ex, sym), lnet in sorted(by_symbol.items()):
        if abs(lnet) < 1e-9:
            continue
        try:
            eqty, _ = registry.get(ex).get_position(sym)
        except Exception as e:                       # noqa: BLE001 — report, don't raise
            exchange_drift.append({"exchange": ex, "symbol": sym, "issue": f"read failed: {type(e).__name__}"})
            continue
        if abs(eqty - lnet) > 1e-4:
            exchange_drift.append({"exchange": ex, "symbol": sym, "ledger_net": round(lnet, 6),
                                   "exchange_net": round(eqty, 6), "drift": round(eqty - lnet, 6)})

    # Clean = nothing the operator can act on. Replay-based ESTIMATES on suspect/unpriced
    # histories are reported (for visibility) but don't make it dirty — only actionable
    # strategy drift (fully-priced) and exchange drift (verifiable) do.
    actionable = [s for s in strat_issues if s.get("actionable")]
    clean = not actionable and not exchange_drift
    log.info("audit_pnl: %d strategy issue(s) (%d actionable), %d exchange drift(s) — %s",
             len(strat_issues), len(actionable), len(exchange_drift), "clean" if clean else "ATTENTION")
    return {"strategy_issues": strat_issues, "exchange_drift": exchange_drift, "clean": clean}


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
