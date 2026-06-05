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

    def get_position(self, symbol: str) -> tuple[float, float]:
        """Live position for `symbol`: (signed_base_qty, mark_price).
        Positive qty = long, negative = short, 0.0 = flat. Read-only; used to
        reconcile the internal ledger to real exchange state."""
        ...

    def get_position_detail(self, symbol: str) -> dict:
        """Live position incl. the exchange's own entry + unrealized PnL:
        {qty, mark, entry, unrealized}. All 0.0 if flat. Lets callers use the
        venue's own unrealized instead of reconstructing it from a ledger entry."""
        ...

    def get_price(self, symbol: str) -> float:
        """Latest price for `symbol` (mark on Bybit, mid on Hyperliquid). Used to
        size managed orders from a USDT budget. Best-effort: 0.0 on failure."""
        ...

    def get_step_size(self, symbol: str) -> float:
        """Minimum order unit (step size) for `symbol`, in base-asset units.
        Prefers the exchange's own grid; falls back to a canonical map."""
        ...

    def get_funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        """Funding payments for `symbol` in [start_ms, end_ms]. Each item is
        {"time_ms": int, "amount": float} (amount signed: + received, - paid).
        Best-effort: returns [] on failure."""
        ...

    def get_min_notional(self, symbol: str) -> float:
        """Minimum order value (quote currency, e.g. USDT) the exchange accepts,
        so managed/paper orders aren't placed below it and rejected."""
        ...
