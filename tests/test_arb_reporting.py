"""A.6 — per-arb funding attribution + the reporting page.

Covers:
  * The pure ``compute_arb_pnl`` helper for the single-venue HL combo, a Bybit
    combo, and a cross-exchange perp-perp combo (combo 2 sums BOTH perp legs).
  * Route-level attribution (``_leg_funding`` / ``_position_view`` over real
    ``ArbFundingEvent`` rows), incl. spot legs = 0 and the window bound.
  * Two concurrent BTC arbs can't double-count one account-wide settlement
    (the A.5 symbol-exclusivity is what makes the per-leg sum single-arb-exact).
  * The dedicated arb funding poll (``poll_arb_once``) writes ``ArbFundingEvent``
    only (never ``FundingEvent``) and is insert-or-ignore idempotent.
  * ``GET /funding-arb`` renders 200 with ``Cache-Control: no-store`` + nav, and
    is OPEN (browser-nav can't send ``X-Arb-Secret``) — unlike the JSON routes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.arb_pnl import LegPnLInput, compute_arb_pnl, leg_key
from app.config import get_settings
from app.db import session_scope
from app.funding_worker import poll_arb_once
from app.main import app
from app.models import ArbFundingEvent, ArbLeg, ArbPosition, FundingEvent
from app.routes.funding_arb import _arb_performance, _leg_funding, _position_view

H = {"X-Arb-Secret": "s3cret"}


@pytest.fixture
def client_set(monkeypatch):
    monkeypatch.setenv("FUNDING_ARB_SECRET", "s3cret")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


# ===========================================================================
# Pure helper: per-combo funding attribution (spot=0; combo 2 sums both perps)
# ===========================================================================

def test_pure_helper_single_venue_hl_combo():
    """Combo 0: long HL spot + short HL perp. Funding accrues ONLY on the perp
    leg; the spot leg contributes 0. net = funding − commission."""
    legs = [
        LegPnLInput(exchange="hyperliquid", account="arb", product="spot",
                    symbol="UBTC/USDC", side="buy", filled=0.02, avg_fill=50000.0,
                    mark=50000.0, funding=0.0, commission=0.01),
        LegPnLInput(exchange="hyperliquid", account="arb", product="perp",
                    symbol="BTC", side="sell", filled=0.02, avg_fill=50000.0,
                    mark=50000.0, funding=1.50, commission=0.02),
    ]
    r = compute_arb_pnl(legs)
    assert r.funding_total == pytest.approx(1.50)              # perp only
    assert r.funding_by_leg[leg_key("hyperliquid", "arb", "UBTC/USDC")] == 0.0
    assert r.funding_by_leg[leg_key("hyperliquid", "arb", "BTC")] == pytest.approx(1.50)
    assert r.commission_total == pytest.approx(0.03)
    assert r.net == pytest.approx(1.50 - 0.03)


def test_pure_helper_bybit_combo_single_short_perp():
    """Combo 1: Bybit spot buy + Bybit perp sell. Single short perp's funding."""
    legs = [
        LegPnLInput(exchange="bybit", account="arb", product="spot",
                    symbol="ETHUSDT", side="buy", filled=1.0, avg_fill=2500.0,
                    mark=2500.0, funding=0.0, commission=0.05),
        LegPnLInput(exchange="bybit", account="arb", product="perp",
                    symbol="ETHUSDT", side="sell", filled=1.0, avg_fill=2500.0,
                    mark=2500.0, funding=0.80, commission=0.06),
    ]
    r = compute_arb_pnl(legs)
    assert r.funding_total == pytest.approx(0.80)
    assert r.net == pytest.approx(0.80 - 0.11)


