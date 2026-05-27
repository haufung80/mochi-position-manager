"""Admin endpoints for managing strategies via the UI.

All write actions require the WEBHOOK_SECRET (reused as admin password) to
be submitted via the form field 'secret'. We reuse the existing secret to
avoid adding another env var; trade-off is that leaking the webhook secret
also leaks admin access. Rotate via `fly secrets set WEBHOOK_SECRET=...`.
"""
from __future__ import annotations
import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _require_secret(secret: str) -> None:
    settings = get_settings()
    expected = settings.webhook_secret
    if not secret or not expected or secret != expected:
        raise HTTPException(status_code=401, detail="bad secret")


def _load_yaml(path: Path) -> dict:
    """Returns the parsed YAML or a fresh {strategies: {}} skeleton."""
    if not path.exists():
        return {"strategies": {}}
    try:
        with path.open("r") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        log.warning("strategies.yaml is malformed (%s) — starting fresh", e)
        return {"strategies": {}}
    # Heal old/wrong shapes: if 'strategies' is missing or not a dict, reset it
    if not isinstance(data.get("strategies"), dict):
        log.warning("strategies file has wrong shape; resetting to empty dict")
        data["strategies"] = {}
    return data


def _save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


@router.get("/strategies", response_class=HTMLResponse)
def strategies_page(request: Request):
    settings = get_settings()
    routes = request.app.state.strategy_router.all()
    return templates.TemplateResponse(
        "admin_strategies.html",
        {
            "request": request,
            "routes": routes,
            "strategies_file": settings.strategies_file,
            "has_secret": bool(settings.webhook_secret),
        },
    )


@router.post("/strategies", response_class=HTMLResponse)
def save_strategy(
    request: Request,
    secret: str = Form(...),
    strategy_id: str = Form(...),
    exchange: str = Form(...),
    symbol: str = Form(...),
    quantity_usd: float = Form(...),
    leverage: float = Form(1.0),
    enabled: str = Form("true"),
):
    _require_secret(secret)

    sid = strategy_id.strip()
    if not sid or not sid.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "strategy_id must be alphanumeric (plus _ and -)")
    ex = exchange.lower().strip()
    if ex not in ("bybit", "hyperliquid"):
        raise HTTPException(400, f"unsupported exchange: {exchange}")
    sym = symbol.strip()
    if not sym:
        raise HTTPException(400, "symbol is required")
    if quantity_usd <= 0:
        raise HTTPException(400, "quantity_usd must be > 0")
    if leverage <= 0:
        raise HTTPException(400, "leverage must be > 0")

    settings = get_settings()
    path = Path(settings.strategies_file)
    data = _load_yaml(path)
    strategies = data.setdefault("strategies", {})
    is_update = sid in strategies
    strategies[sid] = {
        "exchange": ex,
        "symbol": sym,
        "quantity_usd": float(quantity_usd),
        "leverage": float(leverage),
        "enabled": str(enabled).lower() in ("true", "1", "yes", "on"),
    }
    _save_yaml(path, data)
    request.app.state.strategy_router.reload()
    log.info("admin: %s strategy %s -> %s/%s qty=$%s lev=%sx",
             "updated" if is_update else "created",
             sid, ex, sym, quantity_usd, leverage)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/strategies/delete/{sid}", response_class=HTMLResponse)
def delete_strategy(sid: str, request: Request, secret: str = Form(...)):
    _require_secret(secret)
    settings = get_settings()
    path = Path(settings.strategies_file)
    data = _load_yaml(path)
    strategies = data.get("strategies", {})
    if sid not in strategies:
        raise HTTPException(404, f"strategy_id not found: {sid}")
    del strategies[sid]
    _save_yaml(path, data)
    request.app.state.strategy_router.reload()
    log.info("admin: deleted strategy %s", sid)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/reload-strategies")
def reload_strategies(request: Request, secret: str = Form(...)):
    _require_secret(secret)
    request.app.state.strategy_router.reload()
    return {"status": "ok", "count": len(request.app.state.strategy_router.all())}
