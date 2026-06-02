"""Order placement and lifecycle management.

Each call to `execute_order` places (or retries) ONE order on ONE venue.
The webhook fan-out logic calls this once per enabled venue.

Quantity flows from TradingView (base-asset units, e.g. 0.001 BTC) →
webhook handler → here → exchange adapter. We do NOT convert USD↔base
anywhere in the path; the pine-script sizing logic owns that decision.

Responsibilities:
    - Drive the Order row through pending -> success / retrying / dead.
    - Mutate the Position ledger on successful fills only.
    - Emit notifier events.

TradingView only fires `buy` and `sell` — the middleware always places a
plain market order. On one-way-mode perps (Bybit/HL defaults), a sell
against an open long naturally closes it; a sell against a flat position
opens a short. The pine script is responsible for the action sequence.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .config import get_settings
from .exchanges.registry import get_registry
from .models import Alert, Order, Position, StrategyPosition
from .notifier import get_notifier
from .routing import VenueRoute
from .schemas import OrderResult

log = logging.getLogger(__name__)

# Leverage applied to every order: each adapter sets it on the symbol
# (Bybit set_leverage / HL update_leverage) right before placing the order.
# Takes effect per symbol on its NEXT order — open positions keep their
# current leverage until then.
DEFAULT_LEVERAGE: float = 2.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _next_retry_delay(attempts: int) -> int:
    s = get_settings()
    delay = s.retry_base_delay_sec * (3 ** max(0, attempts - 1))
    return min(delay, s.retry_max_delay_sec)


def _bump_position(db: Session, model, keys: dict,
                   signed_delta: float, price: float) -> None:
    """Atomic insert-or-increment of a ledger row.

    Uses a single SQLite UPSERT so concurrent fills on the same row (e.g.
    several strategies trading the same symbol at the same bar) can't lose each
    other's increment — the read-modify-write would otherwise race.
    """
    now = _utcnow()
    col = model.__table__.c
    stmt = (
        sqlite_insert(model)
        .values(**keys, net_qty_base=signed_delta, net_qty_usd=signed_delta * price,
                last_price=price, updated_at=now)
        .on_conflict_do_update(
            index_elements=list(keys),
            set_={
                "net_qty_base": col.net_qty_base + signed_delta,
                "net_qty_usd": (col.net_qty_base + signed_delta) * price,
                "last_price": price,
                "updated_at": now,
            },
        )
    )
    db.execute(stmt)


def _apply_fill_to_position(db: Session, strategy_id: str, exchange: str, symbol: str,
                            side: str, qty_base: float, price: float) -> None:
    """Update BOTH ledgers: the per-(exchange,symbol) total and the
    per-(strategy,exchange,symbol) breakdown."""
    signed = qty_base if side == "buy" else -qty_base
    _bump_position(db, Position, {"exchange": exchange, "symbol": symbol}, signed, price)
    _bump_position(db, StrategyPosition,
                   {"strategy_id": strategy_id, "exchange": exchange, "symbol": symbol},
                   signed, price)


def _new_order(alert_id: int, venue: VenueRoute, side: str,
               quantity: float, signal_price: float | None = None) -> Order:
    """Create a pending Order row. qty_base is the source of truth (from TV);
    qty_usd is left at 0 and filled in once we know the fill price.
    signal_price is the alert's reference price ({{close}}), carried here so the
    fill's slippage can be measured per order without a join."""
    return Order(
        alert_id=alert_id,
        exchange=venue.exchange,
        symbol=venue.symbol,
        side=side,
        qty_base=quantity,
        qty_usd=0.0,
        reduce_only=False,
        leverage=DEFAULT_LEVERAGE,
        signal_price=signal_price,
        status="pending",
        attempts=0,
    )


def _call_exchange(venue: VenueRoute, side: str, quantity: float) -> OrderResult:
    return get_registry().get(venue.exchange).market_order(
        symbol=venue.symbol,
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        leverage=DEFAULT_LEVERAGE,
    )


def _on_success(db: Session, order: Order, alert: Alert, venue: VenueRoute,
                side: str, result: OrderResult) -> None:
    filled_qty = result.filled_qty_base or order.qty_base
    order.status = "success"
    order.exchange_order_id = result.exchange_order_id
    order.qty_base = filled_qty
    order.qty_usd = filled_qty * result.avg_price  # derived for dashboard
    order.fill_price = result.avg_price or None    # actual VWAP fill
    order.commission = result.commission or 0.0
    order.commission_asset = result.commission_asset or ""
    order.error_message = ""
    order.next_retry_at = None
    if result.avg_price > 0 and filled_qty > 0:
        _apply_fill_to_position(
            db, alert.strategy_id, venue.exchange, venue.symbol,
            side, filled_qty, result.avg_price,
        )
    get_notifier().order_succeeded(
        alert.strategy_id, venue.exchange, venue.symbol,
        side, filled_qty, result.avg_price,
    )
    log.info("Order success alert=%s strategy=%s ex=%s sym=%s qty=%s @ $%.2f",
             alert.id, alert.strategy_id, venue.exchange, venue.symbol,
             filled_qty, result.avg_price)


def _on_failure(order: Order, alert: Alert, venue: VenueRoute,
                side: str, result: OrderResult) -> None:
    order.error_message = result.error_message
    settings = get_settings()
    notifier = get_notifier()

    if order.attempts >= settings.retry_max_attempts:
        order.status = "dead"
        order.next_retry_at = None
        notifier.order_dead(
            alert.strategy_id, venue.exchange, venue.symbol,
            side, order.qty_base, order.attempts, result.error_message,
        )
        log.error("Order DEAD alert=%s strategy=%s ex=%s err=%s",
                  alert.id, alert.strategy_id, venue.exchange, result.error_message)
        return

    order.status = "retrying"
    delay = _next_retry_delay(order.attempts)
    order.next_retry_at = _utcnow() + timedelta(seconds=delay)
    notifier.order_failed(
        alert.strategy_id, venue.exchange, venue.symbol,
        side, order.qty_base, order.attempts, result.error_message,
    )
    log.warning("Order retrying in %ss alert=%s ex=%s err=%s",
                delay, alert.id, venue.exchange, result.error_message)


def execute_order(db: Session, alert: Alert, venue: VenueRoute, *,
                  quantity: float,
                  existing_order: Order | None = None) -> Order:
    """Place (or retry) a market order on a single venue.

    Args:
        db: SQLAlchemy session.
        alert: the Alert row this order is acting on. Action is 'buy' or 'sell'.
        venue: resolved per-exchange route.
        quantity: order size in BASE-ASSET units (e.g. 0.001 = 0.001 BTC).
        existing_order: pass when retrying an already-persisted Order.
    """
    side = alert.action.lower()  # "buy" | "sell"

    order = existing_order or _new_order(alert.id, venue, side, quantity,
                                         getattr(alert, "signal_price", None))
    if existing_order is None:
        db.add(order)
        db.flush()

    order.attempts += 1
    order.updated_at = _utcnow()
    db.flush()

    result = _call_exchange(venue, side, quantity)
    if result.success:
        _on_success(db, order, alert, venue, side, result)
    else:
        _on_failure(order, alert, venue, side, result)
    return order
