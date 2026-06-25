"""Per-leg funding attribution for the arb book — the SINGLE source of arb funding,
shared by the /funding-arb report and the close-alert net so the two can never compute
funding differently. Pure DB read (no exchange access, no writes)."""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import func

from .models import ArbFundingEvent, ArbLeg, ArbPosition


def leg_funding(db, arb: ArbPosition, leg: ArbLeg) -> float:
    """Σ ``ArbFundingEvent.amount`` attributed to one PERP leg, over the arb's window
    ``[opened_at, closed_at|now]`` and the leg's ``(exchange, account, symbol)``. Spot
    legs return 0 (funding accrues only on the perp).

    The A.5 open-time symbol exclusivity (one ``(exchange, account, symbol)`` held by at
    most one non-closed arb) is what makes this single-arb attribution exact: two
    concurrent BTC arbs can't both claim one account-wide settlement, because they can't
    both hold the BTC perp symbol on the same account at once.
    """
    if leg.product != "perp":
        return 0.0
    lo = arb.opened_at
    hi = arb.closed_at or datetime.now(timezone.utc)
    q = (
        db.query(func.coalesce(func.sum(ArbFundingEvent.amount), 0.0))
        .filter(ArbFundingEvent.exchange == leg.exchange,
                ArbFundingEvent.account == leg.account,
                ArbFundingEvent.symbol == leg.symbol)
    )
    # Bound the window to the arb's lifetime so a settlement from a PRIOR closed arb on
    # the same (re-used) symbol can't leak into this one. `opened_at` is None only before
    # the open finalizes (no funding yet), so an unbounded `lo` is harmless then.
    if lo is not None:
        q = q.filter(ArbFundingEvent.funding_time >= lo)
    q = q.filter(ArbFundingEvent.funding_time <= hi)
    return float(q.scalar() or 0.0)
