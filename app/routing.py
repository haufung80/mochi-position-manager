from dataclasses import dataclass
from pathlib import Path
import yaml


@dataclass(frozen=True)
class StrategyRoute:
    strategy_id: str
    exchange: str       # "bybit" | "hyperliquid"
    symbol: str         # exchange-native symbol
    quantity_usd: float
    leverage: float
    enabled: bool


class StrategyRouter:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._routes: dict[str, StrategyRoute] = {}
        self.reload()

    def reload(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"Strategies config not found: {self._path}")
        with self._path.open("r") as f:
            data = yaml.safe_load(f) or {}
        raw = (data.get("strategies") or {})
        routes: dict[str, StrategyRoute] = {}
        for sid, cfg in raw.items():
            ex = str(cfg["exchange"]).lower()
            if ex not in ("bybit", "hyperliquid"):
                raise ValueError(f"Strategy {sid}: unsupported exchange '{ex}'")
            routes[sid] = StrategyRoute(
                strategy_id=sid,
                exchange=ex,
                symbol=str(cfg["symbol"]),
                quantity_usd=float(cfg["quantity_usd"]),
                leverage=float(cfg.get("leverage", 1.0)),
                enabled=bool(cfg.get("enabled", True)),
            )
        self._routes = routes

    def get(self, strategy_id: str) -> StrategyRoute | None:
        return self._routes.get(strategy_id)

    def all(self) -> list[StrategyRoute]:
        return list(self._routes.values())
