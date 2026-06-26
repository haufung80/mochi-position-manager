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
import uuid
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import portfolio, strategy_store
from ..config import get_settings
from ..db import session_scope
from ..exchanges.registry import get_registry
from ..exchanges.symbols import SUPPORTED_BASE_ASSETS, SUPPORTED_EXCHANGES
from ..executor import execute_order, make_client_order_id
from ..models import Alert
from ..risk import get_risk_settings, update_risk_settings

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


def _position_size_warnings(request: Request, sid: str) -> list[str]:
    """Config-time notice when a strategy's position_size is outside the placeable range:
    ABOVE the per-order cap (orders will be REJECTED) or BELOW an enabled venue's exchange
    minimum (orders won't fill). Best-effort — a live min/price hiccup never blocks the
    save (the order path also rejects these with a Telegram alert at fire time)."""
    route = request.app.state.strategy_router.get(sid)
    if route is None or route.position_size is None:
        return []
    size = route.position_size
    warns: list[str] = []
    with session_scope() as db:
        cap = get_risk_settings(db).per_order_max_notional or 0.0
    if cap > 0 and size > cap:
        warns.append(f"position_size ${size:g} exceeds the per-order cap ${cap:g} — "
                     f"orders for {sid} will be REJECTED")
    for v in route.enabled_venues():
        try:
            mn = get_registry().get(v.exchange).get_min_notional(v.symbol)
        except Exception:                       # noqa: BLE001 — best-effort notice only
            continue
        if mn and size < mn:
            warns.append(f"position_size ${size:g} is below {v.exchange}'s minimum "
                         f"${mn:g} for {v.symbol} — orders won't fill")
    return warns


def _redirect_strategies(warns: list[str] | None = None) -> RedirectResponse:
    url = "/admin/strategies"
    if warns:
        url += "?" + urlencode({"warn": " · ".join(warns)})
    return RedirectResponse(url=url, status_code=303)


# ---------- endpoints ----------