def test_pure_helper_cross_exchange_sums_both_perp_legs():
    """Combo 2: cross-exchange perp-perp (long HL perp + short Bybit perp). BOTH
    perp fundings net into the harvested carry: +received on one, −paid on the
    other → funding_total is their SUM (here 1.2 + (−0.3) = 0.9)."""
    legs = [
        LegPnLInput(exchange="hyperliquid", account="arb", product="perp",
                    symbol="BTC", side="buy", filled=0.02, avg_fill=50000.0,
                    mark=50000.0, funding=-0.30, commission=0.02),
        LegPnLInput(exchange="bybit", account="arb", product="perp",
                    symbol="BTCUSDT", side="sell", filled=0.02, avg_fill=50000.0,
                    mark=50000.0, funding=1.20, commission=0.03),
    ]
    r = compute_arb_pnl(legs)
    assert r.funding_total == pytest.approx(0.90)              # 1.2 + (-0.3)
    assert r.funding_by_leg[leg_key("hyperliquid", "arb", "BTC")] == pytest.approx(-0.30)
    assert r.funding_by_leg[leg_key("bybit", "arb", "BTCUSDT")] == pytest.approx(1.20)
    assert r.net == pytest.approx(0.90 - 0.05)


def test_pure_helper_directional_net_neutral_on_balanced_pair():
    """A long spot + short perp of equal qty move opposite, so directional_net
    (spot+perp unrealized) is ≈0 even when the mark moves — the health check."""
    legs = [
        LegPnLInput(exchange="hyperliquid", account="arb", product="spot",
                    symbol="UBTC/USDC", side="buy", filled=0.02, avg_fill=50000.0,
                    mark=55000.0, funding=0.0, commission=0.0),
        LegPnLInput(exchange="hyperliquid", account="arb", product="perp",
                    symbol="BTC", side="sell", filled=0.02, avg_fill=50000.0,
                    mark=55000.0, funding=0.0, commission=0.0),
    ]
    r = compute_arb_pnl(legs)
    assert r.spot_unrealized == pytest.approx(0.02 * 5000)     # +100 (long gains)
    assert r.perp_unrealized == pytest.approx(-0.02 * 5000)    # -100 (short loses)
    assert r.directional_net == pytest.approx(0.0)


def test_pure_helper_basis_term_folds_into_net():
    legs = [LegPnLInput(exchange="bybit", account="arb", product="perp",
                        symbol="BTCUSDT", side="sell", funding=1.0, commission=0.1)]
    assert compute_arb_pnl(legs, basis=0.25).net == pytest.approx(1.0 - 0.1 + 0.25)


def test_pure_helper_directional_mtm_folds_into_net():
    """net is total economic value: a perp marked away from entry moves net by the
    unrealized MTM (net = funding − fee + directional_net)."""
    legs = [LegPnLInput(exchange="bybit", account="arb", product="perp",
                        symbol="BTCUSDT", side="sell", filled=0.02, avg_fill=50000.0,
                        mark=51000.0, funding=1.0, commission=0.1)]
    r = compute_arb_pnl(legs)
    assert r.directional_net == pytest.approx(-0.02 * 1000)        # short loses as mark rises
    assert r.net == pytest.approx(1.0 - 0.1 - 20.0)               # funding − fee + MTM


# ===========================================================================
# Route-level attribution over real ArbFundingEvent rows
# ===========================================================================

def _make_arb(asset, legs, *, status="open", opened_at=None, closed_at=None):
    """Persist one ArbPosition + legs; return its id. legs = list of dicts."""
    with session_scope() as db:
        arb = ArbPosition(idempotency_key=f"k-{asset}-{datetime.now().timestamp()}",
                          asset=asset, status=status,
                          opened_at=opened_at, closed_at=closed_at)
        db.add(arb)
        db.flush()
        for lg in legs:
            db.add(ArbLeg(arb_id=arb.id, exchange=lg["exchange"],
                          account=lg.get("account", "arb"), product=lg["product"],
                          symbol=lg["symbol"], side=lg["side"],
                          filled_qty=lg.get("filled", 0.0),
                          avg_fill=lg.get("avg_fill", 0.0),
                          commission=lg.get("commission", 0.0),
                          status=lg.get("status", "success")))
        return arb.id


def _add_funding(exchange, account, symbol, when, amount):
    with session_scope() as db:
        db.add(ArbFundingEvent(exchange=exchange, account=account, symbol=symbol,
                               funding_time=when, amount=amount))


