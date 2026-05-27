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
        qty_usd: float,
        leverage: float = 1.0,
        reduce_only: bool = False,
    ) -> OrderResult: ...

    def close_position(self, symbol: str) -> OrderResult: ...
