from __future__ import annotations
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from .. import network
from ..config import get_settings
from ..db import session_scope
from ..exchanges.registry import get_registry
from ..executor import _fill_math
from ..models import Alert, FundingEvent, Order, Position, StrategyPosition
from ..reconcile import single_owner_map

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


# ---------- live performance page ----------

def _actual_positions(db) -> list[dict]:
    """What the EXCHANGE actually holds, per (exchange, symbol) — read live from
    the venue (`get_position`), which is the only source of truth.

    The per-strategy ledger is signal-derived *intent*: under one-way netting the
    venue collapses every strategy on a symbol into ONE position, so the ledger's
    per-strategy split can't be verified and can drift from reality (manual or
    unrecorded trades). We therefore read the net straight from the exchange and
    fall back to the netted ledger only when the read fails.

    We probe only symbols with a non-flat ledger leg (bounds the live calls to a
    handful and keeps a flat account network-free); a fully-manual position on an
    untouched symbol won't surface here. Unrealized marks the exchange net against
    the ledger's blended entry for the symbol (the venue API returns a mark here,
    not an entry). For a solely-owned or uniformly re-baselined symbol that blended
    entry is the real entry, so its unrealized is exact; only a genuinely
    mixed-entry symbol is approximate."""
    cost: dict[tuple, float] = {}      # Σ |qty|*avg over open legs (for blended entry)
    qsum: dict[tuple, float] = {}      # Σ |qty| over open legs
    last: dict[tuple, float] = {}      # last stored mark
    net: dict[tuple, float] = {}       # netted ledger qty (fallback only)
    probe: set[tuple] = set()          # symbols worth a live read (a non-flat leg)
    for sp in db.query(StrategyPosition).all():
        k = (sp.exchange, sp.symbol)
        net[k] = net.get(k, 0.0) + sp.net_qty_base
        if sp.last_price:
            last[k] = sp.last_price
        if not _venue_flat(sp.net_qty_base, sp.net_qty_usd):
            probe.add(k)
            if (sp.avg_entry_price or 0) > 0:
                cost[k] = cost.get(k, 0.0) + abs(sp.net_qty_base) * sp.avg_entry_price
                qsum[k] = qsum.get(k, 0.0) + abs(sp.net_qty_base)

    reg = get_registry()
    out: list[dict] = []
    for ex, sym in probe:
        k = (ex, sym)
        # Defaults are the netted ledger (used only if the live read fails).
        qty, mark = net.get(k, 0.0), last.get(k, 0.0)
        entry = (cost[k] / qsum[k]) if qsum.get(k) else 0.0   # ledger blend
        unreal, source = None, "ledger"
        try:                                   # live exchange state is the truth
            d = reg.get(ex).get_position_detail(sym)
            qty = d["qty"]
            mark = d["mark"] or mark
            entry = d["entry"] or entry        # exchange's own avg entry (correct net entry)
            unreal = d.get("unrealized")       # exchange's own unrealized PnL
            source = "exchange"
        except Exception:                      # noqa: BLE001 — display path, never raise
            pass
        if _venue_flat(qty, qty * mark):
            continue
        # Fall back to entry-based unrealized only when the venue didn't report one.
        if unreal is None:
            unreal = qty * (mark - entry) if entry > 0 else 0.0
        out.append({"exchange": ex, "symbol": sym, "net_qty_base": qty, "mark": mark,
                    "entry": entry, "source": source, "unrealized": unreal})
    return sorted(out, key=lambda a: (a["exchange"], a["symbol"]))


