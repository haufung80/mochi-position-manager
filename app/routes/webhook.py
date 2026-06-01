"""POST /webhook/tradingview — the only inbound endpoint.

The handler must answer FAST: TradingView's webhook delivery times out after
a few seconds, so a slow response is reported as a failure even when the order
actually filled. We therefore do only the cheap, must-be-synchronous work in
the request path — parse, authorise, dedup-gate, and persist the alert (all
local, sub-millisecond) — and hand the slow exchange round-trips to a
background task. TradingView gets an immediate "accepted"; the orders fill a
beat later and show up in the logs / dashboard exactly as before.

Why background tasks (not inline): the exchange SDKs (pybit, hyperliquid) make
blocking HTTP calls. Run inline in this async handler they (a) make the
response too slow for TradingView and (b) freeze the event loop. FastAPI runs
*sync* background functions in a threadpool, which solves both.
"""
from __future__ import annotations
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
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


def _run_fan_out(alert_id: int, route, quantity: float) -> None:
    """Runs AFTER the HTTP response is sent (FastAPI BackgroundTask → threadpool,
    so the blocking exchange SDK calls never touch the event loop).

    One independent transaction per venue, so a failure on one exchange can't
    roll back another's fill. `execute_order` persists each Order, drives it
    through success / retrying / dead, and emits its own notifier events.
    """
    for venue in route.enabled_venues():
        try:
            with session_scope() as db:
                alert = db.get(Alert, alert_id)
                if alert is None:
                    log.error("background fan-out: alert %s vanished", alert_id)
                    return
                execute_order(db, alert, venue, quantity=quantity)
        except Exception:
            log.exception("fan-out: order failed alert=%s ex=%s",
                          alert_id, venue.exchange)


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request,
                              background_tasks: BackgroundTasks,
                              settings: Settings = Depends(get_settings)):
    body = await _parse_body(request)
    alert = _validate(body)
    source_ip = request.client.host if request.client else ""
    _authorise(alert, settings.webhook_secret, source_ip)

    key = idempotency_key(alert)
    route = request.app.state.strategy_router.get(alert.strategy_id)
    notifier = get_notifier()

    # --- fast + synchronous: dedup-gate + persist the alert in a short txn ---
    try:
        with session_scope() as db:
            alert_row = _persist_alert(db, alert, body, key, source_ip)
            alert_id = alert_row.id
    except IntegrityError:
        log.info("Duplicate alert ignored key=%s strat=%s", key, alert.strategy_id)
        background_tasks.add_task(notifier.duplicate_alert, alert.strategy_id, key)
        return {"status": "duplicate", "idempotency_key": key}

    if route is None:
        background_tasks.add_task(notifier.unknown_strategy, alert.strategy_id)
        return {"status": "skipped", "reason": "unknown_strategy",
                "alert_id": alert_id, "idempotency_key": key}

    if not route.enabled_venues():
        background_tasks.add_task(notifier.disabled_strategy, alert.strategy_id)
        return {"status": "skipped", "reason": "no_enabled_venues",
                "alert_id": alert_id, "idempotency_key": key,
                "strategy_id": alert.strategy_id}

    # --- slow exchange round-trips happen AFTER we answer TradingView ---
    background_tasks.add_task(_run_fan_out, alert_id, route, alert.quantity or 0.0)
    return {
        "status": "accepted",
        "alert_id": alert_id,
        "idempotency_key": key,
        "strategy_id": alert.strategy_id,
        "venues": [v.exchange for v in route.enabled_venues()],
    }
