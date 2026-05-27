from __future__ import annotations
from pathlib import Path

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..db import session_scope
from ..models import Alert, Order, Position

router = APIRouter()
_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


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
                "alerts": recent_alerts,
                "orders": recent_orders,
                "retrying": retrying,
                "dead": dead,
                "routes": routes,
            },
        )


@router.post("/admin/reload-strategies")
def reload_strategies(request: Request):
    request.app.state.strategy_router.reload()
    return {"status": "ok", "count": len(request.app.state.strategy_router.all())}
