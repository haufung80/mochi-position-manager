"""Pydantic schemas for inbound webhook payloads and outbound exchange results."""
from __future__ import annotations
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


Action = Literal["buy", "sell", "close", "close_long", "close_short"]


class TradingViewAlert(BaseModel):
    """JSON body that TradingView POSTs to /webhook/tradingview.

    Paste this into the TradingView alert's *Message* field (replace the secret):

        {
          "secret": "<paste-WEBHOOK_SECRET-here>",
          "strategy_id": "MR_VOTING_BTC_6H",
          "action": "{{strategy.order.action}}",
          "quantity": {{strategy.order.contracts}},
          "alert_id": "{{strategy.order.id}}-{{timenow}}"
        }

    `quantity` is the order size in **base-asset units** (e.g. 0.001 = 0.001 BTC),
    as computed by your pine-script sizing logic and surfaced via
    `{{strategy.order.contracts}}`. Required for `buy` and `sell`; ignored on
    close actions (which exit the whole position).

    Note: this is NOT a USD amount. If your pine script sizes orders in dollars,
    convert to base in pine before sending (e.g. `qty := cash_size / close`).
    """
    secret: str
    strategy_id: str = Field(..., min_length=1, max_length=128)
    action: Action
    quantity: Optional[float] = Field(default=None, gt=0)
    alert_id: Optional[str] = None
    bar_time: Optional[str] = None
    price: Optional[float] = None

    @field_validator("strategy_id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _qty_required_for_entries(self) -> "TradingViewAlert":
        if self.action in ("buy", "sell"):
            if self.quantity is None or self.quantity <= 0:
                raise ValueError(
                    "quantity > 0 is required for buy/sell actions "
                    "(use {{strategy.order.contracts}} in your TV alert — "
                    "it's the order size in BASE-ASSET units, not USD)"
                )
        return self


class OrderResult(BaseModel):
    """Normalized result returned by every exchange adapter."""
    success: bool
    exchange_order_id: str = ""
    filled_qty_base: float = 0.0
    avg_price: float = 0.0
    error_message: str = ""
    raw: dict | None = None