def _performance(db, router) -> dict:
    """Per-strategy + per-exchange PnL breakdown.

    Total = realized + unrealized + funding − commission. Slippage is a
    DIAGNOSTIC (implementation shortfall vs the signal price): it's already
    embedded in the fills/realized PnL, so it's shown but NOT re-deducted.
    Funding is attributed to a strategy only when it solely owns the symbol;
    otherwise it counts at the exchange/portfolio level only.

    The headline and per-exchange rows are the SUM of the per-strategy figures, so
    the breakdown always adds up to the total. Per-strategy unrealized marks each
    leg against its own (kline-reconstructed) entry; on a symbol shared by several
    strategies the split is signal-derived intent, flagged ≈. Because offsetting
    legs cancel on a one-way-netting account, this per-strategy sum is NOTIONAL and
    can differ from what's realizable — the "Exchange positions" table shows the
    realizable netted truth straight from the venue for reconciliation.
    """
    owners = single_owner_map(router)
    strat: dict[str, dict] = {}
    exch: dict[str, dict] = {}

    def row(store: dict, key: str, label_key: str) -> dict:
        if key not in store:
            store[key] = {label_key: key, "realized": 0.0, "unrealized": 0.0,
                          "commission": 0.0, "slippage": 0.0, "funding": 0.0,
                          "unrealized_attributed": False}
        return store[key]

    # Netted positions the exchange actually holds (realizable-reconciliation table)
    # + a live mark per symbol to value the per-strategy legs against.
    actual = _actual_positions(db)
    marks = {(a["exchange"], a["symbol"]): a["mark"] for a in actual}
    actual_unrealized = sum(a["unrealized"] for a in actual)

    open_positions: list[dict] = []
    for sp in db.query(StrategyPosition).all():
        s = row(strat, sp.strategy_id, "strategy_id")
        e = row(exch, sp.exchange, "exchange")
        s["realized"] += sp.realized_pnl or 0.0
        e["realized"] += sp.realized_pnl or 0.0
        if not _venue_flat(sp.net_qty_base, sp.net_qty_usd):
            avg = sp.avg_entry_price or 0.0
            mark = marks.get((sp.exchange, sp.symbol)) or sp.last_price or 0.0
            # Guard: a position migrated/synced in without a known entry has avg=0;
            # net*(mark-0) would report the whole notional as bogus unrealized PnL.
            unreal = sp.net_qty_base * (mark - avg) if avg > 0 else 0.0
            # Per-strategy entry is only "real" when ONE strategy owns the symbol.
            # On a shared symbol the exchange nets every leg into one position, so the
            # per-strategy split (qty AND entry) is intent/attribution, not verifiable.
            # Flagged with ≈ in the UI and kept OUT of the headline unrealized.
            if avg <= 0:
                basis = "none"
            elif owners.get((sp.exchange, sp.symbol)) == sp.strategy_id:
                basis = "real"
            else:
                basis = "attributed"
                s["unrealized_attributed"] = True
            s["unrealized"] += unreal
            e["unrealized"] += unreal
            open_positions.append({
                "strategy_id": sp.strategy_id, "exchange": sp.exchange,
                "symbol": sp.symbol, "net_qty_base": sp.net_qty_base,
                "avg_entry_price": avg, "mark": mark, "unrealized": unreal,
                "basis": basis})

    for o, sid in (db.query(Order, Alert.strategy_id)
                     .join(Alert, Order.alert_id == Alert.id)
                     .filter(Order.status == "success").all()):
        s = row(strat, sid, "strategy_id")
        e = row(exch, o.exchange, "exchange")
        s["commission"] += o.commission or 0.0
        e["commission"] += o.commission or 0.0
        if o.fill_price and o.signal_price:
            sign = 1.0 if o.side == "buy" else -1.0
            cost = (o.fill_price - o.signal_price) * (o.qty_base or 0.0) * sign
            s["slippage"] += cost
            e["slippage"] += cost

    funding_total = 0.0
    for ex, sym, total in (db.query(FundingEvent.exchange, FundingEvent.symbol,
                                    func.sum(FundingEvent.amount))
                             .group_by(FundingEvent.exchange, FundingEvent.symbol).all()):
        total = float(total or 0.0)
        funding_total += total
        row(exch, ex, "exchange")["funding"] += total
        sid = owners.get((ex, sym))
        if sid is not None:
            row(strat, sid, "strategy_id")["funding"] += total

    def finalize(store: dict) -> list[dict]:
        rows = []
        for r in store.values():
            r["total"] = r["realized"] + r["unrealized"] + r["funding"] - r["commission"]
            rows.append(r)
        return sorted(rows, key=lambda r: r["total"], reverse=True)

    per_strategy = finalize(strat)
    per_exchange = finalize(exch)
    # Headline = SUM of the per-strategy figures, so the breakdown always adds up.
    totals = {k: sum(r[k] for r in per_strategy)
              for k in ("realized", "unrealized", "commission", "slippage")}
    totals["funding"] = funding_total          # exchange-level total (incl. unattributed)
    totals["total"] = (totals["realized"] + totals["unrealized"]
                       + totals["funding"] - totals["commission"])
    totals["unrealized_attributed"] = any(r["unrealized_attributed"] for r in per_strategy)
    # Realizable unrealized straight from the venue (offsetting legs cancel here);
    # differs from the per-strategy sum by the netting offset — shown for reconciliation.
    totals["unrealized_realizable"] = actual_unrealized
    return {"per_strategy": per_strategy, "per_exchange": per_exchange,
            "totals": totals, "open_positions": open_positions,
            "exchange_positions": actual}


