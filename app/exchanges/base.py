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

    def fetch_fill(self, symbol: str, order_id: str,
                   want_qty: float = 0.0) -> OrderResult | None:
        """Re-fetch a PAST order's real fill (commission [+ price]) by exchange
        order id — for the commission backfill (orders whose fee wasn't captured at
        fill time). `OrderResult.fee_source="backfill"` on success; None when the
        venue has no execution record for it. Best-effort; never raises."""
        ...

    # ---------- spot surface (funding-arb cash-and-carry legs) ----------
    # The arb spot leg (long spot vs short perp) needs a market-order + balance +
    # grid + min-notional surface SEPARATE from the perp methods above. Both
    # Bybit and Hyperliquid implement these for real; a venue that genuinely
    # cannot do spot should raise a clear error here, never AttributeError.

    def spot_market_order(self, symbol: str, side: Side, qty: float) -> OrderResult:
        """Place a spot market order. `qty` is the base-asset quantity (snapped to
        the venue's spot grid). For a BUY whose fee is charged in the BASE coin
        (Bybit), `OrderResult.filled_qty_base` is the NET received base and
        `commission_asset` is that base coin — the caller records the hedgeable
        (net) quantity. Best-effort enrichment; the order itself is never blocked."""
        ...

    def get_spot_balance(self, base_asset: str) -> float:
        """FREE/available spot balance of `base_asset` in base units (not the
        total wallet balance — held/locked amounts are excluded). Used to clamp a
        spot SELL on close so it never exceeds what is actually held."""
        ...

    def get_spot_step_size(self, symbol: str) -> float:
        """Minimum spot order unit (step size) for `symbol`, in base-asset units.
        Distinct from the perp grid (Bybit basePrecision vs qtyStep; HL spot
        szDecimals)."""
        ...

    def get_spot_min_notional(self, symbol: str) -> float:
        """Minimum spot order value (quote currency, e.g. USDT/USDC) the exchange
        accepts for `symbol`."""
        ...
