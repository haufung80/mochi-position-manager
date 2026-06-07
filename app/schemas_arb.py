"""Pydantic v2 schemas for the `/funding-arb/*` API surface.

These models are the **contract** for the funding-arbitrage execution API: the
signal app (`mochi-funding-signal`) POSTs "open this arb / close arb N" and polls
a status endpoint; this app fires the legs, tracks the pair as one unit, and
reports funding-minus-fees PnL. Phase 0 ships only these schemas + thin typed
stub handlers so an OpenAPI artifact can be reviewed before any executor/model/
adapter code is written.

Design notes that live in the contract (not just the code):

- **Hyperliquid spot is first-class.** `(exchange=hyperliquid, product=spot)` is a
  VALID leg. There is deliberately no `{hyperliquid, spot}` rejection here — the
  HL spot *adapter* is a later phase, but the contract accepts it now.
- **Default combo = single-venue Hyperliquid cash-and-carry.** When `legs` is
  omitted the arb defaults to long HL spot + short HL perp on the same dedicated
  HL ``arb`` account (perp at 1x). Other combos (Bybit cash-and-carry,
  cross-exchange perp-perp, Bybit-spot + HL-perp) stay expressible via explicit
  ``legs[]``.
- **size_mode.** ``"notional"`` (default) sizes each leg from ``notional`` (USD,
  ``> 0`` required). ``"min"`` is "paper" mode: ignore ``notional`` and size each
  leg to the exchange's MINIMUM order size (real but tiny orders); ``notional``
  becomes optional. Phase 0 only defines the field + semantics — no sizing code.
- A non-default ``legs[]`` must describe a delta-neutral pair: exactly one ``buy``
  and one ``sell`` leg with an equal ``target_qty`` (when targets are given).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Exchange = Literal["bybit", "hyperliquid"]
Product = Literal["spot", "perp"]
Side = Literal["buy", "sell"]
Asset = Literal["BTC", "ETH", "SOL"]
SizeMode = Literal["notional", "min"]


# --- Leg specification (request side) --------------------------------------

class LegSpec(BaseModel):
    """One leg of an arbitrage pair, fully self-describing.

    Identity lives on the leg — ``(exchange, account, product, side)`` — which is
    what lets a single schema express every combo (single-venue HL/Bybit
    cash-and-carry, cross-exchange perp-perp, spot + cross-perp) and any future
    N-leg arb. ``(exchange=hyperliquid, product=spot)`` is VALID.
    """

    exchange: Exchange
    account: str = Field(
        default="arb",
        description="Credential bucket / sub-account label. Defaults to the "
        "dedicated 'arb' account so arb fills never net against the "
        "directional book.",
    )
    product: Product
    side: Side
    target_qty: Optional[float] = Field(
        default=None,
        gt=0,
        description="Target base-asset quantity for this leg. Optional: when "
        "omitted the leg is sized at open time from `notional`/`size_mode`. "
        "When provided on both legs they must be equal (delta-neutral).",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"exchange": "hyperliquid", "account": "arb",
                 "product": "spot", "side": "buy"},
                {"exchange": "hyperliquid", "account": "arb",
                 "product": "perp", "side": "sell"},
            ]
        }
    }


# --- Open request / response ------------------------------------------------

# The default combo when `legs` is omitted: single-venue Hyperliquid
# cash-and-carry — long HL spot + short HL perp on the same dedicated `arb`
# account, perp at 1x. Surfaced as a request example and reused by the stub
# handler so the example and the documented default never drift.
DEFAULT_COMBO_EXAMPLE: dict = {
    "idempotency_key": "sig-2026-06-07T00:00:00Z-BTC",
    "asset": "BTC",
    "notional": 1000.0,
    "size_mode": "notional",
    "strategy_tag": "hl-cash-and-carry",
}

# An explicit Bybit cash-and-carry combo (spot buy + perp sell on Bybit `arb`).
BYBIT_COMBO_EXAMPLE: dict = {
    "idempotency_key": "sig-2026-06-07T00:00:00Z-ETH",
    "asset": "ETH",
    "notional": 2500.0,
    "size_mode": "notional",
    "strategy_tag": "bybit-cash-and-carry",
    "legs": [
        {"exchange": "bybit", "account": "arb", "product": "spot", "side": "buy"},
        {"exchange": "bybit", "account": "arb", "product": "perp", "side": "sell"},
    ],
}


class ArbOpenRequest(BaseModel):
    """Open one delta-neutral funding-arb position.

    ``legs`` omitted ⇒ the **default single-venue Hyperliquid cash-and-carry**
    combo (long HL spot + short HL perp, same dedicated HL ``arb`` account, perp
    at 1x). Provide ``legs[]`` to select another combo. ``idempotency_key`` is the
    dedup gate (a repeat returns ``status="duplicate"``), mirroring the webhook
    path.
    """

    idempotency_key: str = Field(
        ..., min_length=1, max_length=200,
        description="Caller-supplied dedup key; a repeat open returns 'duplicate'.",
    )
    asset: Asset
    notional: Optional[float] = Field(
        default=None,
        gt=0,
        description="USD notional per leg. Required (> 0) when "
        "size_mode='notional'; ignored when size_mode='min'.",
    )
    size_mode: SizeMode = Field(
        default="notional",
        description="'notional' sizes each leg from `notional`; 'min' (paper "
        "mode) ignores `notional` and sizes each leg to the exchange minimum "
        "order size (real but tiny orders).",
    )
    strategy_tag: Optional[str] = Field(default=None, max_length=128)
    legs: Optional[list[LegSpec]] = Field(
        default=None,
        description="Explicit legs. Omit for the default single-venue "
        "Hyperliquid cash-and-carry combo.",
    )

    @model_validator(mode="after")
    def _check_sizing_and_shape(self) -> "ArbOpenRequest":
        # size_mode / notional rule.
        if self.size_mode == "notional" and (self.notional is None or self.notional <= 0):
            raise ValueError("notional must be > 0 when size_mode='notional'")

        # When legs are explicit, enforce the delta-neutral pair shape.
        if self.legs is not None:
            if len(self.legs) != 2:
                raise ValueError("legs must contain exactly two legs (one buy + one sell)")
            sides = sorted(leg.side for leg in self.legs)
            if sides != ["buy", "sell"]:
                raise ValueError("legs must be exactly one 'buy' and one 'sell' (delta-neutral)")
            targets = [leg.target_qty for leg in self.legs if leg.target_qty is not None]
            if len(targets) == 2 and targets[0] != targets[1]:
                raise ValueError("both legs' target_qty must be equal (delta-neutral)")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [DEFAULT_COMBO_EXAMPLE, BYBIT_COMBO_EXAMPLE],
        }
    }


class SizedLeg(BaseModel):
    """A leg after the open endpoint resolved the combo and sized it."""

    exchange: Exchange
    account: str = "arb"
    product: Product
    symbol: str = Field(..., description="Exchange-native symbol (e.g. BTC, BTCUSDT).")
    side: Side
    target_qty: float = Field(..., description="Resolved base-asset quantity for the leg.")


class ArbOpenResponse(BaseModel):
    status: Literal["accepted", "duplicate"]
    arb_id: int
    idempotency_key: str
    legs: list[SizedLeg]


# --- Close request / response ----------------------------------------------

class ArbCloseRequest(BaseModel):
    arb_id: int = Field(..., gt=0)


class ArbCloseResponse(BaseModel):
    status: Literal["closing", "already_closed"]
    arb_id: int


# --- Status / reporting views ----------------------------------------------

class ArbLegView(BaseModel):
    """Per-leg status for the status endpoint."""

    exchange: Exchange
    account: str
    product: Product
    symbol: str
    side: Side
    target_qty: float
    filled_qty: float = Field(
        0.0,
        description="NET base quantity received (spot buys are net of base-coin "
        "fees) — the actually-held, hedgeable quantity.",
    )
    avg_fill: float = 0.0
    funding: Optional[float] = Field(
        default=None,
        description="Funding harvested by this leg (spot legs contribute 0; "
        "None until first settlement).",
    )
    status: Literal["pending", "success", "retrying", "dead", "error"] = Field(
        ..., description="pending | success | retrying | dead | error"
    )


class ArbPnL(BaseModel):
    """PnL breakdown for one arb. Funding is the point; fees and basis net it."""

    funding_total: float
    funding_by_leg: dict[str, float] = Field(
        default_factory=dict,
        description="Per-leg funding keyed by 'exchange:account:symbol'.",
    )
    commission_total: float
    spot_unrealized: float
    perp_unrealized: float
    directional_net: float = Field(
        ...,
        description="Spot + perp directional PnL; ≈0 is the neutrality health check.",
    )
    net: float = Field(..., description="funding_total − commission_total (+ basis).")


class ArbPositionView(BaseModel):
    """Full status of one arb position (the status endpoint payload)."""

    arb_id: int
    asset: Asset
    status: Literal["opening", "open", "closing", "closed", "error"]
    neutral: bool = Field(..., description="True when both legs are filled and balanced.")
    neutrality_skew: float = Field(
        ...,
        description="long_leg.filled − short_leg.filled (base, net of fees); 0 = balanced.",
    )
    legs: list[ArbLegView]
    pnl: ArbPnL
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    error_message: Optional[str] = None
