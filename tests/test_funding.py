"""Funding: idempotent poller storage + single-owner attribution helper."""
from __future__ import annotations

from app.db import session_scope
from app.funding_worker import poll_once
from app.models import FundingEvent
from app.reconcile import single_owner_map
from app.routing import StrategyRouter


def test_single_owner_map_excludes_shared(strategies_yaml):
    owners = single_owner_map(StrategyRouter(strategies_yaml))
    assert owners[("bybit", "BTCUSDT")] == "TEST_BTC"      # solely owned
    # ETHUSDT is claimed by TEST_MULTI + TEST_DISABLED -> not attributable
    assert ("bybit", "ETHUSDT") not in owners


def test_poll_once_stores_and_is_idempotent(strategies_yaml, stub_exchange):
    router = StrategyRouter(strategies_yaml)
    stub_exchange.funding["BTCUSDT"] = [
        {"time_ms": 1_700_000_000_000, "amount": -0.5},
        {"time_ms": 1_700_028_800_000, "amount": 0.3},
    ]
    assert poll_once(router) == 2          # both stored
    assert poll_once(router) == 0          # same window -> no duplicates

    with session_scope() as db:
        amounts = sorted(
            r.amount for r in
            db.query(FundingEvent).filter_by(exchange="bybit", symbol="BTCUSDT").all()
        )
    assert amounts == [-0.5, 0.3]
