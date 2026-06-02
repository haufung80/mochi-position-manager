from __future__ import annotations
import logging
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from .. import network
from ..config import get_settings
from ..db import session_scope
from ..models import Alert, Order, Position, StrategyPosition

log = logging.getLogger(__name__)

router = APIRouter()
_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Live trading views must never be served stale from a browser/proxy cache.
_NO_STORE = "no-store, must-revalidate"


def _display_tz():
    """Resolve the configured display timezone, falling back to UTC if the tz
    database can't find it (e.g. slim image without tzdata installed)."""
    name = get_settings().display_timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ModuleNotFoundError, ValueError, KeyError):
        log.warning("display_timezone %r unavailable; rendering times in UTC", name)
        return timezone.utc


def _fmt_when(dt, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Render a stored timestamp in the configured display timezone.

    Timestamps are written as UTC but SQLite drops tzinfo, so a naive value is
    assumed to be UTC. None renders empty (some columns are nullable)."""
    if dt is None:
        return ""
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_tz()).strftime(fmt)


templates.env.filters["when"] = _fmt_when


def _slip_bps(fill, signal, side: str = "buy"):
    """Side-adjusted execution slippage in basis points: positive = WORSE than
    the signal price (paid up on a buy / sold cheap on a sell), negative = price
    improvement. None when either price is missing/zero."""
    try:
        fill = float(fill)
        signal = float(signal)
    except (TypeError, ValueError):
        return None
    if signal <= 0 or fill <= 0:
        return None
    raw = (fill - signal) / signal * 1e4
    return raw if side == "buy" else -raw


def _fmt_fee(v) -> str:
    """Fees are small — show up to 6 dp, trailing zeros stripped (0.271500 -> '0.2715')."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "0"
    if abs(f) < 1e-12:
        return "0"
    return f"{f:.6f}".rstrip("0").rstrip(".") or "0"


templates.env.filters["slipbps"] = _slip_bps
templates.env.filters["fee"] = _fmt_fee


def _fmt_qty(v) -> str:
    """Format a base-asset quantity without trailing zeros: 0.290000 -> '0.29'.

    Float dust that rounds to zero at 8 dp (e.g. the -1e-9 residue a buy/sell
    round-trip can leave in the ledger) must render as a clean '0', never '-0'.
    """
    try:
        s = f"{float(v):.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)
    if s in ("", "-", "-0"):
        return "0"
    return s


templates.env.filters["qty"] = _fmt_qty


def _fmt_usd(v) -> str:
    """Format a USD amount at 2 dp, clamping sub-cent dust to a clean '0.00'
    so a -1e-14 residual renders '$0.00', never '$-0.00'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) < 0.005:  # rounds to 0.00 at 2 dp anyway — drop the sign
        f = 0.0
    return f"{f:.2f}"


templates.env.filters["usd"] = _fmt_usd

# A venue counts as "flat" — position fully closed, only float/rounding dust
# left in the fill-based ledger — below these thresholds. Exchange minimum order
# sizes are ~$5+ of notional, so a $1 cutoff sits safely between a real position
# and the sub-cent residue a buy/sell round-trip leaves behind. The base-qty
# guard only matters in the shouldn't-happen case of a ledger row whose
# last_price is 0 (net_usd would read 0 despite a real quantity); 1e-4 sits
# between dust and the smallest real position (~1e-3 base).
_FLAT_USD_EPS = 1.0
_FLAT_BASE_EPS = 1e-4


def _venue_flat(net_qty_base: float, net_qty_usd: float) -> bool:
    return abs(net_qty_usd) < _FLAT_USD_EPS and abs(net_qty_base) < _FLAT_BASE_EPS


# Exposed to the template so the per-symbol "Net positions" table dims flat
# rows using the SAME threshold as the per-strategy view.
templates.env.filters["isflat"] = _venue_flat


def _strategy_flat(venues: list[dict]) -> bool:
    """A strategy is flat when every venue is flat. Computed here, not in the
    template, so dust can't slip past Jinja's truthiness-based `select` filter
    (which reads a -1e-9 residual as a live position)."""
    return all(_venue_flat(v["net_qty_base"], v["net_qty_usd"]) for v in venues)


def _strategy_positions(db, routes) -> list[dict]:
    """Net position per strategy, populated from the stored StrategyPosition
    ledger but listing EVERY configured strategy — so a freshly added strategy
    shows up immediately (flat) without waiting for a fill or a sync.

    The ledger is updated on every fill and can be re-baselined to live
    exchange state via the admin "sync to exchange" action. Any ledgered
    strategy that's no longer configured is appended (marked unconfigured) so
    stray positions stay visible.
    """
    ledger = {
        (r.strategy_id, r.exchange, r.symbol): r
        for r in db.query(StrategyPosition).all()
    }
    out: list[dict] = []
    seen: set[str] = set()

    for route in routes:
        seen.add(route.strategy_id)
        venues, net_base, net_usd = [], 0.0, 0.0
        for v in route.venues:
            row = ledger.get((route.strategy_id, v.exchange, v.symbol))
            qb = row.net_qty_base if row else 0.0
            qu = row.net_qty_usd if row else 0.0
            venues.append({"exchange": v.exchange, "symbol": v.symbol,
                           "net_qty_base": qb, "net_qty_usd": qu,
                           "last_price": row.last_price if row else 0.0})
            net_base += qb
            net_usd += qu
        out.append({"strategy_id": route.strategy_id, "venues": venues,
                    "net_base": net_base, "net_usd": net_usd, "configured": True,
                    "flat": _strategy_flat(venues)})

    # Ledgered strategies that are no longer configured — keep them visible.
    orphans: dict[str, dict] = {}
    for (sid, ex, sym), row in ledger.items():
        if sid in seen:
            continue
        e = orphans.setdefault(sid, {"strategy_id": sid, "venues": [],
                                     "net_base": 0.0, "net_usd": 0.0, "configured": False})
        e["venues"].append({"exchange": ex, "symbol": sym,
                            "net_qty_base": row.net_qty_base,
                            "net_qty_usd": row.net_qty_usd, "last_price": row.last_price})
        e["net_base"] += row.net_qty_base
        e["net_usd"] += row.net_qty_usd
    for _, e in sorted(orphans.items()):
        e["flat"] = _strategy_flat(e["venues"])
        out.append(e)
    return out


def _execution_quality(db) -> dict:
    """Stats for the 'Execution quality' panel: total commission per exchange
    (all-time successful fills) and mean slippage over the most recent fills
    that recorded both a signal and a fill price."""
    fee_rows = (db.query(Order.exchange,
                         func.sum(Order.commission),
                         func.count(Order.id),
                         func.max(Order.commission_asset))
                .filter(Order.status == "success", Order.commission > 0)
                .group_by(Order.exchange).all())
    fees = sorted(
        ({"exchange": e, "total": float(t or 0.0), "n": int(n), "asset": a or ""}
         for e, t, n, a in fee_rows),
        key=lambda r: r["exchange"],
    )
    fill_rows = (db.query(Order)
                 .filter(Order.status == "success",
                         Order.signal_price.isnot(None),
                         Order.fill_price.isnot(None))
                 .order_by(Order.created_at.desc()).limit(200).all())
    slips = [s for s in (_slip_bps(o.fill_price, o.signal_price, o.side) for o in fill_rows)
             if s is not None]
    return {
        "fees_by_exchange": fees,
        "total_fees": sum(r["total"] for r in fees),
        "avg_slippage_bps": (sum(slips) / len(slips)) if slips else None,
        "slippage_sample": len(slips),
    }


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/positions", response_class=JSONResponse)
def positions_json(response: Response):
    response.headers["Cache-Control"] = _NO_STORE
    with session_scope() as db:
        rows = db.query(Position).order_by(Position.exchange, Position.symbol).all()
        return [
            {
                "exchange": p.exchange,
                "symbol": p.symbol,
                "net_qty_base": p.net_qty_base,
                "net_qty_usd": p.net_qty_usd,
                "last_price": p.last_price,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in rows
        ]


@router.get("/alerts", response_class=JSONResponse)
def alerts_json(response: Response, limit: int = Query(100, ge=1, le=1000)):
    response.headers["Cache-Control"] = _NO_STORE
    with session_scope() as db:
        rows = db.query(Alert).order_by(Alert.received_at.desc()).limit(limit).all()
        return [
            {
                "id": a.id,
                "strategy_id": a.strategy_id,
                "action": a.action,
                "idempotency_key": a.idempotency_key,
                "received_at": a.received_at.isoformat(),
                "source_ip": a.source_ip,
            }
            for a in rows
        ]


@router.get("/orders", response_class=JSONResponse)
def orders_json(
    response: Response,
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    response.headers["Cache-Control"] = _NO_STORE
    with session_scope() as db:
        q = db.query(Order).order_by(Order.created_at.desc())
        if status:
            q = q.filter(Order.status == status)
        return [
            {
                "id": o.id,
                "alert_id": o.alert_id,
                "exchange": o.exchange,
                "symbol": o.symbol,
                "side": o.side,
                "qty_usd": o.qty_usd,
                "qty_base": o.qty_base,
                "status": o.status,
                "attempts": o.attempts,
                "exchange_order_id": o.exchange_order_id,
                "signal_price": o.signal_price,
                "fill_price": o.fill_price,
                "slippage_bps": _slip_bps(o.fill_price, o.signal_price, o.side),
                "commission": o.commission,
                "commission_asset": o.commission_asset,
                "error": o.error_message,
                "next_retry_at": o.next_retry_at.isoformat() if o.next_retry_at else None,
                "created_at": o.created_at.isoformat(),
            }
            for o in q.limit(limit).all()
        ]


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as db:
        positions = db.query(Position).order_by(Position.exchange, Position.symbol).all()
        strategy_positions = _strategy_positions(db, request.app.state.strategy_router.all())
        recent_alerts = db.query(Alert).order_by(Alert.received_at.desc()).limit(25).all()
        recent_orders = db.query(Order).order_by(Order.created_at.desc()).limit(25).all()
        retrying = db.query(Order).filter(Order.status == "retrying").count()
        dead = db.query(Order).filter(Order.status == "dead").count()
        execq = _execution_quality(db)
        routes = request.app.state.strategy_router.all()
        resp = templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "positions": positions,
                "strategy_positions": strategy_positions,
                "alerts": recent_alerts,
                "orders": recent_orders,
                "retrying": retrying,
                "dead": dead,
                "execq": execq,
                "routes": routes,
                "outbound_ip": network.get_outbound_ip(),
            },
        )
        # Live trading data — never let a browser/proxy serve a stale dashboard.
        resp.headers["Cache-Control"] = _NO_STORE
        return resp


@router.get("/strategy-positions", response_class=JSONResponse)
def strategy_positions_json(request: Request, response: Response):
    response.headers["Cache-Control"] = _NO_STORE
    with session_scope() as db:
        return _strategy_positions(db, request.app.state.strategy_router.all())


@router.get("/network/egress-ip", response_class=JSONResponse)
def egress_ip(refresh: bool = False):
    """JSON endpoint for the current outbound IP. Useful for monitoring
    scripts that need to detect when the IP shifts.

    Pass ?refresh=true to bypass the 5-minute cache.
    """
    return {"egress_ip": network.get_outbound_ip(force_refresh=refresh)}
