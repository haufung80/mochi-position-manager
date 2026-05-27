import hashlib
from .schemas import TradingViewAlert


def idempotency_key(alert: TradingViewAlert) -> str:
    """Stable key for deduplication.

    Prefers an explicit `alert_id` from the payload (recommended: have TV
    template `{{strategy.order.id}}` or `{{timenow}}` in there). Falls back to
    a hash of strategy_id + action + bar_time so that re-fired duplicates of
    the same bar collapse to one row."""
    if alert.alert_id:
        return f"{alert.strategy_id}:{alert.alert_id}"
    blob = f"{alert.strategy_id}|{alert.action}|{alert.bar_time or ''}".encode()
    return f"{alert.strategy_id}:hash:{hashlib.sha256(blob).hexdigest()[:16]}"