@router.get("/strategies", response_class=HTMLResponse)
def strategies_page(request: Request):
    routes = request.app.state.strategy_router.all()
    with session_scope() as db:                          # copy out — row detaches on close
        rs = get_risk_settings(db)
        risk = {"per_order_max_notional": rs.per_order_max_notional,
                "kill_switch": rs.kill_switch}
    return templates.TemplateResponse(
        "admin_strategies.html",
        {
            "request": request,
            "routes": routes,
            "supported_base_assets": SUPPORTED_BASE_ASSETS,
            "supported_exchanges": SUPPORTED_EXCHANGES,
            "strategies_file": get_settings().strategies_file,
            "risk": risk,
            # Total OPEN-notional cap = Σ of the per-strategy position_size (max open per
            # strategy), per the config model. Informational.
            "total_position_size": sum(r.position_size or 0.0 for r in routes),
            "warn": request.query_params.get("warn", ""),
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
    entry = "limit" if _form_bool(form, "entry_limit") else "market"
    is_update = strategy_store.upsert_strategy(
        _strategies_path(), sid, base_asset=base, venues=venues, sar=sar,
        position_size=position_size, entry=entry,
    )
    _reload_router(request)
    log.info("admin: %s strategy %s base=%s venues=%s sar=%s size=%s entry=%s",
             "updated" if is_update else "created", sid, base, venues, sar, position_size, entry)
    return _redirect_strategies(_position_size_warnings(request, sid))


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


@router.post("/strategies/toggle-entry/{sid}", response_class=HTMLResponse)
def toggle_entry(sid: str, request: Request, secret: str = Form(...)):
    """Flip a strategy's entry mode market<->limit (affects managed OPENs only)."""
    _require_secret(secret)
    new_val = strategy_store.toggle_entry(_strategies_path(), sid)
    if new_val is None:
        raise HTTPException(404, f"strategy_id not found: {sid}")
    _reload_router(request)
    log.info("admin: toggled entry %s -> %s", sid, new_val)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/strategies/fire-limit", response_class=HTMLResponse)
def fire_limit(request: Request, secret: str = Form(...), strategy_id: str = Form(...),
               side: str = Form(...), limit_price: float = Form(...),
               fire_quantity: str = Form("")):
    """Manually place a ONE-OFF resting LIMIT order on a strategy's enabled venues at
    `limit_price`. Optional `quantity` (base units) overrides managed sizing; blank →
    size from the strategy's position_size at the limit price. Does NOT change the
    strategy's entry mode. Force-places (no open/close/pyramid logic) but still respects
    the kill-switch (halt = halt) and the per-order cap (fat-finger guard). The order is
    tracked like any limit entry (fill-poller + cancel-on-close).

    Sync endpoint → FastAPI runs it in a threadpool, so the blocking exchange calls don't
    touch the event loop; the operator gets an immediate per-venue result."""
    _require_secret(secret)
    side = side.strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be 'buy' or 'sell'")
    if limit_price <= 0:
        raise HTTPException(400, "limit_price must be > 0")
    route = request.app.state.strategy_router.get(strategy_id)
    if route is None:
        raise HTTPException(404, f"strategy_id not found: {strategy_id}")
    venues = route.enabled_venues()
    if not venues:
        return _redirect_strategies([f"{strategy_id}: no enabled venues — nothing fired"])

    qty_explicit: float | None = None
    q = fire_quantity.strip()
    if q:
        try:
            qty_explicit = float(q)
        except ValueError:
            raise HTTPException(400, "quantity must be a number (base units) or blank")
        if qty_explicit <= 0:
            raise HTTPException(400, "quantity must be > 0 (or blank for managed sizing)")

    with session_scope() as db:
        risk = get_risk_settings(db)
        kill, cap = risk.kill_switch, (risk.per_order_max_notional or 0.0)
    if kill:
        return _redirect_strategies(["global kill-switch is ON — manual fire refused"])

    # One synthetic alert ties the order(s) into the ledger / poller / cancel-on-close.
    idem = f"manual-{uuid.uuid4().hex[:12]}"
    with session_scope() as db:
        alert = Alert(idempotency_key=idem, strategy_id=strategy_id, action=side,
                      raw_payload=f'{{"manual": true, "limit_price": {limit_price}}}',
                      signal_price=limit_price,
                      source_ip=(request.client.host if request.client else ""))
        db.add(alert)
        db.flush()
        alert_id = alert.id

    notices: list[str] = []
    reg = get_registry()
    for v in venues:
        try:
            qty = qty_explicit
            if qty is None:
                if route.position_size is None:
                    notices.append(f"{v.exchange}: no position_size and no quantity — skipped")
                    continue
                step = reg.get(v.exchange).get_step_size(v.symbol)
                qty = portfolio.compute_managed_qty(route.position_size, limit_price, step)
            if qty <= 0:
                notices.append(f"{v.exchange}: sized to 0 — skipped")
                continue
            if cap > 0 and qty * limit_price > cap:
                notices.append(f"{v.exchange}: ${qty * limit_price:,.0f} exceeds per-order cap "
                               f"${cap:,.0f} — skipped")
                continue
            cloid = make_client_order_id(alert_id, v.exchange, v.symbol)
            with session_scope() as db:
                execute_order(db, db.get(Alert, alert_id), v, quantity=qty, order_type="limit",
                              limit_price=limit_price, client_order_id=cloid)
            notices.append(f"{v.exchange}: limit {side} {qty:g} @ {limit_price:g} placed")
        except Exception as e:
            log.exception("fire-limit failed strat=%s ex=%s", strategy_id, v.exchange)
            notices.append(f"{v.exchange}: error — {type(e).__name__}")
    log.info("admin: manual fire-limit %s %s @ %s -> %s", strategy_id, side, limit_price, notices)
    return _redirect_strategies(notices)


@router.post("/strategies/set-size/{sid}", response_class=HTMLResponse)
def set_position_size(sid: str, request: Request, secret: str = Form(...),
                      position_size: str = Form("")):
    """Set/clear a managed strategy's position_size inline — leaves base_asset,
    venues and SAR untouched. Blank clears it (paper mode)."""
    _require_secret(secret)
    size = _validate_position_size(position_size)
    if strategy_store.set_position_size(_strategies_path(), sid, size) is None:
        raise HTTPException(404, f"strategy_id not found: {sid}")
    _reload_router(request)
    log.info("admin: set position_size %s -> %s", sid, size)
    return _redirect_strategies(_position_size_warnings(request, sid))


@router.post("/risk", response_class=HTMLResponse)
async def save_risk(request: Request):
    """Set the global pre-trade risk controls — per-order max notional (USDT; blank/0 =
    off) and the kill-switch (halts ALL new orders). Same WEBHOOK_SECRET auth as the
    strategy writes."""
    form = await request.form()
    _require_secret(str(form.get("secret", "")))
    raw = str(form.get("per_order_max_notional", "")).strip()
    try:
        cap = float(raw) if raw else 0.0
    except ValueError:
        raise HTTPException(400, "per_order_max_notional must be a number (USDT) or blank")
    if cap < 0:
        raise HTTPException(400, "per_order_max_notional must be >= 0 (0 disables the cap)")
    kill = _form_bool(form, "kill_switch")
    with session_scope() as db:
        update_risk_settings(db, per_order_max_notional=cap, kill_switch=kill)
    log.warning("admin: risk controls updated — per_order_max=$%.2f kill_switch=%s", cap, kill)
    return RedirectResponse(url="/admin/strategies", status_code=303)


@router.post("/strategies/backfill-entries", response_class=HTMLResponse)
def backfill_entries(request: Request, secret: str = Form(...)):
    """Rebuild avg_entry_price from historical klines for open positions whose fills
    weren't recorded with a price (only when the fills explain the stored net), so
    per-strategy unrealized is real. Realized PnL is NOT touched here — it's owned by
    the live executor (reconstructing it from a truncated history fabricates profit)."""
    _require_secret(secret)
    from .. import reconcile
    result = reconcile.backfill_pnl_from_klines(request.app.state.strategy_router)

    def _fmt(u: dict) -> str:
        parts = []
        if "entry" in u:
            parts.append(f"entry {u['entry']['old']:.2f} → {u['entry']['new']:.2f}")
        return (f"<li><b>{u['strategy_id']}</b> — {u['exchange']}/{u['symbol']}: "
                f"{'; '.join(parts) or '(no change)'}</li>")

    upd = "".join(_fmt(u) for u in result["updated"]) or "<li>(none)</li>"
    skip = "".join(
        f"<li><b>{s['strategy_id']}</b> — {s['exchange']}/{s['symbol']}: {s['reason']}</li>"
        for s in result["skipped"]
    )
    skip_block = (f'<h3 style="color:#e0a030">Left untouched {len(result["skipped"])}</h3>'
                  f'<ul>{skip}</ul>' if skip else "")
    log.info("admin: backfill-entries -> %d updated, %d skipped",
             len(result["updated"]), len(result["skipped"]))
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8"><title>Backfill result</title></head>'
        '<body style="font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;'
        'padding:28px;max-width:760px;margin:auto">'
        f'<h2>Rebuilt entry price for {len(result["updated"])} position(s)</h2><ul>{upd}</ul>'
        f'{skip_block}'
        '<p style="margin-top:20px"><a href="/performance" style="color:#6cf">'
        'performance →</a></p></body></html>'
    )


@router.post("/strategies/audit", response_class=HTMLResponse)
def audit_pnl(request: Request, secret: str = Form(...)):
    """Read-only reconciliation report: per-strategy ledger vs fill-replay, and
    per-symbol ledger vs the live exchange. Surfaces PnL/position drift on demand."""
    _require_secret(secret)
    from .. import reconcile
    result = reconcile.audit_pnl(request.app.state.strategy_router)

    def _si(s: dict) -> str:
        if "issue" in s:
            return f"<li><b>{s['strategy_id']}</b> — {s['exchange']}/{s['symbol']}: {s['issue']}</li>"
        if s.get("actionable"):
            tag = ' <span style="color:#f87171">✗ actionable</span>'
        else:
            why = "first fill is a sell" if s.get("replay_suspect_truncated") else "history has unpriced fills"
            tag = (f' <span style="color:#9aa">ℹ estimate only ({why}) — replay can\'t be trusted, '
                   'not actionable</span>')
        return (f"<li><b>{s['strategy_id']}</b> — {s['exchange']}/{s['symbol']}: "
                f"realized ledger {s['ledger_realized']:.2f} vs replay-estimate {s['replay_realized']:.2f} "
                f"(Δ{s['realized_drift']:+.2f}); net ledger {s['ledger_net']:g} vs replay "
                f"{s['replay_net']:g} (Δ{s['net_drift']:+g}){tag}</li>")

    def _xd(x: dict) -> str:
        if "issue" in x:
            return f"<li><b>{x['exchange']}/{x['symbol']}</b>: {x['issue']}</li>"
        return (f"<li><b>{x['exchange']}/{x['symbol']}</b>: ledger {x['ledger_net']:g} vs exchange "
                f"{x['exchange_net']:g} (Δ{x['drift']:+g})</li>")

    si = "".join(_si(s) for s in result["strategy_issues"]) or "<li>(none)</li>"
    xd = "".join(_xd(x) for x in result["exchange_drift"]) or "<li>(none)</li>"
    banner = ('<h2 style="color:#4ade80">✓ Clean — ledger reconciles</h2>' if result["clean"]
              else '<h2 style="color:#f87171">⚠ Drift detected</h2>')
    log.info("admin: audit-pnl -> %d strategy, %d exchange issues",
             len(result["strategy_issues"]), len(result["exchange_drift"]))
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8"><title>PnL audit</title></head>'
        '<body style="font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;'
        'padding:28px;max-width:820px;margin:auto">'
        f'{banner}'
        f'<h3>Strategy ledger vs fill-replay ({len(result["strategy_issues"])})</h3><ul>{si}</ul>'
        f'<h3>Per-symbol ledger vs exchange ({len(result["exchange_drift"])})</h3><ul>{xd}</ul>'
        '<p style="margin-top:20px"><a href="/performance" style="color:#6cf">performance →</a></p>'
        '</body></html>'
    )


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