def test_leg_funding_sums_perp_window_spot_zero():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    arb_id = _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC",
         "side": "buy", "filled": 0.02},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC",
         "side": "sell", "filled": 0.02},
    ], opened_at=t0)
    # Two settlements inside the window for the perp leg's (ex, acct, symbol).
    _add_funding("hyperliquid", "arb", "BTC", t0 + timedelta(hours=1), 0.5)
    _add_funding("hyperliquid", "arb", "BTC", t0 + timedelta(hours=9), 0.3)
    # A settlement BEFORE opened_at must NOT be counted.
    _add_funding("hyperliquid", "arb", "BTC", t0 - timedelta(hours=1), 99.0)

    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        perp = next(lg for lg in legs if lg.product == "perp")
        spot = next(lg for lg in legs if lg.product == "spot")
        assert _leg_funding(db, arb, perp) == pytest.approx(0.8)   # 0.5+0.3, pre-window excluded
        assert _leg_funding(db, arb, spot) == 0.0                  # spot always 0
        view = _position_view(arb, legs, db)
    assert view.pnl.funding_total == pytest.approx(0.8)
    assert view.pnl.funding_by_leg["hyperliquid:arb:BTC"] == pytest.approx(0.8)
    assert view.pnl.funding_by_leg["hyperliquid:arb:UBTC/USDC"] == 0.0


def test_closed_arb_funding_bounded_by_closed_at():
    """A closed arb only counts settlements up to closed_at — a later settlement
    (e.g. attributed to a re-opened arb on the re-used symbol) is excluded."""
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    tc = t0 + timedelta(hours=8)
    arb_id = _make_arb("ETH", [
        {"exchange": "bybit", "product": "spot", "symbol": "ETHUSDT", "side": "buy"},
        {"exchange": "bybit", "product": "perp", "symbol": "ETHUSDT", "side": "sell"},
    ], status="closed", opened_at=t0, closed_at=tc)
    _add_funding("bybit", "arb", "ETHUSDT", t0 + timedelta(hours=1), 0.4)   # in window
    _add_funding("bybit", "arb", "ETHUSDT", tc + timedelta(hours=1), 5.0)   # after close
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        perp = next(lg for lg in db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
                    if lg.product == "perp")
        assert _leg_funding(db, arb, perp) == pytest.approx(0.4)


def test_cross_exchange_route_sums_both_perp_legs():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    arb_id = _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "buy"},
        {"exchange": "bybit", "product": "perp", "symbol": "BTCUSDT", "side": "sell"},
    ], opened_at=t0)
    _add_funding("hyperliquid", "arb", "BTC", t0 + timedelta(hours=1), -0.30)
    _add_funding("bybit", "arb", "BTCUSDT", t0 + timedelta(hours=1), 1.20)
    with session_scope() as db:
        arb = db.get(ArbPosition, arb_id)
        legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb_id).all()
        view = _position_view(arb, legs, db)
    assert view.pnl.funding_total == pytest.approx(0.90)


# ===========================================================================
# Two concurrent BTC arbs can't double-count one account-wide settlement
# ===========================================================================

def test_two_concurrent_btc_arbs_cannot_double_count(client_set, arb_registry):
    """The A.5 symbol-exclusivity prevents two non-closed arbs from holding the
    SAME (exchange, account, symbol). So one account-wide BTC settlement is
    attributed to exactly one arb — never summed twice across two arbs."""
    # Open arb #1 (default HL BTC combo).
    r1 = client_set.post("/funding-arb/open", headers=H,
                         json={"idempotency_key": "a1", "asset": "BTC", "notional": 1000})
    assert r1.status_code == 200
    arb1 = r1.json()["arb_id"]
    # A SECOND BTC arb on the same HL legs is REFUSED (409) — it can't co-hold BTC.
    r2 = client_set.post("/funding-arb/open", headers=H,
                         json={"idempotency_key": "a2", "asset": "BTC", "notional": 1000})
    assert r2.status_code == 409, r2.text

    # Pin opened_at to a fixed PAST time so the settlement falls inside the
    # window [opened_at, now] (the background open set opened_at≈now, which would
    # put an opened+1h settlement in the future).
    opened = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with session_scope() as db:
        db.get(ArbPosition, arb1).opened_at = opened
    # One account-wide BTC perp settlement, inside the window.
    _add_funding("hyperliquid", "arb", "BTC", opened + timedelta(hours=1), 2.0)

    # It is counted by exactly ONE arb (the only one holding the BTC perp).
    holders = []
    with session_scope() as db:
        for arb in db.query(ArbPosition).all():
            legs = db.query(ArbLeg).filter(ArbLeg.arb_id == arb.id).all()
            f = sum(_leg_funding(db, arb, lg) for lg in legs)
            if abs(f) > 0:
                holders.append((arb.id, f))
    assert holders == [(arb1, pytest.approx(2.0))]   # one arb, counted once


