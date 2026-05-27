"""Glue between an Alert row and an exchange order.

Each call to execute_order() places a SINGLE order on a SINGLE venue.
The webhook handler fans out one alert across N enabled venues by calling
this function once per venue.

Responsibilities:
  - Translate (alert + venue) into a concrete exchange call.
  - Update the Order row through its lifecycle: pending -> success / retrying / dead.
  - Mutate the Position ledger on successful fills only.
  - Notify on terminal states.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Alert, Order, Position
from .routing import VenueRoute
from .exchanges.registry import get_registry
from .notifier import get_notifier
from .schemas import OrderResult

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _side_from_action(action: str) -> tuple[str, bool, bool]:
    """Return (side, is_close, reduce_only)."""
    a = action.lower()
    if a == "buy":
        return "buy", False, False
    if a == "sell":
        return "sell", False, False
    if a == "close":
        return "", True, True
    if a == "close_long":
        return "sell", True, True
    if a == "close_short":
        return "buy", True, True
    raise ValueError(f"Unknown action: {action}")


def _next_retry_delay(attempts: int) -> int:
    s = get_settings()
    delay = s.retry_base_delay_sec * (3 ** max(0, attempts - 1))
    return min(delay, s.retry_max_delay_sec)


def _apply_fill_to_position(db: Session, exchange: str, symbol: str,
                            side: str, qty_base: float, price: float,
                            is_close: bool) -> None:
    pos = (
        db.query(Position)
        .filter(Position.exchange == exchange, Position.symbol == symbol)
        .one_or_none()
    )
    if pos is None:
        pos = Position(exchange=exchange, symbol=symbol, net_qty_base=0.0,
                       net_qty_usd=0.0, last_price=price)
        db.add(pos)
        db.flush()

    signed = qty_base if side == "buy" else -qty_base
    if is_close:
        pos.net_qty_base = 0.0
        pos.net_qty_usd = 0.0
    else:
        pos.net_qty_base += signed
        pos.net_qty_usd = pos.net_qty_base * price
    pos.last_price = price
    pos.updated_at = _utcnow()


def execute_order(db: Session, alert: Alert, venue: VenueRoute,
                  *, existing_order: Order | None = None) -> Order:
    """Place (or retry) an order for this alert on a single venue.

    Mutates DB state and emits notifications. Returns the Order row.
    """
    side, is_close, reduce_only = _side_from_action(alert.action)

    if existing_order is not None:
        order = existing_order
    else:
        order = Order(
            alert_id=alert.id,
            exchange=venue.exchange,
            symbol=venue.symbol,
            side=side or "buy",
            qty_usd=venue.quantity_usd,
            reduce_only=reduce_only,
            leverage=venue.leverage,  # hardcoded 1.0 — see VenueRoute
            status="pending",
            attempts=0,
        )
        db.add(order)
        db.flush()

    order.attempts += 1
    order.updated_at = _utcnow()
    db.flush()

    notifier = get_notifier()
    exchange = get_registry().get(venue.exchange)

    if is_close:
        result: OrderResult = exchange.close_position(venue.symbol)
    else:
        result = exchange.market_order(
            symbol=venue.symbol,
            side=side,  # type: ignore[arg-type]
            qty_usd=venue.quantity_usd,
            leverage=venue.leverage,
            reduce_only=reduce_only,
        )

    if result.success:
        order.status = "success"
        order.exchange_order_id = result.exchange_order_id
        order.qty_base = result.filled_qty_base
        order.error_message = ""
        order.next_retry_at = None
        if is_close or (result.avg_price > 0 and result.filled_qty_base > 0):
            _apply_fill_to_position(
                db, venue.exchange, venue.symbol,
                side or "buy", result.filled_qty_base, result.avg_price, is_close
            )
        notifier.order_succeeded(
            alert.strategy_id, venue.exchange, venue.symbol,
            side or "close", venue.quantity_usd, result.avg_price,
        )
        log.info("Order success alert=%s strategy=%s ex=%s sym=%s",
                 alert.id, alert.strategy_id, venue.exchange, venue.symbol)
        return order

    # ---- failure path ----
    order.error_message = result.error_message
    settings = get_settings()

    if order.attempts >= settings.retry_max_attempts:
        order.status = "dead"
        order.next_retry_at = None
        notifier.order_dead(
            alert.strategy_id, venue.exchange, venue.symbol,
            side or "close", venue.quantity_usd, order.attempts, result.error_message,
        )
        log.error("Order DEAD alert=%s strategy=%s ex=%s err=%s",
                  alert.id, alert.strategy_id, venue.exchange, result.error_message)
    else:
        order.status = "retrying"
        delay = _next_retry_delay(order.attempts)
        order.next_retry_at = _utcnow() + timedelta(seconds=delay)
        notifier.order_failed(
            alert.strategy_id, venue.exchange, venue.symbol,
            side or "close", venue.quantity_usd, order.attempts, result.error_message,
        )
        log.warning("Order retrying in %ss alert=%s ex=%s err=%s",
                    delay, alert.id, venue.exchange, result.error_message)

    return order
