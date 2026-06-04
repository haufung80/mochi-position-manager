"""Exchange-native symbol mapping.

Each supported exchange has its own perp naming convention. This module is
the single source of truth — to add a new exchange, add a single entry to
EXCHANGE_QUOTE_SUFFIX (and an adapter under app/exchanges/).

The middleware accepts a canonical base asset in `strategies.yaml`
(BTC / ETH / SOL / BNB) and resolves the exchange-native symbol via
`symbol_for(exchange, base)`.

Examples:
    symbol_for("hyperliquid", "BTC") -> "BTC"
    symbol_for("bybit",       "BTC") -> "BTCUSDT"
"""
from __future__ import annotations

# Canonical base assets supported across all configured exchanges.
# Add new ones here ONLY if every supported exchange lists the perp.
SUPPORTED_BASE_ASSETS: tuple[str, ...] = ("BTC", "ETH", "SOL", "BNB", "XRP")

# Quote-currency suffix appended to base asset to form the exchange-native
# perp symbol. Empty string means "use the bare base ticker".
EXCHANGE_QUOTE_SUFFIX: dict[str, str] = {
    "hyperliquid": "",       # HL: bare ticker (BTC, ETH, SOL, BNB)
    "bybit":       "USDT",   # Bybit linear perp (BTCUSDT, ETHUSDT, ...)
}

SUPPORTED_EXCHANGES: tuple[str, ...] = tuple(EXCHANGE_QUOTE_SUFFIX.keys())


def symbol_for(exchange: str, base_asset: str) -> str:
    """Return the exchange-native perp symbol for a canonical base asset.

    Raises ValueError on unknown exchange or unsupported base asset, so
    misconfigurations surface loudly at strategy-load time rather than at
    order-placement time.
    """
    ex = exchange.lower()
    if ex not in EXCHANGE_QUOTE_SUFFIX:
        raise ValueError(f"unsupported exchange: {exchange}")
    base = base_asset.upper()
    if base not in SUPPORTED_BASE_ASSETS:
        raise ValueError(
            f"unsupported base_asset: {base_asset} "
            f"(must be one of {', '.join(SUPPORTED_BASE_ASSETS)})"
        )
    return f"{base}{EXCHANGE_QUOTE_SUFFIX[ex]}"


# Minimum order unit (step size) per base asset, in base-asset units. This is
# the canonical FALLBACK used for managed sizing when live exchange metadata is
# unavailable (DRY_RUN / tests). In production the adapters prefer the exchange's
# own grid (Bybit qtyStep, Hyperliquid szDecimals); these match it for the majors.
CANONICAL_STEP_SIZES: dict[str, float] = {
    "BTC": 0.001,
    "ETH": 0.01,
    "SOL": 0.1,
    "BNB": 0.01,
    "XRP": 0.1,
}


def canonical_step_size(base_asset: str) -> float:
    """Fallback step size for a canonical base asset (0.001 if unknown)."""
    return CANONICAL_STEP_SIZES.get(base_asset.upper(), 0.001)


def base_asset_of(exchange: str, symbol: str) -> str:
    """Reverse of symbol_for: strip the exchange's quote suffix to recover the
    canonical base asset (bybit 'XRPUSDT' -> 'XRP'; hyperliquid 'XRP' -> 'XRP')."""
    suffix = EXCHANGE_QUOTE_SUFFIX.get(exchange.lower(), "")
    if suffix and symbol.upper().endswith(suffix):
        return symbol[: -len(suffix)].upper()
    return symbol.upper()