# ===========================================================================
# The dedicated arb funding poll
# ===========================================================================

def test_poll_arb_once_writes_only_arb_table_idempotent(arb_registry):
    """poll_arb_once fetches the perp legs' funding via get('hyperliquid','arb')
    and writes ArbFundingEvent (insert-or-ignore), NEVER the directional
    FundingEvent table."""
    _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC", "side": "buy"},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "sell"},
    ], opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    # The HL arb fake returns these settlements for the perp symbol.
    arb_registry.state.funding["BTC"] = [
        {"time_ms": 1_700_000_000_000, "amount": 0.5},
        {"time_ms": 1_700_028_800_000, "amount": -0.2},
    ]
    assert poll_arb_once() == 2          # both stored
    assert poll_arb_once() == 0          # same window -> dedup, no double-count

    with session_scope() as db:
        arb_amounts = sorted(r.amount for r in db.query(ArbFundingEvent).all())
        assert arb_amounts == [-0.2, 0.5]
        # The spot leg's symbol is never polled (perp-only), and the directional
        # FundingEvent table is untouched.
        assert db.query(FundingEvent).count() == 0
        assert db.query(ArbFundingEvent).filter_by(symbol="UBTC/USDC").count() == 0


def test_poll_arb_once_resilient_to_leg_failure(arb_registry):
    """One leg's get_funding raising doesn't abort the rest of the poll (it logs +
    continues to the next leg)."""
    _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "sell"},
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC", "side": "buy"},
    ], opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc))

    def _boom(symbol, start, end):
        raise RuntimeError("venue down")

    arb_registry.get("hyperliquid", "arb").get_funding = _boom
    # Must not raise; returns 0 (the only perp leg failed).
    assert poll_arb_once() == 0
    with session_scope() as db:
        assert db.query(ArbFundingEvent).count() == 0


def test_funding_loop_runs_both_directional_and_arb_polls(strategies_yaml, arb_registry):
    """The SAME hourly loop runs the directional poll then the arb poll, each in
    its own session_scope. One short tick (sleep≈0) proves both fire and land in
    their SEPARATE tables."""
    import asyncio

    from app.funding_worker import funding_loop
    from app.routing import StrategyRouter

    router = StrategyRouter(strategies_yaml)
    # Directional venue funding (BTCUSDT is solely owned by TEST_BTC).
    arb_registry.state.funding["BTCUSDT"] = [{"time_ms": 1_700_000_000_000, "amount": -0.5}]
    # Arb perp-leg funding (a non-closed BTC arb on the HL arb account).
    _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "sell"},
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC", "side": "buy"},
    ], opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    arb_registry.state.funding["BTC"] = [{"time_ms": 1_700_000_000_000, "amount": 0.9}]

    async def _drive():
        stop = asyncio.Event()
        task = asyncio.create_task(
            funding_loop(router, poll_interval_sec=0.0, stop_event=stop, align_hour=False))
        await asyncio.sleep(0.05)   # let one tick run
        stop.set()
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())
    with session_scope() as db:
        assert db.query(FundingEvent).filter_by(symbol="BTCUSDT").count() == 1   # directional
        assert db.query(ArbFundingEvent).filter_by(symbol="BTC").count() == 1    # arb
        # Cross-check: directional table has NO arb funding, arb table has NO
        # directional funding (separate books).
        assert db.query(FundingEvent).filter_by(symbol="BTC").count() == 0
        assert db.query(ArbFundingEvent).filter_by(symbol="BTCUSDT").count() == 0


def test_poll_arb_once_skips_closed_arbs(arb_registry):
    """Closed arbs are flat — their perp legs are NOT polled."""
    _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "sell"},
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC", "side": "buy"},
    ], status="closed", opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
       closed_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    arb_registry.state.funding["BTC"] = [{"time_ms": 1_700_000_000_000, "amount": 9.9}]
    assert poll_arb_once() == 0
    with session_scope() as db:
        assert db.query(ArbFundingEvent).count() == 0


