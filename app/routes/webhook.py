"""POST /webhook/tradingview — the only inbound endpoint.

The handler is intentionally thin; orchestration steps are broken out into
small helpers so each concern (auth, persist alert, fan out) is testable
and readable in isolation.
"""
from __future__ import annotations
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from ..config import Settings, get_settings
from ..db import session_scope
from ..dedup import idempotency_key
from ..executor import execute_order
from ..models import Alert
from ..notifier import get_notifier
from ..schemas import TradingViewAlert

log = logging.getLogger(__name__)
router = APIRouter()


async def _parse_body(request: Request) -> dict:
    """TradingView sometimes sets content-type to text/plain. Tolerate both."""
    if "application/json" in request.headers.get("content-type", ""):
        return await request.json()
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid json: {e}") from e


def _validate(body: dict) -> TradingViewAlert:
    try:
        return TradingViewAlert(**body)
    except Exception as e:
        log.warning("Rejected payload (schema): %s :: body=%s", e, body)
        raise HTTPException(status_code=422, detail=f"schema: {e}") from e


def _authorise(alert: TradingViewAlert, secret: str, source_ip: str) -> None:
    if alert.secret != secret:
        log.warning("Rejected payload: bad secret (ip=%s strat=%s)",
                    source_ip, alert.strategy_id)
        raise HTTPException(status_code=401, detail="bad secret")


def _persist_alert(db, alert: TradingViewAlert, body: dict,
                   key: str, source_ip: str) -> Alert:
    row = Alert(
        idempotency_key=key,
        strategy_id=alert.strategy_id,
        action=alert.action,
        raw_payload=json.dumps(body),
        source_ip=source_ip,
    )
    db.add(row)
    db.flush()  # raises IntegrityError on duplicate
    return row


def _fan_out(db, alert_row: Alert, alert: TradingViewAlert,
             route) -> list[dict[str, Any]]:
    """Place one order per enabled venue. Returns per-venue summaries."""
    summaries = []
    for venue in route.enabled_venues():
        order = execute_order(
            db, alert_row, venue,
            quantity=alert.quantity or 0.0,
        )
        summaries.append({
            "exchange": venue.exchange,
            "symbol": venue.symbol,
            "order_id": order.id,
            "qty_base": order.qty_base,
            "status": order.status,
            "attempts": order.attempts,
        })
    return summaries


def _overall_status(order_summaries: list[dict[str, Any]]) -> str:
    """'accepted' if any venue succeeded or is retrying; 'all_failed' otherwise."""
    statuses = {s["status"] for s in order_summaries}
    return "accepted" if (statuses & {"success", "retrying"}) else "all_failed"


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request,
                              settings: Settings = Depends(get_settings)):
    body = await _parse_body(request)
    alert = _validate(body)
    source_ip = request.client.host if request.client else ""
    _authorise(alert, settings.webhook_secret, source_ip)

    key = idempotency_key(alert)
    route = request.app.state.strategy_router.get(alert.strategy_id)
    notifier = get_notifier()

    try:
        with session_scope() as db:
            alert_row = _persist_alert(db, alert, body, key, source_ip)

            if route is None:
                notifier.unknown_strategy(alert.strategy_id)
                return {"status": "skipped", "reason": "unknown_strategy",
                        "alert_id": alert_row.id, "idempotency_key": key}

            if not route.enabled_venues():
                notifier.disabled_strategy(alert.strategy_id)
                return {"status": "skipped", "reason": "no_enabled_venues",
                        "alert_id": alert_row.id, "idempotency_key": key,
                        "strategy_id": alert.strategy_id}

            summaries = _fan_out(db, alert_row, alert, route)
            return {
                "status": _overall_status(summaries),
                "alert_id": alert_row.id,
                "idempotency_key": key,
                "strategy_id": alert.strategy_id,
                "orders": summaries,
            }

    except IntegrityError:
        log.info("Duplicate alert ignored key=%s strat=%s",
                 key, alert.strategy_id)
        notifier.duplicate_alert(alert.strategy_id, key)
        return {"status": "duplicate", "idempotency_key": key}
