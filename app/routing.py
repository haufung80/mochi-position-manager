from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path
import yaml

log = logging.getLogger(__name__)

# Supported exchanges with their canonical USD-denominated perp symbol format.
# Keyed by exchange name; value is the quote suffix appended to the base asset.
EXCHANGE_QUOTE_SUFFIX: dict[str, str] = {
    "hyperliquid": "",       # HL: bare base ticker  (BTC, ETH, SOL)
    "bybit": "USDT",         # Bybit linear perp     (BTCUSDT, ETHUSDT)
}
SUPPORTED_EXCHANGES = tuple(EXCHANGE_QUOTE_SUFFIX.keys())


def symbol_for(exchange: str, base_asset: str) -> str:
    """Return the exchange-native perp symbol for a base asset.

    Examples:
      symbol_for("hyperliquid", "BTC") -> "BTC"
      symbol_for("bybit",       "BTC") -> "BTCUSDT"
    """
    if exchange not in EXCHANGE_QUOTE_SUFFIX:
        raise ValueError(f"unsupported exchange: {exchange}")
    return f"{base_asset}{EXCHANGE_QUOTE_SUFFIX[exchange]}"


@dataclass(frozen=True)
class VenueRoute:
    """Per-venue resolved route. One of these per (strategy × enabled exchange)."""
    exchange: str
    symbol: str               # exchange-native, derived from base_asset
    enabled: bool
    quantity_usd: float       # inherited from parent strategy

    @property
    def leverage(self) -> float:
        """Hardcoded to 1.0 — the middleware does not configure leverage;
        the exchange's default margin mode applies."""
        return 1.0


@dataclass(frozen=True)
class StrategyRoute:
    """One TradingView alert → one StrategyRoute → fans out to N venues.

    YAML schema:

        strategies:
          MR_VOTING_BTC_6H:
            base_asset: BTC
            quantity_usd: 20
            venues:
              hyperliquid: true
              bybit: false
    """
    strategy_id: str
    base_asset: str
    quantity_usd: float
    venues: tuple[VenueRoute, ...]

    def enabled_venues(self) -> tuple[VenueRoute, ...]:
        return tuple(v for v in self.venues if v.enabled)

    @property
    def enabled(self) -> bool:
        """A strategy is considered enabled if any of its venues is enabled."""
        return any(v.enabled for v in self.venues)


def _build_strategy(sid: str, cfg: dict) -> StrategyRoute:
    """Parse a single strategy entry from YAML. Raises ValueError on bad input."""
    if not isinstance(cfg, dict):
        raise ValueError(f"entry must be a dict, got {type(cfg).__name__}")

    base = str(cfg["base_asset"]).strip().upper()
    if not base:
        raise ValueError("base_asset is required")

    qty = float(cfg["quantity_usd"])
    if qty <= 0:
        raise ValueError(f"quantity_usd must be > 0 (got {qty})")

    venues_cfg = cfg.get("venues") or {}
    if not isinstance(venues_cfg, dict):
        raise ValueError(f"venues must be a dict, got {type(venues_cfg).__name__}")

    venues: list[VenueRoute] = []
    for ex_name, raw in venues_cfg.items():
        ex = str(ex_name).lower()
        if ex not in SUPPORTED_EXCHANGES:
            log.warning("strategy %s: skipping unknown exchange '%s'", sid, ex)
            continue
        # Accept either `bool` or `{enabled: bool}` for forward compat
        if isinstance(raw, bool):
            enabled = raw
        elif isinstance(raw, dict):
            enabled = bool(raw.get("enabled", True))
        else:
            log.warning("strategy %s: venue %s: bad value %r — treating as disabled",
                        sid, ex, raw)
            enabled = False
        venues.append(VenueRoute(
            exchange=ex,
            symbol=symbol_for(ex, base),
            enabled=enabled,
            quantity_usd=qty,
        ))

    if not venues:
        raise ValueError("at least one supported venue must be declared")

    return StrategyRoute(
        strategy_id=sid,
        base_asset=base,
        quantity_usd=qty,
        venues=tuple(venues),
    )


class StrategyRouter:
    """Loads strategies from a YAML file. Resilient to bad input — individual
    bad entries are logged and skipped, never fatal at startup."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._routes: dict[str, StrategyRoute] = {}
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    def reload(self) -> None:
        self._routes = {}
        if not self._path.exists():
            log.warning("strategies file not found: %s — router empty", self._path)
            return
        try:
            with self._path.open("r") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            log.error("strategies file is invalid YAML: %s — router empty: %s",
                      self._path, e)
            return

        raw = data.get("strategies")
        if raw is None:
            log.warning("strategies file has no 'strategies:' key — router empty")
            return
        if not isinstance(raw, dict):
            log.error("strategies must be a dict keyed by strategy_id (got %s) — router empty",
                      type(raw).__name__)
            return

        loaded: dict[str, StrategyRoute] = {}
        for sid, cfg in raw.items():
            try:
                loaded[sid] = _build_strategy(sid, cfg)
            except (KeyError, ValueError, TypeError) as e:
                log.error("skipping strategy '%s': %s", sid, e)
        self._routes = loaded
        log.info("router reloaded: %d strategies from %s",
                 len(self._routes), self._path)

    def get(self, strategy_id: str) -> StrategyRoute | None:
        return self._routes.get(strategy_id)

    def all(self) -> list[StrategyRoute]:
        return list(self._routes.values())
