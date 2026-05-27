"""Pydantic schemas for inbound webhook payloads and outbound exchange results."""
from __future__ import annotations
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


Action = Literal["buy", "sell", "close", "close_long", "close_short"]


class TradingViewAlert(BaseModel):
    """JSON body that TradingView POSTs to /webhook/tradingview.

    Recommended alert message body (paste into TradingView -> Notifications):

        {
          "secret": "<paste-WEBHOOK_SECRET-here>",
          "strategy_id": "MR_VOTING_BTC_6H",
          "action": "{{strategy.order.action}}",
          "quantity_usd": {{strategy.order.value}},
          "alert_id": "{{strategy.order.id}}-{{timenow}}",
          "bar_time": "{{timenow}}",
          "price": {{close}}
        }

    `quantity_usd` is the notional size of THIS order in USD, supplied by
    your TradingView strategy's sizing logic. Required for `buy` and `sell`
    actions; ignored on close actions (which exit the whole position).
    """
    secret: str
    strategy_id: str = Field(..., min_length=1, max_length=128)
    action: Action
    quantity_usd: Optional[float] = Field(default=None, ge=0)
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
            if self.quantity_usd is None or self.quantity_usd <= 0:
                raise ValueError(
                    "quantity_usd > 0 is required for buy/sell actions "
                    "(use {{strategy.order.value}} in your TV alert message)"
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
