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
CATEGORY = "linear"       # USDT perpetuals
SPOT_CATEGORY = "spot"    # USDT spot (the cash-and-carry spot leg)


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
        # Keyed by (category, symbol): BTCUSDT exists in BOTH spot and linear with
        # DIFFERENT filters, so a symbol-only cache would return the wrong grid.
        self._instrument_cache: dict[tuple[str, str], dict] = {}

    # ---------- helpers ----------

    def _instrument(self, symbol: str, category: str = CATEGORY) -> dict:
        key = (category, symbol)
        if key not in self._instrument_cache:
            resp = self._client.get_instruments_info(category=category, symbol=symbol)
            items = (resp.get("result") or {}).get("list") or []
            if not items:
                raise RuntimeError(f"Bybit: instrument not found: {category}:{symbol}")
            self._instrument_cache[key] = items[0]
        return self._instrument_cache[key]

    def _mark_price(self, symbol: str) -> float:
        """Best-effort mark price for OrderResult metadata. Not in the critical
        sizing path — the order quantity comes pre-computed from TradingView."""
        resp = self._client.get_tickers(category=CATEGORY, symbol=symbol)
        items = (resp.get("result") or {}).get("list") or []
        if not items:
            raise RuntimeError(f"Bybit: ticker not found: {symbol}")
        return float(items[0]["lastPrice"])

    def _round_qty(self, symbol: str, qty: float) -> str:
        """Snap a base-asset quantity DOWN to the exchange's lot-size grid (protects
        against an unaligned size Bybit would reject). FLOAT-DUST TOLERANT: a value a
        hair below a step multiple — e.g. a managed-close `abs(net)` of 0.34 that the
        RMW ledger stores as 0.33999999999999997 — snaps to that multiple instead of
        dropping a whole step. Dropping one systematically under-closes a position and
        leaves a residual (e.g. closed 0.33 of a 0.34 long → 0.01 stuck)."""
        inst = self._instrument(symbol)
        lot = inst.get("lotSizeFilter", {})
        step = Decimal(str(lot.get("qtyStep", "0.001")))
        min_qty = Decimal(str(lot.get("minOrderQty", "0")))
        # +epsilon (in step-units; ~1e-9 of a step) absorbs float dust at the boundary
        # without promoting a genuine sub-step quantity (e.g. 0.335 still floors to 0.33).
        units = (Decimal(str(qty)) / step + Decimal("1e-9")).to_integral_value(rounding=ROUND_DOWN)
        q = units * step
        if q < min_qty:
            raise RuntimeError(f"Bybit: quantity {q} below minOrderQty {min_qty} for {symbol}")
        return format(q.normalize(), "f")

    def _fill_details(self, symbol: str, order_id: str, want_qty: float,
                      category: str = CATEGORY):
        """Poll execution records for `order_id` and aggregate the REAL fill:
        (vwap_price, total_fee, fee_currency, filled_qty). Returns None if no
        execution surfaced within the poll window — caller keeps the mark-price
        fallback and a zero fee. Never raises into the order path."""
        best = None
        for _ in range(self.FILL_POLL_ATTEMPTS):
            try:
                resp = self._client.get_executions(
                    category=category, symbol=symbol, orderId=order_id, limit=50,
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
        d = self.get_position_detail(symbol)
        return d["qty"], d["mark"]

    def get_position_detail(self, symbol: str) -> dict:
        """Live position incl. the exchange's own entry + unrealized PnL:
        {qty, mark, entry, unrealized}. All 0.0 if flat. Lets the dashboard show
        the venue's own unrealized rather than reconstructing it from a (possibly
        blended/attributed) ledger entry."""
        resp = self._client.get_positions(category=CATEGORY, symbol=symbol)
        for p in (resp.get("result") or {}).get("list") or []:
            size = float(p.get("size", 0) or 0)
            if size == 0:
                continue
            signed = size if p.get("side") == "Buy" else -size
            # unrealisedPnl absent (None/"") -> None so callers can fall back to an
            # entry-based estimate; a genuine "0" parses to 0.0 and is trusted as-is.
            up = p.get("unrealisedPnl")
            return {"qty": signed,
                    "mark": float(p.get("markPrice") or p.get("avgPrice") or 0.0),
                    "entry": float(p.get("avgPrice") or 0.0),
                    "unrealized": float(up) if up not in (None, "") else None}
        return {"qty": 0.0, "mark": 0.0, "entry": 0.0, "unrealized": 0.0}

    def get_kline_close(self, symbol: str, ts_ms: int) -> float:
        """1-minute close of the candle CONTAINING `ts_ms` — reconstructs an entry
        price for a fill recorded without one (a market order fills ~here). 0.0 on
        failure. The window is floored to the minute: Bybit returns klines newest-
        first, so an unaligned [ts, ts+60s] window would span two candles and
        rows[0] would be the minute AFTER the fill."""
        start = (ts_ms // 60_000) * 60_000          # floor to the minute boundary
        try:
            resp = self._client.get_kline(category=CATEGORY, symbol=symbol,
                                          interval="1", start=start,
                                          end=start + 59_999, limit=1)
            rows = (resp.get("result") or {}).get("list") or []
            return float(rows[0][4]) if rows else 0.0
        except Exception as e:
            log.warning("Bybit get_kline_close failed for %s @ %s: %s", symbol, ts_ms, e)
            return 0.0

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

    @staticmethod
    def _settlement_row(r: dict):
        """Decode one Bybit v5 SETTLEMENT transaction-log row -> (time_ms, amount), or
        None to skip (no time/amount). Funding is in `funding` (signed; negative =
        paid). One decoder so the live funding poll and the equity backfill can't drift
        on field names."""
        ts = int(r.get("transactionTime") or 0)
        amt = float(r.get("funding") or r.get("change") or 0.0)
        return (ts, amt) if ts and amt else None

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
            row = self._settlement_row(r)
            if row:
                out.append({"time_ms": row[0], "amount": row[1]})
        return out

    def get_closed_pnl(self, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """Realized PnL of every closed position in [start,end] (all symbols) as
        (time_ms, closedPnl). Paginated in <=7-day windows (Bybit's max range).
        Best-effort — returns what it can. For the equity backfill."""
        out: list[tuple[int, float]] = []
        win = 7 * 24 * 3600 * 1000
        ws = start_ms
        while ws < end_ms:
            we = min(ws + win - 1, end_ms)
            cursor = ""
            for _ in range(50):                      # pagination safety cap
                try:
                    resp = self._client.get_closed_pnl(
                        category=CATEGORY, startTime=ws, endTime=we, limit=100, cursor=cursor)
                except Exception as e:
                    log.warning("Bybit get_closed_pnl %s-%s failed: %s", ws, we, e)
                    break
                result = resp.get("result") or {}
                for r in result.get("list") or []:
                    ts = int(r.get("updatedTime") or r.get("createdTime") or 0)
                    if ts:
                        out.append((ts, float(r.get("closedPnl") or 0.0)))
                cursor = result.get("nextPageCursor") or ""
                if not cursor:
                    break
            ws = we + 1
        return out

    def get_account_funding(self, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """All-symbol funding settlements in [start,end] as (time_ms, amount) (signed;
        negative = paid). Paginated in <=7-day windows. Best-effort."""
        out: list[tuple[int, float]] = []
        win = 7 * 24 * 3600 * 1000
        ws = start_ms
        while ws < end_ms:
            we = min(ws + win - 1, end_ms)
            cursor = ""
            for _ in range(50):
                try:
                    resp = self._client.get_transaction_log(
                        category=CATEGORY, type="SETTLEMENT",
                        startTime=ws, endTime=we, limit=200, cursor=cursor)
                except Exception as e:
                    log.warning("Bybit get_account_funding %s-%s failed: %s", ws, we, e)
                    break
                result = resp.get("result") or {}
                for r in result.get("list") or []:
                    row = self._settlement_row(r)
                    if row:
                        out.append(row)
                cursor = result.get("nextPageCursor") or ""
                if not cursor:
                    break
            ws = we + 1
        return out

    def get_min_notional(self, symbol: str) -> float:
        """Bybit minimum order value (USDT), from lotSizeFilter. Falls back to $5."""
        try:
            v = self._instrument(symbol).get("lotSizeFilter", {}).get("minNotionalValue")
            if v:
                return float(v)
        except Exception as e:
            log.warning("Bybit get_min_notional failed for %s: %s", symbol, e)
        return 5.0

    # ---------- spot (funding-arb cash-and-carry spot leg) ----------
    #
    # Bybit spot is a DISTINCT category from the hard-coded `linear` perp methods
    # above. It has its own instrument filters (basePrecision / minOrderQty /
    # minOrderAmt), uses base balances rather than positions, and charges the BUY
    # fee in the BASE coin. None of the linear methods are overloaded.

    def _spot_mark_price(self, symbol: str) -> float:
        resp = self._client.get_tickers(category=SPOT_CATEGORY, symbol=symbol)
        items = (resp.get("result") or {}).get("list") or []
        if not items:
            raise RuntimeError(f"Bybit: spot ticker not found: {symbol}")
        return float(items[0]["lastPrice"])

    def _spot_round_qty(self, symbol: str, qty: float) -> str:
        """Snap a base-asset quantity DOWN to the spot grid (basePrecision /
        minOrderQty). Distinct from the linear `_round_qty` (qtyStep)."""
        lot = self._instrument(symbol, SPOT_CATEGORY).get("lotSizeFilter", {})
        step = Decimal(str(lot.get("basePrecision", "0.000001")))
        min_qty = Decimal(str(lot.get("minOrderQty", "0")))
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if q < min_qty:
            raise RuntimeError(
                f"Bybit spot: quantity {q} below minOrderQty {min_qty} for {symbol}")
        return format(q.normalize(), "f")

    def spot_market_order(self, symbol: str, side: Side, qty: float) -> OrderResult:
        """Spot market order, base-denominated (`marketUnit=baseCoin`).

        For a BUY the fee is charged in the BASE coin, so the NET received base is
        `executed - base_fee` — that (hedgeable) quantity is returned as
        `filled_qty_base`, with `commission_asset` = the base coin. The caller
        records the net so neutrality is measured against what is actually held.
        """
        try:
            qty_str = self._spot_round_qty(symbol, qty)

            try:
                price = self._spot_mark_price(symbol)
            except Exception as e:
                log.warning("Bybit spot price lookup failed (continuing): %s", e)
                price = 0.0

            if self.dry_run:
                log.info("[DRY_RUN] bybit spot %s %s qty=%s", side, symbol, qty_str)
                return OrderResult(success=True, exchange_order_id="DRY_RUN",
                                   filled_qty_base=float(qty_str), avg_price=price)

            resp = self._client.place_order(
                category=SPOT_CATEGORY,
                symbol=symbol,
                side="Buy" if side == "buy" else "Sell",
                orderType="Market",
                qty=qty_str,
                marketUnit="baseCoin",   # qty is in base units (not quote)
            )
            if resp.get("retCode") != 0:
                return OrderResult(
                    success=False,
                    error_message=f"retCode={resp.get('retCode')} {resp.get('retMsg')}",
                    raw=resp,
                )
            order_id = (resp.get("result") or {}).get("orderId", "")

            fill_price, commission, commission_asset = price, 0.0, ""
            filled_qty = float(qty_str)
            try:
                det = self._fill_details(symbol, order_id, float(qty_str),
                                         category=SPOT_CATEGORY)
                if det:
                    fill_price, commission, commission_asset, filled_qty = det
            except Exception as e:
                log.warning("Bybit spot fill enrichment failed (continuing): %s", e)

            # Base-denominated BUY fee -> the actually-held base is net of it.
            base_coin = base_asset_of(self.name, symbol)
            if (side == "buy" and commission
                    and commission_asset.upper() == base_coin.upper()):
                filled_qty = max(0.0, filled_qty - commission)

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
            log.exception("Bybit spot_market_order failed")
            return OrderResult(success=False, error_message=f"{type(e).__name__}: {e}")

    def get_spot_balance(self, base_asset: str) -> float:
        """FREE (available) spot balance of `base_asset`, in base units.
        Returns the transferable balance, not the total wallet balance."""
        try:
            resp = self._client.get_wallet_balance(accountType="UNIFIED", coin=base_asset)
            for acct in (resp.get("result") or {}).get("list") or []:
                for c in acct.get("coin") or []:
                    if str(c.get("coin", "")).upper() == base_asset.upper():
                        free = c.get("availableToWithdraw") or c.get("free") or c.get("walletBalance")
                        return float(free or 0.0)
        except Exception as e:
            log.warning("Bybit get_spot_balance failed for %s: %s", base_asset, e)
        return 0.0

    def get_spot_step_size(self, symbol: str) -> float:
        """Spot base-precision grid. Falls back to the canonical map."""
        try:
            bp = self._instrument(symbol, SPOT_CATEGORY).get("lotSizeFilter", {}).get("basePrecision")
            if bp:
                return float(bp)
        except Exception as e:
            log.warning("Bybit get_spot_step_size failed for %s (canonical): %s", symbol, e)
        return canonical_step_size(base_asset_of(self.name, symbol))

    def get_spot_min_notional(self, symbol: str) -> float:
        """Spot minimum order VALUE (quote/USDT), from the spot filter's
        `minOrderAmt`. Falls back to $5."""
        try:
            v = self._instrument(symbol, SPOT_CATEGORY).get("lotSizeFilter", {}).get("minOrderAmt")
            if v:
                return float(v)
        except Exception as e:
            log.warning("Bybit get_spot_min_notional failed for %s: %s", symbol, e)
        return 5.0
