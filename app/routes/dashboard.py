from __future__ import annotations
from pathlib import Path

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import network
from ..db import session_scope
from ..models import Alert, Order, Position, StrategyPosition

router = APIRouter()
_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


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
                    "net_base": net_base, "net_usd": net_usd, "configured": True})

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
    out.extend(v for _, v in sorted(orphans.items()))
    return out


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/positions", response_class=JSONResponse)
def positions_json():
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
def alerts_json(limit: int = Query(100, ge=1, le=1000)):
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
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
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
        routes = request.app.state.strategy_router.all()
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "positions": positions,
                "strategy_positions": strategy_positions,
                "alerts": recent_alerts,
                "orders": recent_orders,
                "retrying": retrying,
                "dead": dead,
                "routes": routes,
                "outbound_ip": network.get_outbound_ip(),
            },
        )


@router.get("/strategy-positions", response_class=JSONResponse)
def strategy_positions_json(request: Request):
    with session_scope() as db:
        return _strategy_positions(db, request.app.state.strategy_router.all())


@router.get("/network/egress-ip", response_class=JSONResponse)
def egress_ip(refresh: bool = False):
    """JSON endpoint for the current outbound IP. Useful for monitoring
    scripts that need to detect when the IP shifts.

    Pass ?refresh=true to bypass the 5-minute cache.
    """
    return {"egress_ip": network.get_outbound_ip(force_refresh=refresh)}


@router.post("/admin/reload-strategies")
def reload_strategies(request: Request):
    request.app.state.strategy_router.reload()
    return {"status": "ok", "count": len(request.app.state.strategy_router.all())}
