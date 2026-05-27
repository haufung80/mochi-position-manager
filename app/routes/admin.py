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
from ..routing import SUPPORTED_EXCHANGES

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
    if not path.exists():
        return {"strategies": {}}
    try:
        with path.open("r") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        log.warning("strategies.yaml is malformed (%s) — starting fresh", e)
        return {"strategies": {}}
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
            "supported_exchanges": SUPPORTED_EXCHANGES,
            "strategies_file": settings.strategies_file,
        },
    )


@router.post("/strategies", response_class=HTMLResponse)
async def save_strategy(request: Request):
    """Upsert a strategy. Form fields:
        secret, strategy_id, base_asset, quantity_usd, venue_<exchange> (checkbox)
    """
    form = await request.form()
    secret = str(form.get("secret", ""))
    _require_secret(secret)

    sid = str(form.get("strategy_id", "")).strip()
    if not sid or not sid.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "strategy_id must be alphanumeric (plus _ and -)")

    base = str(form.get("base_asset", "")).strip().upper()
    if not base or not base.isalnum():
        raise HTTPException(400, "base_asset must be alphanumeric (e.g. BTC)")

    try:
        qty = float(form.get("quantity_usd", "0"))
    except (TypeError, ValueError):
        raise HTTPException(400, "quantity_usd must be a number")
    if qty <= 0:
        raise HTTPException(400, "quantity_usd must be > 0")

    # Collect venue toggles. Checkbox unchecked == field absent.
    venues_cfg: dict[str, bool] = {}
    any_enabled = False
    for ex in SUPPORTED_EXCHANGES:
        field = f"venue_{ex}"
        enabled = str(form.get(field, "")).lower() in ("on", "true", "1", "yes")
        venues_cfg[ex] = enabled
        if enabled:
            any_enabled = True

    if not any_enabled:
        # Allow saving with all-disabled (useful to keep the row but pause it),
        # but warn via log so it's not silent.
        log.warning("admin: strategy %s saved with all venues disabled", sid)

    settings = get_settings()
    path = Path(settings.strategies_file)
    data = _load_yaml(path)
    strategies = data.setdefault("strategies", {})
    is_update = sid in strategies
    strategies[sid] = {
        "base_asset": base,
        "quantity_usd": qty,
        "venues": venues_cfg,
    }
    _save_yaml(path, data)
    request.app.state.strategy_router.reload()
    log.info("admin: %s strategy %s base=%s qty=$%s venues=%s",
             "updated" if is_update else "created",
             sid, base, qty, venues_cfg)
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


@router.post("/strategies/toggle/{sid}/{exchange}", response_class=HTMLResponse)
def toggle_venue(sid: str, exchange: str, request: Request,
                 secret: str = Form(...)):
    """Quick toggle: flip a single venue's enabled bit without re-entering the
    full strategy form."""
    _require_secret(secret)
    if exchange not in SUPPORTED_EXCHANGES:
        raise HTTPException(400, f"unsupported exchange: {exchange}")
    settings = get_settings()
    path = Path(settings.strategies_file)
    data = _load_yaml(path)
    strategies = data.get("strategies", {})
    if sid not in strategies:
        raise HTTPException(404, f"strategy_id not found: {sid}")
    venues = strategies[sid].setdefault("venues", {})
    current = venues.get(exchange, False)
    if isinstance(current, dict):
        venues[exchange]["enabled"] = not bool(current.get("enabled", False))
        new_val = venues[exchange]["enabled"]
    else:
        venues[exchange] = not bool(current)
        new_val = venues[exchange]
    _save_yaml(path, data)
    request.app.state.strategy_router.reload()
    log.info("admin: toggled %s/%s -> %s", sid, exchange, new_val)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/reload-strategies")
def reload_strategies(request: Request, secret: str = Form(...)):
    _require_secret(secret)
    request.app.state.strategy_router.reload()
    return {"status": "ok", "count": len(request.app.state.strategy_router.all())}
