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


def _form_bool(form, key: str) -> bool:
    """Truthiness of an HTML form field. Unchecked checkboxes are absent from
    the payload, so a missing key reads as False."""
    return str(form.get(key, "")).lower() in ("on", "true", "1", "yes")


def _validate_position_size(raw: str) -> float | None:
    """Parse the optional position_size form field (USDT notional). Blank → None
    (paper mode). Raises 400 on a non-numeric or non-positive value."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise HTTPException(400, "position_size must be a number (USDT) or blank")
    if v <= 0:
        raise HTTPException(400, "position_size must be > 0 (or blank for paper mode)")
    return v


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

    venues = {ex: _form_bool(form, f"venue_{ex}") for ex in SUPPORTED_EXCHANGES}
    if not any(venues.values()):
        # All-off is allowed (pause without losing config); log so it's not silent.
        log.warning("admin: strategy %s saved with all venues disabled", sid)

    sar = _form_bool(form, "sar")
    position_size = _validate_position_size(str(form.get("position_size", "")))
    is_update = strategy_store.upsert_strategy(
        _strategies_path(), sid, base_asset=base, venues=venues, sar=sar,
        position_size=position_size,
    )
    _reload_router(request)
    log.info("admin: %s strategy %s base=%s venues=%s sar=%s size=%s",
             "updated" if is_update else "created", sid, base, venues, sar, position_size)
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


@router.post("/strategies/toggle-sar/{sid}", response_class=HTMLResponse)
def toggle_sar(sid: str, request: Request, secret: str = Form(...)):
    """Flip a strategy's stop-and-reverse marker (label only — no order change)."""
    _require_secret(secret)
    new_val = strategy_store.toggle_sar(_strategies_path(), sid)
    if new_val is None:
        raise HTTPException(404, f"strategy_id not found: {sid}")
    _reload_router(request)
    log.info("admin: toggled SAR %s -> %s", sid, new_val)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/reload-strategies")
def reload_strategies(request: Request, secret: str = Form(...)):
    _require_secret(secret)
    _reload_router(request)
    return {"status": "ok", "count": len(request.app.state.strategy_router.all())}


@router.post("/strategies/sync-positions", response_class=HTMLResponse)
def sync_positions(request: Request, secret: str = Form(...)):
    """Re-baseline the per-strategy ledger to live exchange positions, clearing
    stale residue. Reads live state per configured strategy/venue."""
    _require_secret(secret)
    from .. import reconcile
    result = reconcile.sync_strategy_positions(request.app.state.strategy_router)

    synced = "".join(
        f"<li><b>{s['strategy_id']}</b> — {s['exchange']}/{s['symbol']}: "
        f"{s['net_qty_base']:+.6f} (${s['net_qty_usd']:+.2f})</li>"
        for s in result["synced"]
    ) or "<li>(none)</li>"
    skipped = "".join(
        f"<li><b>{s['strategy_id']}</b> — {s['exchange']}/{s['symbol']}: {s['reason']}</li>"
        for s in result["skipped"]
    )
    skip_block = (
        f'<h3 style="color:#e0a030">Skipped {len(result["skipped"])}</h3><ul>{skipped}</ul>'
        if skipped else ""
    )
    log.info("admin: sync-positions -> %d synced, %d skipped",
             len(result["synced"]), len(result["skipped"]))
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8"><title>Sync result</title></head>'
        '<body style="font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;'
        'padding:28px;max-width:760px;margin:auto">'
        f'<h2>Synced {len(result["synced"])} position(s) to exchange</h2><ul>{synced}</ul>'
        f'{skip_block}'
        '<p style="margin-top:20px"><a href="/admin/strategies" style="color:#6cf">'
        '&larr; strategies</a> &nbsp;&middot;&nbsp; '
        '<a href="/" style="color:#6cf">dashboard &rarr;</a></p></body></html>'
    )
