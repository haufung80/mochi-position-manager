"""Order placement and lifecycle management.

Each call to `execute_order` places (or retries) ONE order on ONE venue.
The webhook fan-out logic calls this once per enabled venue.

Responsibilities:
    - Translate (alert + venue + qty) into a concrete exchange call.
    - Drive the Order row through pending -> success / retrying / dead.
    - Mutate the Position ledger on successful fills only.
    - Emit notifier events for visibility / Telegram alerts.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import get_settings
from .exchanges.registry import get_registry
from .models import Alert, Order, Position
from .notifier import get_notifier
from .routing import VenueRoute
from .schemas import OrderResult

log = logging.getLogger(__name__)

# Middleware does not configure leverage — the exchange's account-level
# margin mode applies. Adapters still accept this parameter for API
# compatibility with their underlying SDKs.
DEFAULT_LEVERAGE: float = 1.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _side_from_action(action: str) -> tuple[str, bool, bool]:
    """Map TV action string -> (side, is_close, reduce_only).

    For close actions, `side` is the side of the closing trade (e.g.
    `close_long` -> sell). `side=""` means "let the close_position()
    adapter figure it out" (used for the generic `close` action).
    """
    a = action.lower()
    match a:
        case "buy":          return "buy",  False, False
        case "sell":         return "sell", False, False
        case "close":        return "",     True,  True
        case "close_long":   return "sell", True,  True
        case "close_short":  return "buy",  True,  True
    raise ValueError(f"unknown action: {action}")


def _next_retry_delay(attempts: int) -> int:
    s = get_settings()
    delay = s.retry_base_delay_sec * (3 ** max(0, attempts - 1))
    return min(delay, s.retry_max_delay_sec)


def _apply_fill_to_position(db: Session, exchange: str, symbol: str,
                            side: str, qty_base: float, price: float,
                            is_close: bool) -> None:
    """Update the (exchange, symbol) position row to reflect this fill."""
    pos = (
        db.query(Position)
        .filter(Position.exchange == exchange, Position.symbol == symbol)
        .one_or_none()
    )
    if pos is None:
        pos = Position(exchange=exchange, symbol=symbol)
        db.add(pos)
        db.flush()

    if is_close:
        pos.net_qty_base = 0.0
        pos.net_qty_usd = 0.0
    else:
        signed = qty_base if side == "buy" else -qty_base
        pos.net_qty_base += signed
        pos.net_qty_usd = pos.net_qty_base * price
    pos.last_price = price
    pos.updated_at = _utcnow()


def _new_order(alert_id: int, venue: VenueRoute, side: str,
               qty_usd: float, reduce_only: bool) -> Order:
    return Order(
        alert_id=alert_id,
        exchange=venue.exchange,
        symbol=venue.symbol,
        side=side or "buy",  # close actions store a placeholder
        qty_usd=qty_usd,
        reduce_only=reduce_only,
        leverage=DEFAULT_LEVERAGE,
        status="pending",
        attempts=0,
    )


def _call_exchange(venue: VenueRoute, side: str, qty_usd: float,
                   is_close: bool, reduce_only: bool) -> OrderResult:
    exchange = get_registry().get(venue.exchange)
    if is_close:
        return exchange.close_position(venue.symbol)
    return exchange.market_order(
        symbol=venue.symbol,
        side=side,  # type: ignore[arg-type]
        qty_usd=qty_usd,
        leverage=DEFAULT_LEVERAGE,
        reduce_only=reduce_only,
    )


def _on_success(db: Session, order: Order, alert: Alert, venue: VenueRoute,
                side: str, is_close: bool, result: OrderResult) -> None:
    order.status = "success"
    order.exchange_order_id = result.exchange_order_id
    order.qty_base = result.filled_qty_base
    order.error_message = ""
    order.next_retry_at = None
    if is_close or (result.avg_price > 0 and result.filled_qty_base > 0):
        _apply_fill_to_position(
            db, venue.exchange, venue.symbol,
            side or "buy", result.filled_qty_base, result.avg_price, is_close,
        )
    get_notifier().order_succeeded(
        alert.strategy_id, venue.exchange, venue.symbol,
        side or "close", order.qty_usd, result.avg_price,
    )
    log.info("Order success alert=%s strategy=%s ex=%s sym=%s qty=$%.2f",
             alert.id, alert.strategy_id, venue.exchange, venue.symbol, order.qty_usd)


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
            side or "close", order.qty_usd, order.attempts, result.error_message,
        )
        log.error("Order DEAD alert=%s strategy=%s ex=%s err=%s",
                  alert.id, alert.strategy_id, venue.exchange, result.error_message)
        return

    order.status = "retrying"
    delay = _next_retry_delay(order.attempts)
    order.next_retry_at = _utcnow() + timedelta(seconds=delay)
    notifier.order_failed(
        alert.strategy_id, venue.exchange, venue.symbol,
        side or "close", order.qty_usd, order.attempts, result.error_message,
    )
    log.warning("Order retrying in %ss alert=%s ex=%s err=%s",
                delay, alert.id, venue.exchange, result.error_message)


def execute_order(db: Session, alert: Alert, venue: VenueRoute, *,
                  quantity_usd: float,
                  existing_order: Order | None = None) -> Order:
    """Place (or retry) an order on a single venue.

    Args:
        db: SQLAlchemy session.
        alert: the Alert row this order is acting on.
        venue: resolved per-exchange route.
        quantity_usd: notional size for THIS order (from TV alert payload).
            For close actions this is informational only — the exchange
            closes the full position.
        existing_order: pass when retrying an already-persisted Order; the
            function increments its attempt counter rather than inserting
            a new row.
    """
    side, is_close, reduce_only = _side_from_action(alert.action)

    order = existing_order or _new_order(
        alert.id, venue, side, quantity_usd, reduce_only,
    )
    if existing_order is None:
        db.add(order)
        db.flush()

    order.attempts += 1
    order.updated_at = _utcnow()
    db.flush()

    result = _call_exchange(venue, side, quantity_usd, is_close, reduce_only)
    if result.success:
        _on_success(db, order, alert, venue, side, is_close, result)
    else:
        _on_failure(order, alert, venue, side, result)
    return order
