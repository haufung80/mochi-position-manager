from __future__ import annotations
from typing import Protocol, Literal
from ..schemas import OrderResult


Side = Literal["buy", "sell"]


class Exchange(Protocol):
    name: str

    def market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,        # in base-asset units (e.g. 0.001 = 0.001 BTC)
        leverage: float = 1.0,
    ) -> OrderResult: ...

    def close_position(self, symbol: str) -> OrderResult:
        """Emergency escape hatch — close full position via reduceOnly.
        NOT used by the regular webhook path (TradingView only sends
        buy/sell). Reserved for future admin endpoints."""
        ...
