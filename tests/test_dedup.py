from app.dedup import idempotency_key
from app.schemas import TradingViewAlert


def _alert(**kw):
    base = {"secret": "x", "strategy_id": "S1", "action": "buy"}
    base.update(kw)
    return TradingViewAlert(**base)


def test_alert_id_used_when_present():
    a = _alert(alert_id="abc-123")
    assert idempotency_key(a) == "S1:abc-123"


def test_falls_back_to_hash_of_strategy_action_bartime():
    a = _alert(bar_time="2025-01-01T00:00:00Z")
    k = idempotency_key(a)
    assert k.startswith("S1:hash:")
    # same inputs → same key
    assert k == idempotency_key(_alert(bar_time="2025-01-01T00:00:00Z"))


def test_different_bar_time_yields_different_key():
    k1 = idempotency_key(_alert(bar_time="2025-01-01T00:00:00Z"))
    k2 = idempotency_key(_alert(bar_time="2025-01-01T01:00:00Z"))
    assert k1 != k2


def test_different_action_yields_different_key():
    k1 = idempotency_key(_alert(action="buy", bar_time="t"))
    k2 = idempotency_key(_alert(action="sell", bar_time="t"))
    assert k1 != k2
