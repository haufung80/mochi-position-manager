from __future__ import annotations
import logging
from ..config import get_settings
from .base import Exchange

log = logging.getLogger(__name__)


class ExchangeRegistry:
    """Lazy-loaded singleton-per-exchange wrapper. Adapters are constructed on
    first request so the app can boot even when one exchange's credentials are
    missing (useful for partial setups and tests)."""

    def __init__(self):
        self._cache: dict[str, Exchange] = {}

    def get(self, name: str) -> Exchange:
        name = name.lower()
        if name in self._cache:
            return self._cache[name]

        settings = get_settings()
        if name == "bybit":
            from .bybit import BybitExchange
            ex = BybitExchange(
                api_key=settings.bybit_api_key,
                api_secret=settings.bybit_api_secret,
                testnet=settings.bybit_testnet,
                dry_run=settings.dry_run,
            )
        elif name == "hyperliquid":
            from .hyperliquid import HyperliquidExchange
            ex = HyperliquidExchange(
                private_key=settings.hyperliquid_private_key,
                account_address=settings.hyperliquid_account_address,
                testnet=settings.hyperliquid_testnet,
                dry_run=settings.dry_run,
            )
        else:
            raise ValueError(f"Unknown exchange: {name}")

        self._cache[name] = ex
        return ex


_registry: ExchangeRegistry | None = None


def get_registry() -> ExchangeRegistry:
    global _registry
    if _registry is None:
        _registry = ExchangeRegistry()
    return _registry


def reset_registry() -> None:
    """Test hook."""
    global _registry
    _registry = None
