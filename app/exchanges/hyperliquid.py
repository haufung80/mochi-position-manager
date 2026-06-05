from __future__ import annotations
import logging
import time
from typing import Literal

from eth_account import Account
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from ..schemas import OrderResult
from .symbols import base_asset_of, canonical_step_size

log = logging.getLogger(__name__)
Side = Literal["buy", "sell"]


class HyperliquidExchange:
    name = "hyperliquid"

    # The order response carries the fill price (avgPx) but not the fee — that
    # surfaces via user_fills, which can lag the order ack slightly. Poll briefly.
    # Runs in the background fan-out, so the latency is not user-facing.
    FILL_POLL_ATTEMPTS = 6
    FILL_POLL_DELAY = 0.25

    def __init__(
        self,
        private_key: str,
        account_address: str = "",
        vault_address: str = "",
        testnet: bool = False,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self._vault_address = vault_address or ""
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self._info = Info(base_url, skip_ws=True)
        if private_key:
            wallet = Account.from_key(private_key)
            account = account_address or wallet.address
            # When vault_address is set, the SDK signs orders as "execute on
            # behalf of vault X". The agent (private_key) must be approved by
            # the vault leader for this to work.
            kwargs: dict = {"account_address": account}
            if self._vault_address:
                kwargs["vault_address"] = self._vault_address
            self._exchange = HLExchange(wallet, base_url, **kwargs)
            self._account_address = account
        else:
            self._exchange = None
            self._account_address = account_address

    def _mid_price(self, symbol: str) -> float:
        all_mids = self._info.all_mids()
        if symbol not in all_mids:
            raise RuntimeError(f"Hyperliquid: symbol not found: {symbol}")
        return float(all_mids[symbol])

    def _fill_fee(self, oid: str) -> tuple[float, str]:
        """Sum the fees for fills belonging to `oid` from user_fills.
        Returns (total_fee, fee_token). Best-effort: returns (0.0, 'USDC') if
        nothing matches in the poll window. Never raises into the order path."""
        if not self._account_address or not oid:
            return 0.0, "USDC"
        for _ in range(self.FILL_POLL_ATTEMPTS):
            try:
                fills = self._info.user_fills(self._account_address) or []
            except Exception as e:
                log.warning("HL user_fills failed (continuing): %s", e)
                fills = []
            matched = [f for f in fills if str(f.get("oid")) == str(oid)]
            if matched:
                fee = sum(float(f.get("fee", 0) or 0) for f in matched)
                token = matched[0].get("feeToken") or "USDC"
                return fee, token
            time.sleep(self.FILL_POLL_DELAY)
        return 0.0, "USDC"

    def _round_size(self, symbol: str, qty: float) -> float:
        """Snap a base-asset quantity to HL's per-asset szDecimals grid."""
        meta = self._info.meta()
        for asset in meta["universe"]:
            if asset["name"] == symbol:
                decimals = int(asset.get("szDecimals", 4))
                return round(qty, decimals)
        return round(qty, 4)

    def market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        leverage: float = 1.0,
    ) -> OrderResult:
        try:
            qty_base = self._round_size(symbol, quantity)
            if qty_base <= 0:
                return OrderResult(
                    success=False,
                    error_message=f"quantity rounded to 0 (requested={quantity}, symbol={symbol})",
                )

            # Best-effort mid price for OrderResult metadata. Used by the
            # position tracker to display net_qty_usd. Order placement does
            # NOT depend on this — if the lookup fails we still submit.
            try:
                price = self._mid_price(symbol)
            except Exception as e:
                log.warning("HL mid price lookup failed (continuing): %s", e)
                price = 0.0

            if self.dry_run or self._exchange is None:
                log.info("[DRY_RUN] hyperliquid market %s %s qty=%s",
                         side, symbol, qty_base)
                return OrderResult(success=True, exchange_order_id="DRY_RUN",
                                   filled_qty_base=qty_base, avg_price=price)

            if leverage and leverage > 0:
                try:
                    self._exchange.update_leverage(int(leverage), symbol)
                except Exception as e:
                    log.warning("Hyperliquid update_leverage failed (continuing): %s", e)

            is_buy = side == "buy"
            # NOTE: HL SDK 0.23+ removed the `reduce_only` kwarg from
            # market_open. For our use case it's a no-op anyway — entries
            # are never reduce_only=True. Closes go through close_position()
            # below, which calls market_close() (a separate SDK method).
            resp = self._exchange.market_open(
                name=symbol,
                is_buy=is_buy,
                sz=qty_base,
                px=None,
                slippage=0.01,
            )
            if resp.get("status") != "ok":
                return OrderResult(success=False, error_message=str(resp), raw=resp)

            statuses = (resp.get("response") or {}).get("data", {}).get("statuses") or []
            oid = ""
            filled = 0.0
            avg_px = price
            for st in statuses:
                if "filled" in st:
                    f = st["filled"]
                    oid = str(f.get("oid", ""))
                    filled += float(f.get("totalSz", 0))
                    avg_px = float(f.get("avgPx", price))
                elif "error" in st:
                    return OrderResult(success=False, error_message=str(st["error"]), raw=resp)

            # avgPx above is the real fill; fees aren't in the order response, so
            # look them up. Best-effort — the order already filled regardless.
            commission, commission_asset = 0.0, ""
            try:
                commission, commission_asset = self._fill_fee(oid)
            except Exception as e:
                log.warning("HL fee enrichment failed (continuing): %s", e)

            return OrderResult(
                success=True,
                exchange_order_id=oid,
                filled_qty_base=filled or qty_base,
                avg_price=avg_px,
                commission=commission,
                commission_asset=commission_asset,
                raw=resp,
            )
        except Exception as e:
            log.exception("Hyperliquid market_order failed")
            return OrderResult(success=False, error_message=f"{type(e).__name__}: {e}")

    def close_position(self, symbol: str) -> OrderResult:
        try:
            if self._exchange is None:
                return OrderResult(success=False, error_message="No private key configured")
            if self.dry_run:
                log.info("[DRY_RUN] hyperliquid close %s", symbol)
                return OrderResult(success=True, exchange_order_id="DRY_RUN")
            resp = self._exchange.market_close(symbol)
            if resp is None:
                return OrderResult(success=True, exchange_order_id="NO_POSITION")
            if resp.get("status") != "ok":
                return OrderResult(success=False, error_message=str(resp), raw=resp)
            return OrderResult(success=True, exchange_order_id="CLOSED", raw=resp)
        except Exception as e:
            log.exception("Hyperliquid close_position failed")
            return OrderResult(success=False, error_message=f"{type(e).__name__}: {e}")

    def get_position(self, symbol: str) -> tuple[float, float]:
        """(signed_base_qty, mark_price) for the symbol; (0.0, 0.0) if flat.
        `szi` is already signed (negative = short)."""
        if not self._account_address:
            return 0.0, 0.0
        state = self._info.user_state(self._account_address)
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") != symbol:
                continue
            szi = float(pos.get("szi", 0) or 0)
            if szi == 0:
                continue
            value = abs(float(pos.get("positionValue", 0) or 0))
            mark = value / abs(szi) if szi else 0.0
            return szi, mark
        return 0.0, 0.0

    def get_position_detail(self, symbol: str) -> dict:
        """Live position incl. the exchange's own entry + unrealized PnL:
        {qty, mark, entry, unrealized}. All 0.0 if flat."""
        flat = {"qty": 0.0, "mark": 0.0, "entry": 0.0, "unrealized": 0.0}
        if not self._account_address:
            return flat
        state = self._info.user_state(self._account_address)
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") != symbol:
                continue
            szi = float(pos.get("szi", 0) or 0)
            if szi == 0:
                continue
            value = abs(float(pos.get("positionValue", 0) or 0))
            return {"qty": szi,
                    "mark": value / abs(szi) if szi else 0.0,
                    "entry": float(pos.get("entryPx") or 0.0),
                    "unrealized": float(pos.get("unrealizedPnl") or 0.0)}
        return flat

    def get_price(self, symbol: str) -> float:
        try:
            return self._mid_price(symbol)
        except Exception as e:
            log.warning("HL get_price failed for %s: %s", symbol, e)
            return 0.0

    def get_step_size(self, symbol: str) -> float:
        """HL per-asset grid from szDecimals. Falls back to the canonical map."""
        try:
            for asset in self._info.meta().get("universe", []):
                if asset.get("name") == symbol:
                    return 10 ** -int(asset.get("szDecimals", 4))
        except Exception as e:
            log.warning("HL get_step_size failed for %s (using canonical): %s", symbol, e)
        return canonical_step_size(base_asset_of(self.name, symbol))

    def get_funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        """Account funding history for `symbol` (HL records `delta.usdc`, signed;
        negative = paid). Best-effort — returns [] on failure."""
        if not self._account_address:
            return []
        try:
            hist = self._info.user_funding_history(self._account_address, start_ms, end_ms) or []
        except Exception as e:
            log.warning("HL get_funding failed for %s: %s", symbol, e)
            return []
        out: list[dict] = []
        for h in hist:
            delta = h.get("delta") or {}
            if delta.get("coin") != symbol:
                continue
            ts = int(h.get("time") or 0)
            amt = float(delta.get("usdc") or 0.0)
            if ts:
                out.append({"time_ms": ts, "amount": amt})
        return out

    def get_min_notional(self, symbol: str) -> float:
        """Hyperliquid enforces a global $10 minimum order value."""
        return 10.0
