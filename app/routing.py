"""In-memory strategy router: parses YAML into immutable route objects.

The middleware's hot path (`webhook` -> `router.get(strategy_id)`) reads
from this in-memory cache. Disk I/O happens only on `reload()`, which the
admin endpoints trigger after every write.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path

from .exchanges.symbols import (
    SUPPORTED_BASE_ASSETS,  # noqa: F401  re-exported for callers
    SUPPORTED_EXCHANGES,
    symbol_for,
)
from . import strategy_store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VenueRoute:
    """One (strategy × exchange) resolved route. `symbol` is exchange-native."""
    exchange: str
    symbol: str
    enabled: bool


@dataclass(frozen=True)
class StrategyRoute:
    """One strategy fans out across N venues. Per-signal quantity is supplied
    by the TradingView alert payload, NOT stored here."""
    strategy_id: str
    base_asset: str
    venues: tuple[VenueRoute, ...]
    sar: bool = False   # sar=true: alert-driven (existing flow); sar=false: managed sizing
    # Max position size in USDT (notional) for managed (sar=false) strategies, per
    # venue. None = paper mode (min-unit orders + warning). Ignored when sar=true.
    position_size: float | None = None
    # Entry execution: "market" (default) or "limit" — a managed OPEN rests a GTC limit
    # at the alert price instead of a market order (cancelled on the close). Only affects
    # managed OPENs; CLOSEs are always market. See docs/limit-entry-plan.md.
    entry: str = "market"

    def enabled_venues(self) -> tuple[VenueRoute, ...]:
        return tuple(v for v in self.venues if v.enabled)

    @property
    def enabled(self) -> bool:
        return any(v.enabled for v in self.venues)


def _coerce_venue_enabled(value: object) -> bool:
    """Accept either a plain bool or `{enabled: bool}` shape for forward compat."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return False


def _coerce_position_size(value: object) -> float | None:
    """Parse the optional `position_size` (USDT notional). None/blank/invalid or
    non-positive → None (paper mode)."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _coerce_entry(value: object) -> str:
    """Parse the optional `entry` mode. Anything but 'limit' → 'market' (the safe default)."""
    v = str(value or "market").strip().lower()
    return v if v in ("market", "limit") else "market"


def _build_strategy(sid: str, cfg: dict) -> StrategyRoute:
    """Parse one strategy entry. Raises ValueError on any bad input."""
    if not isinstance(cfg, dict):
        raise ValueError(f"entry must be a dict, got {type(cfg).__name__}")

    base = str(cfg["base_asset"]).strip().upper()
    if base not in SUPPORTED_BASE_ASSETS:
        raise ValueError(f"base_asset '{base}' is not in {SUPPORTED_BASE_ASSETS}")

    venues_cfg = cfg.get("venues") or {}
    if not isinstance(venues_cfg, dict):
        raise ValueError(f"venues must be a dict, got {type(venues_cfg).__name__}")

    venues: list[VenueRoute] = []
    for ex_name, raw in venues_cfg.items():
        ex = str(ex_name).lower()
        if ex not in SUPPORTED_EXCHANGES:
            log.warning("strategy %s: skipping unknown exchange '%s'", sid, ex)
            continue
        venues.append(VenueRoute(
            exchange=ex,
            symbol=symbol_for(ex, base),
            enabled=_coerce_venue_enabled(raw),
        ))

    if not venues:
        raise ValueError("at least one supported venue must be declared")

    # Canonical venue order (SUPPORTED_EXCHANGES) regardless of YAML key order,
    # so the dashboard / per-strategy view / fan-out are consistent.
    venues.sort(key=lambda v: SUPPORTED_EXCHANGES.index(v.exchange))
    ps_raw = cfg.get("position_size")
    position_size = _coerce_position_size(ps_raw)
    if ps_raw not in (None, "") and position_size is None:
        log.warning("strategy %s: invalid position_size %r — running in PAPER mode", sid, ps_raw)
    return StrategyRoute(strategy_id=sid, base_asset=base, venues=tuple(venues),
                         sar=bool(cfg.get("sar", False)), position_size=position_size,
                         entry=_coerce_entry(cfg.get("entry")))


class StrategyRouter:
    """Loads strategies from disk and serves lookups. Resilient — individual
    bad entries are logged and skipped, never fatal at startup."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._routes: dict[str, StrategyRoute] = {}
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    def reload(self) -> None:
        data = strategy_store.load(self._path)
        loaded: dict[str, StrategyRoute] = {}
        for sid, cfg in data.get("strategies", {}).items():
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
