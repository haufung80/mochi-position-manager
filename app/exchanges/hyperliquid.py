from __future__ import annotations
import logging
import time
from typing import Literal

from eth_account import Account
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from ..schemas import OrderResult
from .symbols import (
    HYPERLIQUID_NATIVE_SPOT,
    HYPERLIQUID_SPOT_QUOTE,
    base_asset_of,
    canonical_step_size,
    hyperliquid_spot_token,
)

log = logging.getLogger(__name__)
Side = Literal["buy", "sell"]

# Unit-spot base-token szDecimals as listed on HL (UBTC/UETH/USOL). Used ONLY as
# an offline/DRY_RUN fallback when `spotMeta` can't be fetched; production reads
# the live value from `spotMeta`.
_HL_SPOT_SZ_DECIMALS: dict[str, int] = {
    "BTC": 5, "ETH": 4, "SOL": 3,        # Unit-bridged majors (keyed by canonical base)
    "HYPE": 2, "PURR": 0,                # HL-native (PURR trades in whole units)
}


def _canonical_spot_decimals(symbol: str) -> int:
    """Offline-safe spot szDecimals for a spot symbol ('UBTC/USDC' | 'UBTC' | 'BTC' |
    'HYPE/USDC' | 'PURR'). Falls back to 4 for unknown assets."""
    token = symbol.split("/")[0].upper()
    # Strip the Unit 'U' prefix to recover the canonical base, but NOT for HL-native
    # tokens (HYPE/PURR don't start with U anyway; guard keeps a future U-named native safe).
    base = (token[1:] if token.startswith("U") and len(token) > 1
            and token not in HYPERLIQUID_NATIVE_SPOT else token)
    return _HL_SPOT_SZ_DECIMALS.get(base, 4)


