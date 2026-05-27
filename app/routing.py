from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path
import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyRoute:
    strategy_id: str
    exchange: str       # "bybit" | "hyperliquid"
    symbol: str         # exchange-native symbol
    quantity_usd: float
    leverage: float
    enabled: bool


class StrategyRouter:
    """Loads per-strategy routing config from a YAML file.

    The YAML schema is a dict keyed by strategy_id:

        strategies:
          MR_VOTING_BTC_6H:
            exchange: hyperliquid
            symbol: BTC
            quantity_usd: 20
            leverage: 2
            enabled: true

    Resilient by design — a missing/empty/malformed file yields an empty
    router (with a log warning) rather than crashing the app on startup.
    Individual bad entries are skipped, not fatal.
    """

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
            log.error("strategies must be a dict keyed by strategy_id (got %s) "
                      "— router empty. Use the dict format, not a list.",
                      type(raw).__name__)
            return

        loaded: dict[str, StrategyRoute] = {}
        for sid, cfg in raw.items():
            try:
                if not isinstance(cfg, dict):
                    raise ValueError(f"entry must be a dict, got {type(cfg).__name__}")
                ex = str(cfg["exchange"]).lower()
                if ex not in ("bybit", "hyperliquid"):
                    raise ValueError(f"unsupported exchange '{ex}'")
                loaded[sid] = StrategyRoute(
                    strategy_id=sid,
                    exchange=ex,
                    symbol=str(cfg["symbol"]),
                    quantity_usd=float(cfg["quantity_usd"]),
                    leverage=float(cfg.get("leverage", 1.0)),
                    enabled=bool(cfg.get("enabled", True)),
                )
            except (KeyError, ValueError, TypeError) as e:
                log.error("skipping strategy '%s': %s", sid, e)
        self._routes = loaded
        log.info("router reloaded: %d strategies from %s",
                 len(self._routes), self._path)

    def get(self, strategy_id: str) -> StrategyRoute | None:
        return self._routes.get(strategy_id)

    def all(self) -> list[StrategyRoute]:
        return list(self._routes.values())