# ===========================================================================
# Reporting page
# ===========================================================================

def test_arb_report_page_renders_200_no_store(client_set):
    """GET /funding-arb renders 200 with Cache-Control: no-store + reciprocal nav,
    and is OPEN (no X-Arb-Secret needed — browser nav can't send it)."""
    r = client_set.get("/funding-arb")          # NO header
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert "Funding Arbitrage" in r.text
    assert 'href="/"' in r.text and 'href="/performance"' in r.text   # reciprocal nav
    assert "No funding-arb positions yet" in r.text


def test_arb_report_page_shows_rows_and_funding(client_set):
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC",
         "side": "buy", "filled": 0.02, "avg_fill": 50000.0, "commission": 0.01},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC",
         "side": "sell", "filled": 0.02, "avg_fill": 50000.0, "commission": 0.02},
    ], opened_at=t0)
    _add_funding("hyperliquid", "arb", "BTC", t0 + timedelta(hours=1), 1.50)
    r = client_set.get("/funding-arb")
    assert r.status_code == 200
    assert "hyperliquid:arb perp BTC" in r.text     # nested leg row rendered
    assert "UBTC/USDC" in r.text

    with session_scope() as db:
        report = _arb_performance(db)
    assert report["totals"]["funding"] == pytest.approx(1.50)
    assert report["totals"]["commission"] == pytest.approx(0.03)
    assert report["totals"]["net"] == pytest.approx(1.50 - 0.03)
    assert report["totals"]["open_count"] == 1
    arb = report["arbs"][0]
    spot_leg = next(lg for lg in arb["legs"] if lg["product"] == "spot")
    perp_leg = next(lg for lg in arb["legs"] if lg["product"] == "perp")
    assert spot_leg["funding"] == 0.0                # spot leg shows 0
    assert perp_leg["funding"] == pytest.approx(1.50)


# ===========================================================================
# Equity curve (own table, isolated from the directional /performance curve)
# ===========================================================================

def test_write_arb_equity_snapshot_captures_net_and_by_venue():
    """The hourly arb snapshot stores net (= funding − commission) + a per-venue map,
    pinned to the given hour, in the arb table ONLY (never directional)."""
    from app.funding_worker import write_arb_equity_snapshot
    from app.models import ArbEquitySnapshot, EquitySnapshot
    from app.routes.funding_arb import _load_arb_snapshots
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC",
         "side": "buy", "filled": 0.02, "avg_fill": 50000.0, "commission": 0.01},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC",
         "side": "sell", "filled": 0.02, "avg_fill": 50000.0, "commission": 0.02},
    ], opened_at=t0)
    _add_funding("hyperliquid", "arb", "BTC", t0 + timedelta(hours=1), 1.50)
    assert write_arb_equity_snapshot(datetime(2026, 6, 1, 2, tzinfo=timezone.utc)) is True
    with session_scope() as db:
        assert db.query(EquitySnapshot).count() == 0          # isolation: directional untouched
        assert db.query(ArbEquitySnapshot).count() == 1
        snaps = _load_arb_snapshots(db)
    ts, net, by = snaps[0]
    assert net == pytest.approx(1.50 - 0.03)                  # funding − commission
    assert by["hyperliquid"] == pytest.approx(1.50 - 0.03)    # single-venue HL
    assert ts.hour == 2 and ts.minute == 0                    # pinned to the hour


def test_write_arb_equity_snapshot_skips_when_no_arbs():
    """No arb book yet → no curve point written (the curve builds forward)."""
    from app.funding_worker import write_arb_equity_snapshot
    from app.models import ArbEquitySnapshot
    assert write_arb_equity_snapshot() is False
    with session_scope() as db:
        assert db.query(ArbEquitySnapshot).count() == 0


