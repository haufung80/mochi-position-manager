from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


Action = Literal["buy", "sell", "close", "close_long", "close_short"]


class TradingViewAlert(BaseModel):
    """JSON body that TradingView POSTs to /webhook/tradingview.

    Example payload (paste this into the TradingView alert message box):
    {
      "secret": "{{strategy.account_currency}}-replace-me",
      "strategy_id": "MR_VOTING_BTC_6H",
      "action": "buy",
      "alert_id": "{{strategy.order.id}}",
      "bar_time": "{{timenow}}",
      "price": "{{close}}"
    }
    """
    secret: str
    strategy_id: str = Field(..., min_length=1, max_length=128)
    action: Action
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