class HyperliquidExchange:
    """Hyperliquid adapter — perp + REAL Unit spot.

    SEPARATE-ACCOUNT NOTE (funding-arb): ``close_position`` closes the WHOLE coin
    on the account and ``get_funding`` is account-wide, so the funding-arb book
    MUST be a distinct HL account (own key + address + margin). The registry
    enforces this at construction (it refuses an ``arb`` adapter whose address
    equals the directional one), so by the time an order is placed the arb book
    can never touch the directional position.

    SPOT (first-class): ``spot_market_order``/``get_spot_balance``/
    ``get_spot_step_size``/``get_spot_min_notional`` trade HL Unit spot. The base
    token is ``'U'+base`` (UBTC/UETH/USOL) quoted in USDC; the order coin is the
    pair's canonical name (e.g. ``'@142'``), resolved from ``spotMeta``. The SDK's
    ``market_open`` routes spot vs perp automatically (spot asset ids start at
    10000) and is itself an aggressive IOC limit at the slippage-adjusted mid —
    HL has no separate "market" primitive, which is why spot uses the same call.
    """

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

    @staticmethod
    def _is_spot_symbol(symbol: str) -> bool:
        """A spot symbol is the readable Unit pair form 'UBTC/USDC' (it carries the
        '/' quote separator). Perp symbols are bare tickers ('BTC') — never spot."""
        return "/" in symbol

    def _mid_price(self, symbol: str) -> float:
        """Mid price from `all_mids()`. PERP symbols ('BTC') key directly; SPOT
        symbols ('UBTC/USDC') are keyed by their canonical pair name ('@142'),
        which `all_mids()` returns — so resolve the spot pair first, then look it
        up under that name. (Verified live: all_mids()['@142'] is the UBTC spot
        mid; 'UBTC'/'UBTC/USDC' are NOT keys.)"""
        key = symbol
        if self._is_spot_symbol(symbol):
            key, _ = self._resolve_spot(symbol)   # 'UBTC/USDC' -> '@142'
        all_mids = self._info.all_mids()
        if key not in all_mids:
            raise RuntimeError(f"Hyperliquid: symbol not found: {symbol}")
        return float(all_mids[key])

    def _fill_fee(self, oid: str) -> tuple[float, str]:
        """Sum the fees for fills belonging to `oid` from user_fills.
        Returns (total_fee, fee_token). Best-effort: returns (0.0, 'USDC') if
        nothing matches in the poll window. Never raises into the order path.
        (The spot path + tests rely on this 2-tuple; the perp fill path uses
        `_fill_fee_detail` for the found flag.)"""
        fee, token, _ = self._fill_fee_detail(oid)
        return fee, token

    def _fill_fee_detail(self, oid: str) -> tuple[float, str, bool]:
        """As `_fill_fee` plus a `found` flag — False when no fill matched in the
        poll window, so the caller marks commission a 0 PLACEHOLDER (fee_source
        'unavailable') rather than a real zero."""
        if not self._account_address or not oid:
            return 0.0, "USDC", False
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
                return fee, token, True
            time.sleep(self.FILL_POLL_DELAY)
        return 0.0, "USDC", False

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
                                   filled_qty_base=qty_base, avg_price=price,
                                   fee_source="dry_run")

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
            commission, commission_asset, fee_found = 0.0, "", False
            try:
                commission, commission_asset, fee_found = self._fill_fee_detail(oid)
            except Exception as e:
                log.warning("HL fee enrichment failed (continuing): %s", e)

            return OrderResult(
                success=True,
                exchange_order_id=oid,
                filled_qty_base=filled or qty_base,
                avg_price=avg_px,
                commission=commission,
                commission_asset=commission_asset,
                # avg_px is always the real fill; only the fee is best-effort here.
                fee_source="exchange" if fee_found else "unavailable",
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
        d = self.get_position_detail(symbol)
        return d["qty"], d["mark"]

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
            # unrealizedPnl absent -> None so callers can fall back to an entry-based
            # estimate; a genuine "0" parses to 0.0 and is trusted.
            up = pos.get("unrealizedPnl")
            return {"qty": szi,
                    "mark": value / abs(szi),        # szi != 0 here (filtered above)
                    "entry": float(pos.get("entryPx") or 0.0),
                    "unrealized": float(up) if up not in (None, "") else None}
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

    def get_pnl_history(self) -> list[tuple[int, float]]:
        """Account cumulative PnL over time from HL's `portfolio` endpoint, as
        (time_ms, cumulative_pnl_usd). Prefers the longest/perp window. HL's PnL
        history reflects account-value change (incl. unrealized). Best-effort — for
        the equity backfill; returns [] on any shape/transport failure."""
        if not self._account_address:
            return []
        try:
            data = self._info.post("/info", {"type": "portfolio", "user": self._account_address})
        except Exception as e:
            log.warning("HL get_pnl_history failed: %s", e)
            return []
        periods: dict = {}
        try:
            for name, payload in data:                 # [[period, {...}], ...]
                periods[name] = payload
        except (TypeError, ValueError):
            return []
        for key in ("perpAllTime", "allTime", "perpMonth", "month"):
            hist = (periods.get(key) or {}).get("pnlHistory") or []
            out: list[tuple[int, float]] = []
            for pt in hist:
                try:
                    out.append((int(pt[0]), float(pt[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            if out:
                return out
        return []

    # ---------- spot (REAL, first-class — Unit spot) ----------

    def _spot_base_token(self, symbol: str) -> str:
        """The HL spot BASE token name for whatever spot symbol form is passed: a
        readable pair ('UBTC/USDC', 'HYPE/USDC') or a token ('UBTC', 'HYPE') carries
        the EXACT token verbatim (Unit majors are 'U'+base; HL-native tokens like
        HYPE/PURR are the bare name); a bare canonical base ('BTC') maps via
        hyperliquid_spot_token. The canonical '@NNN' id is resolved from `spotMeta`."""
        token = symbol.split("/")[0].upper()      # 'UBTC/USDC' -> 'UBTC'; 'HYPE/USDC' -> 'HYPE'
        if "/" in symbol:
            return token                          # readable pair already names the token
        if token.startswith("U") and len(token) > 1 and token not in HYPERLIQUID_NATIVE_SPOT:
            return token                          # a Unit token ('UBTC') passed directly
        return hyperliquid_spot_token(token)      # 'BTC' -> 'UBTC', 'HYPE' -> 'HYPE'

    def _resolve_spot(self, symbol: str) -> tuple[str, int]:
        """Resolve a spot symbol to (canonical_pair_name, base_szDecimals).

        Looks up `spotMeta`: find the base token named 'U'+base and the USDC quote
        token, then the universe pair holding exactly those two tokens. The pair's
        `name` (e.g. '@142') is the order coin; sz decimals come from the base
        token. Raises if the Unit pair isn't listed."""
        base_name = self._spot_base_token(symbol)
        sm = self._info.spot_meta()
        tokens = {t["index"]: t for t in sm.get("tokens", [])}
        name_to_index = {t["name"].upper(): t["index"] for t in sm.get("tokens", [])}
        if base_name.upper() not in name_to_index:
            raise RuntimeError(f"Hyperliquid spot: token not found: {base_name}")
        base_idx = name_to_index[base_name.upper()]
        quote_idx = name_to_index.get(HYPERLIQUID_SPOT_QUOTE.upper())
        for pair in sm.get("universe", []):
            pt = pair.get("tokens", [])
            if len(pt) == 2 and pt[0] == base_idx and (quote_idx is None or pt[1] == quote_idx):
                sz = int(tokens.get(base_idx, {}).get("szDecimals", 4))
                return pair["name"], sz
        raise RuntimeError(f"Hyperliquid spot: no {base_name}/{HYPERLIQUID_SPOT_QUOTE} pair listed")

    def spot_market_order(self, symbol: str, side: Side, qty: float) -> OrderResult:
        """Place an immediate-fill HL Unit spot order.

        HL has no dedicated "market" endpoint: the SDK's ``market_open`` is an
        aggressive **IOC limit** at the slippage-adjusted mid, which routes to the
        spot asset automatically (spot ids >= 10000). We reuse it for spot.

        HL spot BUY fees are charged in the BASE coin received (PURR / HYPE / UBTC),
        so `filled_qty_base` is NET of the fee (like Bybit); a SELL fee is USDC-
        denominated, so the base sold is gross. `commission_asset` is surfaced either
        way. DRY_RUN (or no key) short-circuits to a simulated result with NO network,
        mirroring the perp path."""
        try:
            pair_name, sz_decimals = (
                self._resolve_spot(symbol) if not (self.dry_run or self._exchange is None)
                # In DRY_RUN we still resolve sz decimals offline-safe via canonical map.
                else (symbol, None)
            )
            qty_base = (round(qty, sz_decimals) if sz_decimals is not None
                        else round(qty, _canonical_spot_decimals(symbol)))
            if qty_base <= 0:
                return OrderResult(
                    success=False,
                    error_message=f"spot quantity rounded to 0 (requested={qty}, symbol={symbol})",
                )

            if self.dry_run or self._exchange is None:
                log.info("[DRY_RUN] hyperliquid spot %s %s qty=%s", side, symbol, qty_base)
                return OrderResult(success=True, exchange_order_id="DRY_RUN",
                                   filled_qty_base=qty_base, avg_price=0.0)

            try:
                price = self._mid_price(pair_name)
            except Exception as e:
                log.warning("HL spot mid lookup failed (continuing): %s", e)
                price = 0.0

            resp = self._exchange.market_open(
                name=pair_name,
                is_buy=(side == "buy"),
                sz=qty_base,
                px=None,
                slippage=0.01,
            )
            if resp.get("status") != "ok":
                return OrderResult(success=False, error_message=str(resp), raw=resp)

            statuses = (resp.get("response") or {}).get("data", {}).get("statuses") or []
            oid, filled, avg_px = "", 0.0, price
            for st in statuses:
                if "filled" in st:
                    f = st["filled"]
                    oid = str(f.get("oid", ""))
                    filled += float(f.get("totalSz", 0))
                    avg_px = float(f.get("avgPx", price))
                elif "error" in st:
                    return OrderResult(success=False, error_message=str(st["error"]), raw=resp)

            commission, commission_asset = 0.0, ""
            try:
                commission, commission_asset = self._fill_fee(oid)
            except Exception as e:
                log.warning("HL spot fee enrichment failed (continuing): %s", e)

            # HL spot BUY fees are charged in the BASE coin received (e.g. PURR / HYPE /
            # UBTC), so the actually-held, hedgeable base is NET of the fee — mirror the
            # Bybit base-fee netting. A SELL fee is USDC-denominated, so the base sold is
            # gross (unaffected). Guarded by the fee asset matching the leg's base token.
            net_filled = filled or qty_base
            base_tok = self._spot_base_token(symbol)
            if (side == "buy" and commission > 0 and commission_asset
                    and commission_asset.upper() == base_tok.upper()):
                net_filled = max(0.0, net_filled - commission)

            return OrderResult(
                success=True,
                exchange_order_id=oid,
                filled_qty_base=net_filled,
                avg_price=avg_px,
                commission=commission,
                commission_asset=commission_asset,
                raw=resp,
            )
        except Exception as e:
            log.exception("Hyperliquid spot_market_order failed")
            return OrderResult(success=False, error_message=f"{type(e).__name__}: {e}")

    def get_spot_balance(self, base_asset: str) -> float:
        """FREE Unit-spot balance of `base_asset` (UBTC/UETH/USOL), in base units:
        `total - hold` from `spotClearinghouseState`. 0.0 if flat/unknown."""
        if not self._account_address:
            return 0.0
        token = self._spot_base_token(base_asset)
        try:
            state = self._info.spot_user_state(self._account_address)
        except Exception as e:
            log.warning("HL get_spot_balance failed for %s: %s", base_asset, e)
            return 0.0
        for bal in state.get("balances", []):
            if str(bal.get("coin", "")).upper() == token.upper():
                total = float(bal.get("total", 0) or 0)
                hold = float(bal.get("hold", 0) or 0)
                return max(0.0, total - hold)
        return 0.0

    def get_spot_step_size(self, symbol: str) -> float:
        """HL Unit-spot grid (10**-szDecimals of the base token). Falls back to a
        canonical map when meta is unavailable (DRY_RUN/tests)."""
        try:
            _, sz = self._resolve_spot(symbol)
            return 10 ** -sz
        except Exception as e:
            log.warning("HL get_spot_step_size failed for %s (canonical): %s", symbol, e)
        return 10 ** -_canonical_spot_decimals(symbol)

    def get_spot_min_notional(self, symbol: str) -> float:
        """Hyperliquid enforces a global $10 minimum order value (spot too)."""
        return 10.0
