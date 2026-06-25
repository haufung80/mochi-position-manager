"""Global pre-trade risk controls — the singleton `RiskSettings` row (id=1), read on
every directional order in `portfolio.decide` (and the arb open) and set from the admin
page. Kept tiny and DB-backed so the hot path reads it with the session it already holds."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import RiskSettings

_SINGLETON_ID = 1


def get_risk_settings(db: Session) -> RiskSettings:
    """The singleton risk row — created with defaults (per-order cap $500, kill-switch
    off) on first use so callers never have to handle a missing row."""
    rs = db.get(RiskSettings, _SINGLETON_ID)
    if rs is None:
        rs = RiskSettings(id=_SINGLETON_ID)
        db.add(rs)
        db.flush()
    return rs


def update_risk_settings(db: Session, *, per_order_max_notional: float | None = None,
                         kill_switch: bool | None = None) -> RiskSettings:
    """Patch the risk row (only the passed fields). Returns the updated singleton."""
    rs = get_risk_settings(db)
    if per_order_max_notional is not None:
        rs.per_order_max_notional = per_order_max_notional
    if kill_switch is not None:
        rs.kill_switch = kill_switch
    db.flush()
    return rs
