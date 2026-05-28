"""Pydantic schemas for inbound webhook payloads and outbound exchange results."""
from __future__ import annotations
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


Action = Literal["buy", "sell"]


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

    Actions: TradingView's `{{strategy.order.action}}` always resolves to
    either `buy` or `sell` — entries AND closes both surface as one of these
    two. The middleware places a market order in the given direction; on
    one-way-mode perps (Bybit/HL defaults) a sell against an open long
    naturally closes it, and vice versa.

    `quantity` is the order size in **base-asset units** (e.g. 0.001 = 0.001 BTC),
    surfaced via `{{strategy.order.contracts}}` in TradingView. Required
    (> 0) on every alert.

    Note: this is NOT a USD amount. If your pine script sizes orders in dollars,
    convert to base in pine before sending (e.g. `qty := cash_size / close`).
    """
    secret: str
    strategy_id: str = Field(..., min_length=1, max_length=128)
    action: Action
    quantity: float = Field(..., gt=0)
    alert_id: Optional[str] = None
    bar_time: Optional[str] = None
    price: Optional[float] = None

    @field_validator("strategy_id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        return v.strip()


class OrderResult(BaseModel):
    """Normalized result returned by every exchange adapter."""
    success: bool
    exchange_order_id: str = ""
    filled_qty_base: float = 0.0
    avg_price: float = 0.0
    error_message: str = ""
    raw: dict | None = None