def _equity_curve(db, unrealized: float = 0.0) -> list[tuple]:
    """Cumulative PnL (USDT) from 0 in time order: per-fill realized deltas
    (replayed via the shared fill math) minus commissions, plus funding events.

    A final mark-to-market point at 'now' folds in live `unrealized`, so the curve
    ends at the real current total PnL (realized + funding − commission + unrealized)
    instead of sitting near 0. This matters because most historical fills predate
    fill-price capture (fill_price=0) — their realized PnL can't be replayed, so the
    realized-only line is a flat noise band and the open position's mark-to-market is
    where the real PnL currently lives."""
    events: list[tuple] = []
    state: dict[tuple, tuple] = {}    # (sid, exchange, symbol) -> (net, avg)
    for o, sid in (db.query(Order, Alert.strategy_id)
                     .join(Alert, Order.alert_id == Alert.id)
                     .filter(Order.status == "success")
                     .order_by(Order.created_at).all()):
        delta = -(o.commission or 0.0)
        price = o.fill_price or 0.0
        if price > 0 and o.qty_base:
            key = (sid, o.exchange, o.symbol)
            net, avg = state.get(key, (0.0, 0.0))
            signed = o.qty_base * (1.0 if o.side == "buy" else -1.0)
            net, avg, realized_delta = _fill_math(net, avg, signed, o.qty_base, price)
            state[key] = (net, avg)
            delta += realized_delta
        events.append((o.created_at, delta))
    for fe in db.query(FundingEvent).all():
        events.append((fe.funding_time, fe.amount or 0.0))

    events.sort(key=lambda e: e[0] or datetime.min)
    points, cum = [], 0.0
    for ts, d in events:
        cum += d
        points.append((ts, cum))
    if unrealized and points:
        points.append((datetime.now(timezone.utc), cum + unrealized))
    return points


def _equity_svg(points, width: int = 920, height: int = 200, pad: int = 10):
    """Map equity points to an inline-SVG polyline (+ zero baseline). None when
    there's nothing to plot."""
    if not points:
        return None
    ys = [p[1] for p in points]
    lo, hi = min(ys + [0.0]), max(ys + [0.0])
    span = (hi - lo) or 1.0
    n = len(points)

    # Position points by real elapsed time so dense clusters (hourly funding) don't
    # crowd out sparse order events; fall back to even spacing if times are missing
    # or all identical.
    def _epoch(dt):
        if dt is None:
            return None
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    ts = [_epoch(p[0]) for p in points]
    valid = [t for t in ts if t is not None]
    t0 = min(valid) if valid else 0.0
    tspan = (max(valid) - t0) if valid else 0.0

    def fx(i: int) -> float:
        if tspan and ts[i] is not None:
            return pad + (width - 2 * pad) * ((ts[i] - t0) / tspan)
        return pad + (width - 2 * pad) * (i / (n - 1) if n > 1 else 0.0)

    def fy(v: float) -> float:
        return pad + (height - 2 * pad) * (1 - (v - lo) / span)

    poly = " ".join(f"{fx(i):.1f},{fy(v):.1f}" for i, v in enumerate(ys))
    return {"polyline": poly, "zero_y": round(fy(0.0), 1), "width": width,
            "height": height, "last": ys[-1], "hi": hi, "lo": lo,
            "color": "#4ade80" if ys[-1] >= 0 else "#f87171",
            "start_label": _fmt_when(points[0][0], "%m-%d %H:%M"),
            "end_label": _fmt_when(points[-1][0], "%m-%d %H:%M")}


def _recent_orders(db, limit: int = 50) -> list[dict]:
    """Recent orders (newest first), incl. rejected/paper, with strategy + slippage."""
    rows = (db.query(Order, Alert.strategy_id)
              .join(Alert, Order.alert_id == Alert.id)
              .order_by(Order.created_at.desc()).limit(limit).all())
    return [{
        "created_at": o.created_at, "strategy_id": sid, "exchange": o.exchange,
        "symbol": o.symbol, "side": o.side, "qty_base": o.qty_base,
        "fill_price": o.fill_price, "status": o.status, "commission": o.commission,
        "commission_asset": o.commission_asset,
        "slippage_bps": _slip_bps(o.fill_price, o.signal_price, o.side),
        "error": o.error_message,
    } for o, sid in rows]


@router.get("/performance", response_class=HTMLResponse)
def performance(request: Request):
    with session_scope() as db:
        perf = _performance(db, request.app.state.strategy_router)
        equity = _equity_svg(_equity_curve(db, perf["totals"]["unrealized"]))
        orders = _recent_orders(db, limit=50)
    resp = templates.TemplateResponse("performance.html", {
        "request": request, "perf": perf, "equity": equity, "orders": orders,
    })
    resp.headers["Cache-Control"] = _NO_STORE
    return resp


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
        recent_orders = (db.query(Order, Alert.strategy_id)
                         .join(Alert, Order.alert_id == Alert.id)
                         .order_by(Order.created_at.desc()).limit(25).all())
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
