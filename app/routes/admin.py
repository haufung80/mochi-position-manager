"""HTML admin endpoints for managing strategies.

Every write requires the webhook secret submitted via a form field. We
reuse WEBHOOK_SECRET instead of a separate admin password — one less env
var to manage; rotate via `fly secrets set WEBHOOK_SECRET=...` and update
the TradingView alert body to match.

Persistence is delegated to `strategy_store`. This module only handles
HTTP shape, validation, and router-reload triggering.
"""
from __future__ import annotations
import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import strategy_store
from ..config import get_settings
from ..exchanges.symbols import SUPPORTED_BASE_ASSETS, SUPPORTED_EXCHANGES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


# ---------- helpers ----------

def _require_secret(secret: str) -> None:
    expected = get_settings().webhook_secret
    if not secret or not expected or secret != expected:
        raise HTTPException(status_code=401, detail="bad secret")


def _strategies_path() -> Path:
    return Path(get_settings().strategies_file)


def _reload_router(request: Request) -> None:
    request.app.state.strategy_router.reload()


def _validate_strategy_id(sid: str) -> str:
    sid = sid.strip()
    if not sid or not sid.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "strategy_id must be alphanumeric (plus _ and -)")
    return sid


def _validate_base_asset(asset: str) -> str:
    asset = asset.strip().upper()
    if asset not in SUPPORTED_BASE_ASSETS:
        raise HTTPException(
            400,
            f"base_asset must be one of {', '.join(SUPPORTED_BASE_ASSETS)}",
        )
    return asset


# ---------- endpoints ----------

@router.get("/strategies", response_class=HTMLResponse)
def strategies_page(request: Request):
    return templates.TemplateResponse(
        "admin_strategies.html",
        {
            "request": request,
            "routes": request.app.state.strategy_router.all(),
            "supported_base_assets": SUPPORTED_BASE_ASSETS,
            "supported_exchanges": SUPPORTED_EXCHANGES,
            "strategies_file": get_settings().strategies_file,
        },
    )


@router.post("/strategies", response_class=HTMLResponse)
async def save_strategy(request: Request):
    """Upsert a strategy. Form fields:
        secret, strategy_id, base_asset, venue_<exchange> (checkbox)
    """
    form = await request.form()
    _require_secret(str(form.get("secret", "")))
    sid = _validate_strategy_id(str(form.get("strategy_id", "")))
    base = _validate_base_asset(str(form.get("base_asset", "")))

    venues = {
        ex: str(form.get(f"venue_{ex}", "")).lower() in ("on", "true", "1", "yes")
        for ex in SUPPORTED_EXCHANGES
    }
    if not any(venues.values()):
        # All-off is allowed (pause without losing config); log so it's not silent.
        log.warning("admin: strategy %s saved with all venues disabled", sid)

    is_update = strategy_store.upsert_strategy(
        _strategies_path(), sid, base_asset=base, venues=venues,
    )
    _reload_router(request)
    log.info("admin: %s strategy %s base=%s venues=%s",
             "updated" if is_update else "created", sid, base, venues)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/strategies/delete/{sid}", response_class=HTMLResponse)
def delete_strategy(sid: str, request: Request, secret: str = Form(...)):
    _require_secret(secret)
    if not strategy_store.delete_strategy(_strategies_path(), sid):
        raise HTTPException(404, f"strategy_id not found: {sid}")
    _reload_router(request)
    log.info("admin: deleted strategy %s", sid)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/strategies/toggle/{sid}/{exchange}", response_class=HTMLResponse)
def toggle_venue(sid: str, exchange: str, request: Request,
                 secret: str = Form(...)):
    """Flip a single venue's enabled bit."""
    _require_secret(secret)
    if exchange not in SUPPORTED_EXCHANGES:
        raise HTTPException(400, f"unsupported exchange: {exchange}")
    new_val = strategy_store.toggle_venue(_strategies_path(), sid, exchange)
    if new_val is None:
        raise HTTPException(404, f"strategy_id not found: {sid}")
    _reload_router(request)
    log.info("admin: toggled %s/%s -> %s", sid, exchange, new_val)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/reload-strategies")
def reload_strategies(request: Request, secret: str = Form(...)):
    _require_secret(secret)
    _reload_router(request)
    return {"status": "ok", "count": len(request.app.state.strategy_router.all())}
