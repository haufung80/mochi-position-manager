"""Per-arb PnL attribution — the math, isolated from live exchanges.

The headline of a funding arb is **funding harvested − fees**; the two legs are
delta-neutral so their directional PnL nets to ≈0 (a neutrality health check, not
a profit source). This module computes that breakdown from ALREADY-FETCHED rows +
marks, so it is property/regression-tested without touching any exchange.

The split (plan §5 + §6):

- ``funding_total`` = Σ over the arb's **perp** legs of Σ ``ArbFundingEvent.amount``
  for ``(leg.exchange, leg.account, leg.symbol)`` within ``[opened_at, closed_at|
  now]``. **Spot legs contribute 0** (funding accrues only on the perp). Combo 2
  (cross-exchange perp-perp) sums BOTH perp legs (long-venue + short-venue fundings
  net to the harvested carry); combos 1/3 are the single short perp. ``amount`` is
  signed (+received / −paid).
- ``funding_by_leg`` keyed ``'exchange:account:symbol'`` — the per-venue split.
- ``commission_total`` = Σ leg commissions.
- ``spot_unrealized`` = Σ spot-leg cost-basis ``filled·(mark − avg_fill)`` (a long
  spot rises with the mark).
- ``perp_unrealized`` = Σ perp-leg venue unrealized (from ``get_position_detail`` /
  ``filled·(mark − avg_fill)`` with the leg's signed direction).
- ``directional_net`` = ``spot_unrealized + perp_unrealized`` — the legs' live MTM.
  ≈0 while delta-neutral (a long spot + a short perp move opposite, cancelling), so
  it is BOTH a neutrality health check AND a real economic term: a non-zero value is
  genuine directional exposure (e.g. a grid-dust skew or an un-converged basis).
- ``net`` = ``funding_total − commission_total + directional_net`` (+ basis) — the arb's
  total economic value (realized carry + the legs' unrealized MTM). The caller feeds a
  flat MTM (mark = avg_fill) for a CLOSED arb, so a closed pair's net is just its
  realized funding − fees (no phantom mark on a position that no longer exists).

The account-wide-settlement double-count is prevented UPSTREAM by the A.5 open-time
symbol exclusivity (one ``(exchange, account, symbol)`` is held by at most one
non-closed arb) plus the ``ArbFundingEvent`` UNIQUE — so summing the rows for a
leg's ``(exchange, account, symbol)`` attributes each settlement to exactly one arb.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def leg_key(exchange: str, account: str, symbol: str) -> str:
    """The canonical per-leg funding key: ``'exchange:account:symbol'``."""
    return f"{exchange}:{account}:{symbol}"


@dataclass
class LegPnLInput:
    """One leg's already-fetched inputs for the PnL math (no exchange access).

    ``side`` is ``buy``/``sell`` (the directional sign); ``filled``/``avg_fill`` are
    the recorded fill; ``mark`` is the live/last price; ``funding`` is the Σ of this
    leg's ``ArbFundingEvent.amount`` (0 for a spot leg). ``commission`` is the leg's
    recorded fee.
    """

    exchange: str
    account: str
    product: str          # spot | perp
    symbol: str
    side: str             # buy | sell
    filled: float = 0.0
    avg_fill: float = 0.0
    mark: float = 0.0
    funding: float = 0.0
    commission: float = 0.0

    @property
    def signed_qty(self) -> float:
        """Filled base, signed by side (long buy +, short sell −)."""
        return self.filled if self.side == "buy" else -self.filled

    @property
    def directional_unrealized(self) -> float:
        """Cost-basis unrealized for this leg: ``signed_qty·(mark − avg_fill)``.

        A long spot (buy) gains as the mark rises; a short perp (sell) gains as it
        falls. 0 until both a mark and an avg_fill are known (a leg that hasn't
        filled, or whose mark is missing, contributes nothing rather than a bogus
        ``qty·(0 − avg)``)."""
        if self.filled <= 0 or self.mark <= 0 or self.avg_fill <= 0:
            return 0.0
        return self.signed_qty * (self.mark - self.avg_fill)


@dataclass
class ArbPnLResult:
    funding_total: float = 0.0
    funding_by_leg: dict[str, float] = field(default_factory=dict)
    commission_total: float = 0.0
    spot_unrealized: float = 0.0
    perp_unrealized: float = 0.0
    directional_net: float = 0.0
    net: float = 0.0


def compute_arb_pnl(legs: list[LegPnLInput], *, basis: float = 0.0,
                    realized: float = 0.0) -> ArbPnLResult:
    """Pure PnL roll-up for one arb from its legs' already-fetched inputs.

    ``net = funding_total − commission_total + directional_net + realized + basis``.
    ``directional_net`` (the legs' UNREALIZED MTM from their ``mark``/``avg_fill``) is
    always in net — pass ``mark = avg_fill`` for a closed/flat leg so it contributes 0.
    ``realized`` is the directional P&L already BOOKED at close (``ArbLeg.realized_pnl``);
    a leg is never both (its MTM is suppressed once closed), so the two never double-count
    — open legs carry MTM, closed legs carry realized. ``basis`` is an optional extra
    carry term (default 0); the dashboard leaves it 0 (basis is its own informational
    line, not double-booked into net).
    """
    funding_by_leg: dict[str, float] = {}
    funding_total = 0.0
    commission_total = 0.0
    spot_unrealized = 0.0
    perp_unrealized = 0.0

    for lg in legs:
        # Spot legs contribute 0 funding by definition; carry whatever was passed
        # for a perp leg (its summed ArbFundingEvent.amount).
        fund = lg.funding if lg.product == "perp" else 0.0
        funding_by_leg[leg_key(lg.exchange, lg.account, lg.symbol)] = fund
        funding_total += fund
        commission_total += lg.commission or 0.0
        if lg.product == "spot":
            spot_unrealized += lg.directional_unrealized
        else:
            perp_unrealized += lg.directional_unrealized

    directional_net = spot_unrealized + perp_unrealized
    net = funding_total - commission_total + directional_net + realized + basis
    return ArbPnLResult(
        funding_total=funding_total,
        funding_by_leg=funding_by_leg,
        commission_total=commission_total,
        spot_unrealized=spot_unrealized,
        perp_unrealized=perp_unrealized,
        directional_net=directional_net,
        net=net,
    )