def test_arb_equity_curve_isolated_from_directional():
    """The directional curve sees ONLY equity_snapshots; the arb loader sees ONLY
    arb_equity_snapshots — neither leaks into the other (isolation invariant)."""
    from app.models import ArbEquitySnapshot, EquitySnapshot
    from app.routes.dashboard import _equity_curve, _load_snapshots
    from app.routes.funding_arb import _load_arb_snapshots
    t = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    with session_scope() as db:
        db.add(ArbEquitySnapshot(captured_at=t, net=5.0, by_venue='{"hyperliquid": 5.0}'))
        db.add(EquitySnapshot(captured_at=t, total_pnl=99.0, by_exchange='{"bybit": 99.0}'))
    with session_scope() as db:
        directional = _load_snapshots(db)
        assert len(directional) == 1 and directional[0][1] == pytest.approx(99.0)  # not the arb 5.0
        assert _equity_curve(db)[-1][1] == pytest.approx(99.0)
        arb = _load_arb_snapshots(db)
        assert len(arb) == 1 and arb[0][1] == pytest.approx(5.0)                    # not the directional 99


def test_arb_report_page_renders_equity_curve(client_set):
    """/funding-arb renders the equity-curve section (window chips + ECharts chart +
    venue legend) once arb snapshots exist."""
    from app.models import ArbEquitySnapshot
    with session_scope() as db:
        db.add(ArbEquitySnapshot(captured_at=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
                                 net=1.0, by_venue='{"hyperliquid": 1.0}'))
        db.add(ArbEquitySnapshot(captured_at=datetime(2026, 6, 1, 13, tzinfo=timezone.utc),
                                 net=1.5, by_venue='{"hyperliquid": 1.5}'))
    r = client_set.get("/funding-arb?equity_window=All")
    assert r.status_code == 200, r.text
    assert "Equity curve" in r.text
    assert "hyperliquid" in r.text                 # venue line in the legend
    assert 'class="echart"' in r.text              # ECharts canvas (replaced the SVG/scrub)
    assert "echarts" in r.text and "/static/app.js" in r.text   # charting lib + initializer wired in


# ===========================================================================
# Return-% / APR (capital base: auto from deployed notional, or configured)
# ===========================================================================

_HL_PAIR = [
    {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC",
     "side": "buy", "filled": 0.02, "avg_fill": 50000.0},      # 0.02 * 50000 = $1000
    {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC",
     "side": "sell", "filled": 0.02, "avg_fill": 50000.0},
]


def test_arb_capital_base_auto_from_open_arbs():
    """Auto capital = deployed notional across OPEN arbs (the MAX leg per arb, since
    the two legs are the same position — not their sum); flat book → 0 ($-only)."""
    from app.routes.funding_arb import _arb_capital_base
    aid = _make_arb("BTC", _HL_PAIR, status="open",
                    opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    with session_scope() as db:
        assert _arb_capital_base(_arb_performance(db)) == pytest.approx(1000.0)   # not 2000
    with session_scope() as db:                    # close it -> nothing deployed
        db.query(ArbPosition).filter_by(id=aid).update({"status": "closed"})
    with session_scope() as db:
        assert _arb_capital_base(_arb_performance(db)) == 0.0


def test_arb_capital_base_config_override(monkeypatch):
    """A configured arb_capital_base pins the denominator (ignores deployed notional)."""
    from app.routes.funding_arb import _arb_capital_base
    monkeypatch.setattr("app.routes.funding_arb.get_settings",
                        lambda: type("S", (), {"arb_capital_base": 5000.0})())
    _make_arb("BTC", _HL_PAIR, status="open",
              opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    with session_scope() as db:
        assert _arb_capital_base(_arb_performance(db)) == pytest.approx(5000.0)


def test_arb_report_page_shows_apr(client_set):
    """With an open arb (deployed capital) + a multi-day curve, the page shows
    return-% and an annualized APR."""
    from app.models import ArbEquitySnapshot
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _make_arb("BTC", _HL_PAIR, status="open", opened_at=t0)
    _add_funding("hyperliquid", "arb", "BTC", t0 + timedelta(hours=1), 20.0)   # net = +20
    with session_scope() as db:
        db.add(ArbEquitySnapshot(captured_at=t0, net=0.0, by_venue='{"hyperliquid": 0.0}'))
        db.add(ArbEquitySnapshot(captured_at=t0 + timedelta(days=2), net=20.0,
                                 by_venue='{"hyperliquid": 20.0}'))
    r = client_set.get("/funding-arb?equity_window=All")
    assert r.status_code == 200, r.text
    assert "APR (est.)" in r.text
    assert "on $1000" in r.text                     # auto capital = 0.02 * 50000, return-% caption


# ===========================================================================
# Basis P&L, slippage, and the closed-arb neutrality fix
# ===========================================================================

def test_arb_basis_slippage_and_closed_not_skew():
    """_arb_performance surfaces basis (entry spread) + slippage (fill vs the recorded
    mid), and flags neutrality only for open-ish arbs — a closed/flat arb is not 'skew'."""
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    aid = _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC", "side": "buy",
         "filled": 0.02, "avg_fill": 50010.0},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "sell",
         "filled": 0.02, "avg_fill": 50100.0},
    ], status="open", opened_at=t0)
    with session_scope() as db:                      # record the mid-at-fill on each leg
        for lg in db.query(ArbLeg).filter(ArbLeg.arb_id == aid):
            lg.ref_price = 50000.0
    cid = _make_arb("PURR", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "PURR/USDC", "side": "buy"},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "PURR", "side": "sell"},
    ], status="closed")
    with session_scope() as db:
        report = _arb_performance(db)
    a = next(x for x in report["arbs"] if x["arb_id"] == aid)
    assert a["basis"] == pytest.approx((50100.0 - 50010.0) * 0.02)          # (sell − buy) × qty
    assert a["slippage_known"] is True
    assert a["slippage"] == pytest.approx((50010 - 50000) * 0.02 + (50000 - 50100) * 0.02)
    assert a["show_neutrality"] is True
    c = next(x for x in report["arbs"] if x["arb_id"] == cid)
    assert c["show_neutrality"] is False             # closed -> NOT flagged skew
    assert c["slippage_known"] is False              # no ref / no fills
    assert report["totals"]["basis"] == pytest.approx(a["basis"])           # closed adds 0
    assert report["totals"]["slippage_known"] is True


