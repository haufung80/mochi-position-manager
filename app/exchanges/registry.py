from __future__ import annotations
import logging
from ..config import get_settings
from .base import Exchange

log = logging.getLogger(__name__)


class ExchangeRegistry:
    """Lazy-loaded singleton-per-``(exchange, account)`` wrapper. Adapters are
    constructed on first request so the app can boot even when one exchange's
    credentials are missing (useful for partial setups and tests).

    ``account="default"`` is the directional book — every existing call site uses
    the one-arg ``get(name)`` form, which keeps targeting ``default`` byte-for-byte.
    ``account="arb"`` resolves the dedicated, separately-credentialed funding-arb
    sub-account so arb fills never net against the directional positions.
    """

    def __init__(self):
        # Keyed by (name, account) so BTCUSDT on the arb book never aliases the
        # directional adapter (different keys, different margin).
        self._cache: dict[tuple[str, str], Exchange] = {}

    def get(self, name: str, account: str = "default") -> Exchange:
        name = name.lower()
        account = (account or "default").lower()
        key = (name, account)
        if key in self._cache:
            return self._cache[key]

        settings = get_settings()
        # Raises clearly for an unknown exchange, an unknown account label, or a
        # known non-default account whose creds are empty.
        creds = settings.account_credentials(name, account)

        if name == "bybit":
            from .bybit import BybitExchange
            ex = BybitExchange(**creds)
        elif name == "hyperliquid":
            from .hyperliquid import HyperliquidExchange
            ex = HyperliquidExchange(**creds)
            self._guard_hyperliquid_account(account, ex, settings)
        else:
            # Unreachable: account_credentials already rejected unknown exchanges.
            raise ValueError(f"Unknown exchange: {name}")

        self._cache[key] = ex
        return ex

    @staticmethod
    def _guard_hyperliquid_account(account: str, ex, settings) -> None:
        """Construction-time isolation guard for the HL arb book.

        HL ``close_position`` closes the WHOLE coin on the account and
        ``get_funding`` is account-wide, so a non-default HL account that resolves
        to the SAME address as the directional account would nuke / double-count
        the directional HL book. Catch it here — at the only place that matters,
        before any order is ever sent — which protects both the executor and the
        funding poller with one check.
        """
        if account == "default":
            return
        directional = (settings.hyperliquid_account_address or "").lower()
        resolved = (getattr(ex, "_account_address", "") or "").lower()
        if directional and resolved and resolved == directional:
            raise ValueError(
                f"Hyperliquid arb account address ({resolved}) is the SAME as the "
                "directional HYPERLIQUID_ACCOUNT_ADDRESS. The arb book MUST be a "
                "separate account (own key + address, own margin) — sharing it "
                "would let an arb close/funding-poll hit the directional position. "
                "Set HYPERLIQUID_ARB_ACCOUNT_ADDRESS to a distinct account."
            )


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
