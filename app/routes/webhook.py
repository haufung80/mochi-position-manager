from __future__ import annotations
import json
import logging
from fastapi import APIRouter, HTTPException, Request, Depends
from sqlalchemy.exc import IntegrityError

from ..config import get_settings, Settings
from ..db import session_scope
from ..models import Alert
from ..schemas import TradingViewAlert
from ..dedup import idempotency_key
from ..executor import execute_order
from ..notifier import get_notifier

log = logging.getLogger(__name__)
router = APIRouter()


async def _parse_body(request: Request) -> dict:
    """TradingView sometimes posts JSON with content-type text/plain. Tolerate both."""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        return await request.json()
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid json: {e}") from e


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, settings: Settings = Depends(get_settings)):
    body = await _parse_body(request)

    try:
        alert = TradingViewAlert(**body)
    except Exception as e:
        log.warning("Rejected payload (schema): %s :: body=%s", e, body)
        raise HTTPException(status_code=422, detail=f"schema: {e}") from e

    if alert.secret != settings.webhook_secret:
        log.warning("Rejected payload: bad secret (ip=%s strat=%s)",
                    request.client.host if request.client else "?", alert.strategy_id)
        raise HTTPException(status_code=401, detail="bad secret")

    router_state = request.app.state.strategy_router
    route = router_state.get(alert.strategy_id)

    key = idempotency_key(alert)
    notifier = get_notifier()
    source_ip = request.client.host if request.client else ""

    try:
        with session_scope() as db:
            row = Alert(
                idempotency_key=key,
                strategy_id=alert.strategy_id,
                action=alert.action,
                raw_payload=json.dumps(body),
                source_ip=source_ip,
            )
            db.add(row)
            db.flush()
            alert_id = row.id

            if route is None:
                notifier.unknown_strategy(alert.strategy_id)
                return {"status": "skipped", "reason": "unknown_strategy",
                        "alert_id": alert_id, "idempotency_key": key}

            enabled = route.enabled_venues()
            if not enabled:
                notifier.disabled_strategy(alert.strategy_id)
                return {"status": "skipped", "reason": "no_enabled_venues",
                        "alert_id": alert_id, "idempotency_key": key,
                        "strategy_id": alert.strategy_id}

            # Fan out: one Order per enabled venue.
            results = []
            for venue in enabled:
                order = execute_order(db, row, venue)
                results.append({
                    "exchange": venue.exchange,
                    "symbol": venue.symbol,
                    "order_id": order.id,
                    "status": order.status,
                    "attempts": order.attempts,
                })

            # Overall webhook status: 'accepted' if any venue order is non-failed,
            # 'all_failed' if every venue went straight to retrying/dead.
            statuses = {r["status"] for r in results}
            overall = "accepted" if (statuses & {"success", "retrying"}) else "all_failed"
            return {
                "status": overall,
                "alert_id": alert_id,
                "idempotency_key": key,
                "strategy_id": alert.strategy_id,
                "orders": results,
            }

    except IntegrityError:
        log.info("Duplicate alert ignored key=%s strat=%s", key, alert.strategy_id)
        notifier.duplicate_alert(alert.strategy_id, key)
        return {"status": "duplicate", "idempotency_key": key}