def test_subgrid_skew_reads_neutral_real_skew_does_not(arb_registry):
    """Neutrality is judged against the GRID: a sub-grid imbalance (a spot fill landing
    between perp grid points) reads NEUTRAL, while a skew far above one step does NOT —
    so live grid dust isn't false-flagged as directional exposure, but a half-hedge is."""
    arb_registry.state.step_sizes["BTC"] = 0.001            # perp grid (the coarser one)
    arb_registry.state.spot_step_sizes["UBTC/USDC"] = 0.0001
    tight = _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC",
         "side": "buy", "filled": 0.0204, "avg_fill": 50000.0},     # +0.0004 -> within step/2
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC",
         "side": "sell", "filled": 0.0200, "avg_fill": 50000.0},
    ], status="open")
    wide = _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC",
         "side": "buy", "filled": 0.0250, "avg_fill": 50000.0},     # +0.005 = 5 steps -> real skew
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC",
         "side": "sell", "filled": 0.0200, "avg_fill": 50000.0},
    ], status="open")
    with session_scope() as db:
        report = _arb_performance(db)
    t = next(x for x in report["arbs"] if x["arb_id"] == tight)
    w = next(x for x in report["arbs"] if x["arb_id"] == wide)
    assert t["neutral"] is True          # 0.0004 <= 0.001/2  -> sub-grid dust
    assert w["neutral"] is False         # 0.005   >  0.001/2  -> genuine skew


def test_neutrality_uses_persisted_grid_step_no_live_call(arb_registry):
    """When legs carry the open-time grid (ArbLeg.grid_step), neutrality is judged from
    it with NO read-path exchange call (and stays stable if the adapter is down/re-tiered)
    — the F6 fix. A live get_step_size lookup must NOT happen."""
    aid = _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC", "side": "buy",
         "filled": 0.0204, "avg_fill": 50000.0},     # +0.0004 -> within stored step/2
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "sell",
         "filled": 0.0200, "avg_fill": 50000.0},
    ], status="open")
    with session_scope() as db:                       # persist the open-time grid on the legs
        for lg in db.query(ArbLeg).filter_by(arb_id=aid):
            lg.grid_step = 0.001
    arb_registry.state.calls.clear()
    with session_scope() as db:
        report = _arb_performance(db)
    a = next(x for x in report["arbs"] if x["arb_id"] == aid)
    assert a["neutral"] is True                       # 0.0004 <= 0.001/2, via the STORED step
    step_calls = [c for c in arb_registry.state.calls
                  if c[0] in ("get_step_size", "get_spot_step_size")]
    assert step_calls == [], f"neutrality made a live step lookup: {step_calls}"


