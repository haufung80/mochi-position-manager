"""Persistent storage for strategy configurations (YAML on disk).

Pure file I/O — no FastAPI, no SQLAlchemy. Anything that reads or writes
strategies.yaml goes through here so the on-disk shape is single-sourced.

YAML schema:

    strategies:
      MR_VOTING_BTC_6H:
        base_asset: BTC          # canonical ticker (BTC / ETH / SOL / BNB)
        sar: false               # stop-and-reverse marker (optional; label only)
        venues:
          hyperliquid: true      # symbol resolved at runtime via symbol_for()
          bybit: false

Per-signal order size is NOT stored here — TradingView's alert payload
carries `quantity` (in base-asset units, e.g. 0.001 BTC), letting your
pine-script sizing logic drive size per signal.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def load(path: Path) -> dict[str, Any]:
    """Return the parsed YAML or a fresh {strategies: {}} skeleton.

    Resilient: missing file, empty file, or malformed YAML all yield the
    empty skeleton + a log entry. Callers should never see exceptions
    from disk-level problems.
    """
    if not path.exists():
        return {"strategies": {}}
    try:
        with path.open("r") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        log.warning("%s is malformed YAML (%s) — returning empty skeleton", path, e)
        return {"strategies": {}}
    if not isinstance(data, dict):
        log.warning("%s root is not a mapping (got %s) — returning empty skeleton",
                    path, type(data).__name__)
        return {"strategies": {}}
    if not isinstance(data.get("strategies"), dict):
        log.warning("%s 'strategies' key missing or not a dict — resetting", path)
        data["strategies"] = {}
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    """Atomically rewrite the YAML file with the given dict."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file then rename — avoids leaving the file
    # half-written if the process is killed mid-write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    tmp.replace(path)


def upsert_strategy(path: Path, strategy_id: str, *,
                    base_asset: str, venues: dict[str, bool],
                    sar: bool = False,
                    position_size: float | None = None) -> bool:
    """Insert or update a single strategy entry. Returns True if it was an
    update (i.e. existed before), False if newly created."""
    data = load(path)
    strategies = data.setdefault("strategies", {})
    is_update = strategy_id in strategies
    entry: dict = {"base_asset": base_asset, "sar": bool(sar)}
    if position_size is not None:
        entry["position_size"] = float(position_size)
    entry["venues"] = dict(venues)
    strategies[strategy_id] = entry
    save(path, data)
    return is_update


def delete_strategy(path: Path, strategy_id: str) -> bool:
    """Remove a strategy. Returns True if deleted, False if it didn't exist."""
    data = load(path)
    strategies = data.get("strategies", {})
    if strategy_id not in strategies:
        return False
    del strategies[strategy_id]
    save(path, data)
    return True


def toggle_venue(path: Path, strategy_id: str, exchange: str) -> bool | None:
    """Flip the enabled bit of one venue. Returns the new state, or None
    if the strategy doesn't exist."""
    data = load(path)
    strategies = data.get("strategies", {})
    if strategy_id not in strategies:
        return None
    venues = strategies[strategy_id].setdefault("venues", {})
    current = venues.get(exchange, False)
    # Tolerate the dict-with-enabled shape too (forward compat).
    if isinstance(current, dict):
        new_val = not bool(current.get("enabled", False))
        venues[exchange]["enabled"] = new_val
    else:
        new_val = not bool(current)
        venues[exchange] = new_val
    save(path, data)
    return new_val


def toggle_sar(path: Path, strategy_id: str) -> bool | None:
    """Flip a strategy's stop-and-reverse marker. Returns the new state, or
    None if the strategy doesn't exist."""
    data = load(path)
    strategies = data.get("strategies", {})
    if strategy_id not in strategies:
        return None
    new_val = not bool(strategies[strategy_id].get("sar", False))
    strategies[strategy_id]["sar"] = new_val
    save(path, data)
    return new_val


def set_position_size(path: Path, strategy_id: str, value: float | None) -> bool | None:
    """Set (or clear, when value is None → paper mode) a strategy's position_size
    IN PLACE — base_asset / venues / sar are untouched. Returns True on success,
    or None if the strategy doesn't exist."""
    data = load(path)
    strategies = data.get("strategies", {})
    if strategy_id not in strategies:
        return None
    if value is None:
        strategies[strategy_id].pop("position_size", None)
    else:
        strategies[strategy_id]["position_size"] = float(value)
    save(path, data)
    return True
