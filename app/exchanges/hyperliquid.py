from __future__ import annotations
import logging
from typing import Literal

from eth_account import Account
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from ..schemas import OrderResult

log = logging.getLogger(__name__)
Side = Literal["buy", "sell"]


class HyperliquidExchange:
    name = "hyperliquid"

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
        reduce_only: bool = False,
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
            return OrderResult(
                success=True,
                exchange_order_id=oid,
                filled_qty_base=filled or qty_base,
                avg_price=avg_px,
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