def test_net_includes_directional_mtm_open_but_not_closed(arb_registry):
    """net = funding − fees + directional MTM for an OPEN arb (the legs' live mark), but
    a CLOSED arb is flat -> its directional MTM is suppressed (no phantom mark on a
    gone position), so its net is just realized funding − fees. Per-venue sums to net."""
    from app.routes.funding_arb import _arb_by_venue
    arb_registry.state.positions["BTC"] = (0.0, 51000.0)     # _perp_mark -> 51000 (short down $20)
    arb_registry.state.prices["UBTC/USDC"] = 50000.0         # _spot_mark == entry -> 0 MTM
    legs = [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC",
         "side": "buy", "filled": 0.02, "avg_fill": 50000.0, "commission": 0.0},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC",
         "side": "sell", "filled": 0.02, "avg_fill": 50000.0, "commission": 0.0},
    ]
    op = _make_arb("BTC", legs, status="open")
    cl = _make_arb("BTC", [{**lg, "status": "closed"} for lg in legs], status="closed")
    with session_scope() as db:
        report = _arb_performance(db)
        by = _arb_by_venue(report)
    o = next(x for x in report["arbs"] if x["arb_id"] == op)
    c = next(x for x in report["arbs"] if x["arb_id"] == cl)
    assert o["directional_net"] == pytest.approx(-20.0)      # marked: short down $20
    assert o["net"] == pytest.approx(-20.0)                  # funding 0 − fee 0 + MTM
    assert c["directional_net"] == pytest.approx(0.0)        # closed legs -> flat, suppressed
    assert c["net"] == pytest.approx(0.0)                    # no realized booked here -> 0
    assert by["hyperliquid"] == pytest.approx(o["net"] + c["net"])   # per-venue sums to total net


def test_closed_arb_retains_realized_directional_in_net(arb_registry):
    """A closed arb keeps the realized directional P&L booked at close (ArbLeg.realized_pnl)
    in its net — net = funding − fees + realized — even though its live MTM is suppressed
    (flat). Per-venue still sums to net."""
    from app.routes.funding_arb import _arb_by_venue
    arb_registry.state.prices["UBTC/USDC"] = 99999.0     # a wild live mark...
    arb_registry.state.positions["BTC"] = (0.0, 99999.0) # ...is IGNORED (legs closed -> flat)
    aid = _make_arb("BTC", [
        {"exchange": "hyperliquid", "product": "spot", "symbol": "UBTC/USDC", "side": "buy",
         "filled": 0.02, "avg_fill": 50000.0, "status": "closed"},
        {"exchange": "hyperliquid", "product": "perp", "symbol": "BTC", "side": "sell",
         "filled": 0.02, "avg_fill": 50000.0, "status": "closed"},
    ], status="closed")
    with session_scope() as db:                          # book realized at close (spot +10, perp −20)
        for lg in db.query(ArbLeg).filter(ArbLeg.arb_id == aid):
            lg.realized_pnl = 10.0 if lg.product == "spot" else -20.0
    with session_scope() as db:
        report = _arb_performance(db)
        by = _arb_by_venue(report)
    a = next(x for x in report["arbs"] if x["arb_id"] == aid)
    assert a["directional_net"] == pytest.approx(0.0)     # flat: no phantom MTM despite wild mark
    assert a["net"] == pytest.approx(-10.0)               # funding 0 − fee 0 + realized (10 − 20)
    assert a["realized"] == pytest.approx(-10.0)
    assert by["hyperliquid"] == pytest.approx(-10.0)      # per-venue carries realized too
    # totals expose realized AND the headline identity reconciles (F4: net includes it).
    t = report["totals"]
    assert t["realized"] == pytest.approx(-10.0)
    assert t["net"] == pytest.approx(t["funding"] - t["commission"]
                                     + t["directional_net"] + t["realized"])
