from __future__ import annotations
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from pybit.unified_trading import HTTP

from ..schemas import OrderResult

log = logging.getLogger(__name__)

Side = Literal["buy", "sell"]
CATEGORY = "linear"  # USDT perpetuals


class BybitExchange:
    name = "bybit"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, dry_run: bool = False):
        self.dry_run = dry_run
        self._client = HTTP(
            testnet=testnet,
            api_key=api_key or None,
            api_secret=api_secret or None,
        )
        self._instrument_cache: dict[str, dict] = {}

    # ---------- helpers ----------

    def _instrument(self, symbol: str) -> dict:
        if symbol not in self._instrument_cache:
            resp = self._client.get_instruments_info(category=CATEGORY, symbol=symbol)
            items = (resp.get("result") or {}).get("list") or []
            if not items:
                raise RuntimeError(f"Bybit: instrument not found: {symbol}")
            self._instrument_cache[symbol] = items[0]
        return self._instrument_cache[symbol]

    def _mark_price(self, symbol: str) -> float:
        resp = self._client.get_tickers(category=CATEGORY, symbol=symbol)
        items = (resp.get("result") or {}).get("list") or []
        if not items:
            raise RuntimeError(f"Bybit: ticker not found: {symbol}")
        return float(items[0]["lastPrice"])

    def _round_qty(self, symbol: str, qty: float) -> str:
        inst = self._instrument(symbol)
        lot = inst.get("lotSizeFilter", {})
        step = Decimal(str(lot.get("qtyStep", "0.001")))
        min_qty = Decimal(str(lot.get("minOrderQty", "0")))
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if q < min_qty:
            raise RuntimeError(f"Bybit: computed qty {q} below minOrderQty {min_qty} for {symbol}")
        # Normalize to step's decimal places
        return format(q.normalize(), "f")

    def _ensure_leverage(self, symbol: str, leverage: float) -> None:
        if leverage <= 0:
            return
        try:
            self._client.set_leverage(
                category=CATEGORY, symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage),
            )
        except Exception as e:
            # Bybit returns retCode 110043 when leverage is already set to the same value.
            msg = str(e)
            if "110043" in msg or "leverage not modified" in msg.lower():
                return
            raise

    # ---------- public api ----------

    def market_order(
        self,
        symbol: str,
        side: Side,
        qty_usd: float,
        leverage: float = 1.0,
        reduce_only: bool = False,
    ) -> OrderResult:
        try:
            # Skip authenticated leverage call in dry_run so the app
            # works end-to-end without real API keys.
            if not self.dry_run:
                self._ensure_leverage(symbol, leverage)
            price = self._mark_price(symbol)
            qty_base = qty_usd / price
            qty_str = self._round_qty(symbol, qty_base)

            if self.dry_run:
                log.info("[DRY_RUN] bybit market %s %s qty=%s usd=%.2f lev=%s",
                         side, symbol, qty_str, qty_usd, leverage)
                return OrderResult(success=True, exchange_order_id="DRY_RUN",
                                   filled_qty_base=float(qty_str), avg_price=price)

            resp = self._client.place_order(
                category=CATEGORY,
                symbol=symbol,
                side="Buy" if side == "buy" else "Sell",
                orderType="Market",
                qty=qty_str,
                reduceOnly=reduce_only,
            )
            if resp.get("retCode") != 0:
                return OrderResult(
                    success=False,
                    error_message=f"retCode={resp.get('retCode')} {resp.get('retMsg')}",
                    raw=resp,
                )
            order_id = (resp.get("result") or {}).get("orderId", "")
            return OrderResult(
                success=True,
                exchange_order_id=order_id,
                filled_qty_base=float(qty_str),
                avg_price=price,
                raw=resp,
            )
        except Exception as e:
            log.exception("Bybit market_order failed")
            return OrderResult(success=False, error_message=f"{type(e).__name__}: {e}")

    def close_position(self, symbol: str) -> OrderResult:
        try:
            resp = self._client.get_positions(category=CATEGORY, symbol=symbol)
            positions = (resp.get("result") or {}).get("list") or []
            # one-way mode: at most one entry; hedge mode: long+short
            for p in positions:
                size = float(p.get("size", 0) or 0)
                if size == 0:
                    continue
                side = "Sell" if p.get("side") == "Buy" else "Buy"
                qty = str(size)
                if self.dry_run:
                    log.info("[DRY_RUN] bybit close %s qty=%s", symbol, qty)
                    continue
                close_resp = self._client.place_order(
                    category=CATEGORY,
                    symbol=symbol,
                    side=side,
                    orderType="Market",
                    qty=qty,
                    reduceOnly=True,
                )
                if close_resp.get("retCode") != 0:
                    return OrderResult(
                        success=False,
                        error_message=f"close retCode={close_resp.get('retCode')} {close_resp.get('retMsg')}",
                        raw=close_resp,
                    )
            return OrderResult(success=True, exchange_order_id="CLOSED")
        except Exception as e:
            log.exception("Bybit close_position failed")
            return OrderResult(success=False, error_message=f"{type(e).__name__}: {e}")
