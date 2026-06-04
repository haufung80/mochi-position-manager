from __future__ import annotations
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from pybit.unified_trading import HTTP

from ..schemas import OrderResult
from .symbols import base_asset_of, canonical_step_size

log = logging.getLogger(__name__)

Side = Literal["buy", "sell"]
CATEGORY = "linear"  # USDT perpetuals


class BybitExchange:
    name = "bybit"

    # A just-placed market order's executions can lag the place_order ack by a
    # beat. Poll briefly to capture the real fill price + fee. This runs in the
    # background fan-out (after TradingView already got its response), so the
    # extra ~sub-second is not on any user-facing path.
    FILL_POLL_ATTEMPTS = 6
    FILL_POLL_DELAY = 0.25

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
        """Best-effort mark price for OrderResult metadata. Not in the critical
        sizing path — the order quantity comes pre-computed from TradingView."""
        resp = self._client.get_tickers(category=CATEGORY, symbol=symbol)
        items = (resp.get("result") or {}).get("list") or []
        if not items:
            raise RuntimeError(f"Bybit: ticker not found: {symbol}")
        return float(items[0]["lastPrice"])

    def _round_qty(self, symbol: str, qty: float) -> str:
        """Snap a base-asset quantity down to the exchange's lot-size grid.
        Protects against TradingView sending an unaligned size (which Bybit
        would otherwise reject)."""
        inst = self._instrument(symbol)
        lot = inst.get("lotSizeFilter", {})
        step = Decimal(str(lot.get("qtyStep", "0.001")))
        min_qty = Decimal(str(lot.get("minOrderQty", "0")))
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if q < min_qty:
            raise RuntimeError(f"Bybit: quantity {q} below minOrderQty {min_qty} for {symbol}")
        return format(q.normalize(), "f")

    def _fill_details(self, symbol: str, order_id: str, want_qty: float):
        """Poll execution records for `order_id` and aggregate the REAL fill:
        (vwap_price, total_fee, fee_currency, filled_qty). Returns None if no
        execution surfaced within the poll window — caller keeps the mark-price
        fallback and a zero fee. Never raises into the order path."""
        best = None
        for _ in range(self.FILL_POLL_ATTEMPTS):
            try:
                resp = self._client.get_executions(
                    category=CATEGORY, symbol=symbol, orderId=order_id, limit=50,
                )
                rows = (resp.get("result") or {}).get("list") or []
            except Exception as e:
                log.warning("Bybit get_executions failed (continuing): %s", e)
                rows = []
            tot = sum(float(r.get("execQty", 0) or 0) for r in rows)
            if tot > 0:
                notional = sum(
                    float(r.get("execPrice", 0) or 0) * float(r.get("execQty", 0) or 0)
                    for r in rows
                )
                fee = sum(float(r.get("execFee", 0) or 0) for r in rows)
                ccy = next((r.get("feeCurrency") for r in rows if r.get("feeCurrency")), "") or "USDT"
                best = (notional / tot, fee, ccy, tot)
                if tot + 1e-9 >= want_qty:  # full fill captured — stop early
                    return best
            time.sleep(self.FILL_POLL_DELAY)
        return best

    def _ensure_leverage(self, symbol: str, leverage: float) -> None:
        if leverage <= 0:
            return
        try:
            self._client.set_leverage(
                category=CATEGORY, symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage),
            )
        except Exception as e:
            # 110043 == leverage already at the same value (no-op)
            msg = str(e)
            if "110043" in msg or "leverage not modified" in msg.lower():
                return
            raise

    # ---------- public api ----------

    def market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        leverage: float = 1.0,
    ) -> OrderResult:
        try:
            if not self.dry_run:
                self._ensure_leverage(symbol, leverage)

            qty_str = self._round_qty(symbol, quantity)

            # Best-effort mark price for OrderResult metadata. Used by the
            # position tracker to display net_qty_usd. Order placement does
            # NOT depend on this — if the lookup fails we still submit.
            try:
                price = self._mark_price(symbol)
            except Exception as e:
                log.warning("Bybit mark price lookup failed (continuing): %s", e)
                price = 0.0

            if self.dry_run:
                log.info("[DRY_RUN] bybit market %s %s qty=%s lev=%s",
                         side, symbol, qty_str, leverage)
                return OrderResult(success=True, exchange_order_id="DRY_RUN",
                                   filled_qty_base=float(qty_str), avg_price=price)

            resp = self._client.place_order(
                category=CATEGORY,
                symbol=symbol,
                side="Buy" if side == "buy" else "Sell",
                orderType="Market",
                qty=qty_str,
            )
            if resp.get("retCode") != 0:
                return OrderResult(
                    success=False,
                    error_message=f"retCode={resp.get('retCode')} {resp.get('retMsg')}",
                    raw=resp,
                )
            order_id = (resp.get("result") or {}).get("orderId", "")

            # Enrich with the REAL fill price + fee from executions. Best-effort:
            # the order has already placed successfully; a lookup failure just
            # leaves the mark-price estimate and a zero fee.
            fill_price, commission, commission_asset = price, 0.0, ""
            filled_qty = float(qty_str)
            try:
                det = self._fill_details(symbol, order_id, float(qty_str))
                if det:
                    fill_price, commission, commission_asset, filled_qty = det
            except Exception as e:
                log.warning("Bybit fill enrichment failed (continuing): %s", e)

            return OrderResult(
                success=True,
                exchange_order_id=order_id,
                filled_qty_base=filled_qty,
                avg_price=fill_price,
                commission=commission,
                commission_asset=commission_asset,
                raw=resp,
            )
        except Exception as e:
            log.exception("Bybit market_order failed")
            return OrderResult(success=False, error_message=f"{type(e).__name__}: {e}")

    def close_position(self, symbol: str) -> OrderResult:
        try:
            resp = self._client.get_positions(category=CATEGORY, symbol=symbol)
            positions = (resp.get("result") or {}).get("list") or []
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

    def get_position(self, symbol: str) -> tuple[float, float]:
        """(signed_base_qty, mark_price) for the symbol; (0.0, 0.0) if flat."""
        resp = self._client.get_positions(category=CATEGORY, symbol=symbol)
        for p in (resp.get("result") or {}).get("list") or []:
            size = float(p.get("size", 0) or 0)
            if size == 0:
                continue
            signed = size if p.get("side") == "Buy" else -size
            mark = float(p.get("markPrice") or p.get("avgPrice") or 0.0)
            return signed, mark
        return 0.0, 0.0

    def get_price(self, symbol: str) -> float:
        try:
            return self._mark_price(symbol)
        except Exception as e:
            log.warning("Bybit get_price failed for %s: %s", symbol, e)
            return 0.0

    def get_step_size(self, symbol: str) -> float:
        """Bybit lot-size grid (qtyStep). Falls back to the canonical map."""
        try:
            step = self._instrument(symbol).get("lotSizeFilter", {}).get("qtyStep")
            if step:
                return float(step)
        except Exception as e:
            log.warning("Bybit get_step_size failed for %s (using canonical): %s", symbol, e)
        return canonical_step_size(base_asset_of(self.name, symbol))

    def get_funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        """Funding settlements from the account transaction log. NOTE: Bybit v5
        records funding as type=SETTLEMENT with the amount in `funding` (signed;
        negative = paid). Best-effort — returns [] on failure."""
        try:
            resp = self._client.get_transaction_log(
                category=CATEGORY, symbol=symbol, type="SETTLEMENT",
                startTime=start_ms, endTime=end_ms, limit=200,
            )
            rows = (resp.get("result") or {}).get("list") or []
        except Exception as e:
            log.warning("Bybit get_funding failed for %s: %s", symbol, e)
            return []
        out: list[dict] = []
        for r in rows:
            ts = int(r.get("transactionTime") or 0)
            amt = float(r.get("funding") or r.get("change") or 0.0)
            if ts and amt:
                out.append({"time_ms": ts, "amount": amt})
        return out
