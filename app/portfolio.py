"""Portfolio manager — decides what to submit for a MANAGED (sar=false) strategy.

Given a strategy, a venue, and the alert's action, it returns a `Sizing` decision
(OPEN / CLOSE / REJECT / ALERT_DRIVEN). It reads the latest price + step size from
the exchange and the strategy's net position from the ledger, but it never places
orders or mutates state — that stays in `executor.execute_order`. This keeps the
sizing logic pure and unit-testable.

Model (per venue), for sar=false:
  * flat (net == 0):
      - position_size set   -> OPEN at floor(position_size / price / step) * step
                               (USDT NOTIONAL cap, rounded DOWN)
      - position_size unset -> PAPER: OPEN one min-unit (step) + warning
      - rounds to 0 / no price -> REJECT
  * same direction (long+buy / short+sell) -> REJECT (no pyramiding)
  * opposite direction (long+sell / short+buy) -> CLOSE to flat (qty = abs(net))

sar=true strategies bypass all of this: ALERT_DRIVEN with the alert's quantity.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Literal

from sqlalchemy.orm import Session

from .exchanges.registry import get_registry
from .models import StrategyPosition
from .routing import StrategyRoute, VenueRoute

log = logging.getLogger(__name__)

Side = Literal["buy", "sell"]
_FLAT_EPS = 1e-9  # treat |net| below this as flat (dust from float fills)


class Decision(str, Enum):
    ALERT_DRIVEN = "alert_driven"   # sar=true: submit the alert's quantity unchanged
    OPEN = "open"                   # flat -> open a managed-size position
    CLOSE = "close"                 # in a position, opposite signal -> close to flat
    REJECT = "reject"               # double-down guard / unsized / price unavailable


@dataclass(frozen=True)
class Sizing:
    decision: Decision
    side: Side | None = None
    qty: float | None = None
    paper: bool = False    # OPEN with one min-unit because no position_size was set
    reason: str = ""       # for REJECT / PAPER — surfaced to Telegram + audit row


def compute_managed_qty(usdt: float, price: float, step: float) -> float:
    """Largest multiple of `step` whose notional (qty * price) <= `usdt`.
    Rounds DOWN; returns 0.0 if the budget doesn't cover a single step. Uses
    Decimal so the step-grid arithmetic is exact (float division mis-floors —
    e.g. 2000 / 0.1 == 19999.999...)."""
    if usdt <= 0 or price <= 0 or step <= 0:
        return 0.0
    affordable = Decimal(str(usdt)) / Decimal(str(price))   # base units we can buy
    step_d = Decimal(str(step))
    units = (affordable / step_d).to_integral_value(rounding=ROUND_DOWN)
    if units <= 0:
        return 0.0
    return float(units * step_d)


def _net_qty(db: Session, strategy_id: str, exchange: str, symbol: str) -> float:
    row = (db.query(StrategyPosition)
             .filter_by(strategy_id=strategy_id, exchange=exchange, symbol=symbol)
             .one_or_none())
    return row.net_qty_base if row else 0.0


def decide(db: Session, route: StrategyRoute, venue: VenueRoute,
           action: Side, *, alert_quantity: float | None) -> Sizing:
    """Compute the order intent for ONE venue. See module docstring for the model."""
    if route.sar:
        return Sizing(Decision.ALERT_DRIVEN, side=action, qty=alert_quantity)

    net = _net_qty(db, route.strategy_id, venue.exchange, venue.symbol)
    is_long = net > _FLAT_EPS
    is_short = net < -_FLAT_EPS

    if is_long or is_short:
        same_direction = (is_long and action == "buy") or (is_short and action == "sell")
        if same_direction:
            return Sizing(Decision.REJECT,
                          reason=f"double-down blocked: net {net:+g} vs {action}")
        # opposite signal -> close the strategy's position to flat
        return Sizing(Decision.CLOSE, side=action, qty=abs(net))

    # flat -> open a new position
    ex = get_registry().get(venue.exchange)
    step = ex.get_step_size(venue.symbol)
    if step <= 0:
        return Sizing(Decision.REJECT, reason="step size unavailable")

    if route.position_size is None:
        # paper mode: one minimum unit, with a warning
        return Sizing(Decision.OPEN, side=action, qty=step, paper=True,
                      reason="no position_size configured (paper mode)")

    price = ex.get_price(venue.symbol)
    if price <= 0:
        return Sizing(Decision.REJECT, reason="price unavailable for sizing")
    qty = compute_managed_qty(route.position_size, price, step)
    if qty <= 0:
        return Sizing(
            Decision.REJECT,
            reason=f"position_size {route.position_size:g} below one step "
                   f"({step:g}) at price {price:g}",
        )
    return Sizing(Decision.OPEN, side=action, qty=qty)
