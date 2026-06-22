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
          "alert_id": "{{time}}"
        }

    Actions: TradingView's `{{strategy.order.action}}` always resolves to
    either `buy` or `sell` — entries AND closes both surface as one of these
    two. The middleware places a market order in the given direction; on
    one-way-mode perps (Bybit/HL defaults) a sell against an open long
    naturally closes it, and vice versa.

    `quantity` is the order size in **base-asset units** (e.g. 0.001 = 0.001 BTC),
    surfaced via `{{strategy.order.contracts}}` in TradingView. Required (> 0) for
    alert-driven (sar=true) strategies; optional and IGNORED for managed
    (sar=false) strategies, where the middleware sizes from `position_size`.

    Note: this is NOT a USD amount. If your pine script sizes orders in dollars,
    convert to base in pine before sending (e.g. `qty := cash_size / close`).

    `alert_id` is the dedup key (see app/dedup.py). The app prefixes it with
    strategy_id, so `{{time}}` alone (the triggering bar's timestamp) is enough
    to make the key unique per bar: a candle that repaints and re-fires the same
    signal collapses to one alert, while the next bar's signal goes through.
    Keep alert_id to a SINGLE placeholder — TradingView's alert editor flags a
    JSON warning when several are concatenated (e.g.
    `{{ticker}}_{{interval}}_{{time}}`). For stop-and-reverse strategies that
    close and re-enter on the same bar you'd also need the action; TradingView
    still lets you save through that warning since the fired payload is valid.
    """
    secret: str
    strategy_id: str = Field(..., min_length=1, max_length=128)
    action: Action
    quantity: Optional[float] = Field(None, gt=0)
    alert_id: Optional[str] = None
    bar_time: Optional[str] = None
    price: Optional[float] = None

    @field_validator("strategy_id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        return v.strip()


class OrderResult(BaseModel):
    """Normalized result returned by every exchange adapter.

    `avg_price` is the ACTUAL volume-weighted fill price (not a pre-trade mark),
    so it can be compared against the signal price for slippage. `commission` is
    the real fee charged for this fill, in `commission_asset` units (USDT on
    Bybit linear, USDC on Hyperliquid). Both are best-effort: adapters fetch them
    after the order fills, and fall back to 0 / mark price if the lookup fails —
    the order itself is never blocked on enrichment.

    `fee_source` records that fidelity so a 0 fee is unambiguous: "exchange" = real
    fee fetched from the venue, "unavailable" = enrichment failed (commission is a 0
    placeholder), "dry_run" = simulated. The executor persists it onto Order."""
    success: bool
    exchange_order_id: str = ""
    filled_qty_base: float = 0.0
    avg_price: float = 0.0
    commission: float = 0.0
    commission_asset: str = ""
    fee_source: str = ""
    error_message: str = ""
    raw: dict | None = None


# fee_source: fidelity of OrderResult.commission / Order.fee_source — ONE canonical set,
# so a typo in any producer can't silently disable the /performance "fee not captured"
# warning. (The order-table templates compare these literals directly; keep them in sync
# if these ever change. "" = legacy row written before the column existed = unknown.)
FEE_SOURCE_EXCHANGE = "exchange"        # real fee fetched from the venue at fill time
FEE_SOURCE_UNAVAILABLE = "unavailable"  # enrichment missed -> commission is a 0 placeholder
FEE_SOURCE_DRY_RUN = "dry_run"          # simulated (DRY_RUN)
