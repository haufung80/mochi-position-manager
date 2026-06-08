from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone
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
from ..models import Alert, EquitySnapshot, FundingEvent, Order, Position, StrategyPosition
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
        # Require a real mark too, else a missing/0 mark would fabricate qty*(0-entry).
        if unreal is None:
            unreal = qty * (mark - entry) if (entry > 0 and mark > 0) else 0.0
        out.append({"exchange": ex, "symbol": sym, "net_qty_base": qty, "mark": mark,
                    "entry": entry, "source": source, "unrealized": unreal})
    return sorted(out, key=lambda a: (a["exchange"], a["symbol"]))


def _performance(db, router) -> dict:
    """Per-strategy + per-exchange PnL breakdown.

    Total = realized + unrealized + funding − commission. Slippage is a
    DIAGNOSTIC (implementation shortfall vs the signal price): it's already
    embedded in the fills/realized PnL, so it's shown but NOT re-deducted.
    Funding is EXCHANGE-level only — the venue funds the netted position per
    symbol, so it can't be split per strategy (it reconciles at the exchange +
    portfolio level, not in the per-strategy rows).

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
            # Prefer the live exchange mark (from _actual_positions). A leg that has
            # drifted flat on the venue (manually closed) won't be in `marks`, so fetch
            # the live market price directly rather than falling back to a stale fill.
            mark = marks.get((sp.exchange, sp.symbol)) or 0.0
            if not mark:
                try:
                    live = get_registry().get(sp.exchange).get_price(sp.symbol)
                    mark = live if (live and live > 0) else (sp.last_price or 0.0)
                except Exception:                  # noqa: BLE001 — display path, never raise
                    mark = sp.last_price or 0.0
            # Guard: a position migrated/synced in without a known entry has avg=0;
            # net*(mark-0) would report the whole notional as bogus unrealized PnL.
            unreal = sp.net_qty_base * (mark - avg) if avg > 0 else 0.0
            # Per-strategy entry is only "real" when ONE strategy owns the symbol.
            # On a shared symbol the exchange nets every leg into one position, so the
            # per-strategy split (qty AND entry) is intent/attribution, not verifiable;
            # flagged with ≈ in the UI. This notional unreal IS summed into the headline
            # (the chosen per-strategy-sum model); the realizable figure is tracked
            # separately as totals["unrealized_realizable"] from the live exchange.
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

    # Funding reconciles at the EXCHANGE + portfolio level only. The venue charges
    # it on the netted position per symbol, so it can't be split per strategy on a
    # shared symbol — we never attribute it to a strategy row.
    funding_total = 0.0
    for ex, total in (db.query(FundingEvent.exchange, func.sum(FundingEvent.amount))
                        .group_by(FundingEvent.exchange).all()):
        total = float(total or 0.0)
        funding_total += total
        row(exch, ex, "exchange")["funding"] += total

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


def _equity_curve(db, end_total=None) -> list[tuple]:
    """Equity curve from periodic `EquitySnapshot` rows — each one is the TRUE total
    PnL (realized + unrealized + funding − commission) captured at that time by the
    funding worker, so the line needs no fill-replay reconstruction. (The old replay
    couldn't price fills that predate fill-price capture, so realized never accrued
    and the line read flat with only the endpoint spiking.) A final point at 'now'
    is anchored to `end_total` (the live headline) so the endpoint always matches the
    page between hourly snapshots. Forward-looking: the curve builds from the first
    snapshot after deploy — earlier history isn't reconstructed."""
    # Total-only, all-time — delegates to _equity_series (the multi-series/windowed
    # builder the page uses) so there's one source of truth for the curve.
    return _equity_series(db, None, live_total=end_total).get("Total", [])


_EQUITY_WINDOWS = [
    ("24h", timedelta(hours=24)), ("7D", timedelta(days=7)),
    ("30D", timedelta(days=30)), ("90D", timedelta(days=90)),
    ("180D", timedelta(days=180)), ("365D", timedelta(days=365)),
    ("All", None),
]
_EQUITY_WINDOW_MAP = dict(_EQUITY_WINDOWS)
_EQUITY_DEFAULT_WINDOW = "All"
# bybit brand-orange / hyperliquid teal; any other venue cycles a fallback palette.
_SERIES_COLORS = {"bybit": "#f7a600", "hyperliquid": "#26a69a"}
_SERIES_PALETTE = ["#a78bfa", "#60a5fa", "#fbbf24", "#f472b6", "#34d399"]


def _resolve_window(name: str):
    """(validated_name, timedelta|None). Unknown name -> the default window."""
    if name in _EQUITY_WINDOW_MAP:
        return name, _EQUITY_WINDOW_MAP[name]
    return _EQUITY_DEFAULT_WINDOW, _EQUITY_WINDOW_MAP[_EQUITY_DEFAULT_WINDOW]


def _epoch_ts(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _downsample(pts: list, cap: int = 600) -> list:
    """Even-sample a long series down to <= cap points (keeps first + last)."""
    if len(pts) <= cap:
        return pts
    step = len(pts) / cap
    out = [pts[int(i * step)] for i in range(cap)]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def _equity_series(db, window_delta, live_total=None, live_by_ex=None) -> dict:
    """Per-exchange + aggregate ('Total') PnL series from EquitySnapshot rows inside
    the window. Each series is tipped with the live headline value so the right edge
    always matches the page. Returns {name: [(ts, value)]}; empty when no snapshots."""
    q = db.query(EquitySnapshot).order_by(EquitySnapshot.captured_at)
    if window_delta is not None:
        q = q.filter(EquitySnapshot.captured_at >= datetime.now(timezone.utc) - window_delta)
    total_pts: list = []
    ex_pts: dict[str, list] = {}
    for s in q.all():
        ts = s.captured_at
        if ts is not None and ts.tzinfo is None:        # SQLite returns naive
            ts = ts.replace(tzinfo=timezone.utc)
        total_pts.append((ts, float(s.total_pnl)))
        try:
            by = json.loads(s.by_exchange or "{}")
        except (ValueError, TypeError):
            by = {}
        for ex, val in by.items():
            ex_pts.setdefault(ex, []).append((ts, float(val)))
    if not total_pts:
        return {}
    series = {"Total": total_pts, **ex_pts}
    if live_total is not None:                          # tip every series to the live edge
        anchor = max(datetime.now(timezone.utc), total_pts[-1][0])
        series["Total"] = total_pts + [(anchor, float(live_total))]
        for ex in ex_pts:
            tip = (live_by_ex or {}).get(ex)
            if tip is not None:
                series[ex] = ex_pts[ex] + [(anchor, float(tip))]
    return {k: _downsample(v) for k, v in series.items()}


def _sharpe(equity_vals: list):
    """Annualized Sharpe (est.) from snapshot-to-snapshot equity returns (risk-free
    = 0, ~hourly cadence). None until there are enough points or once variance is 0."""
    rets = [(equity_vals[i] - equity_vals[i - 1]) / equity_vals[i - 1]
            for i in range(1, len(equity_vals)) if equity_vals[i - 1] > 0]
    if len(rets) < 8:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = var ** 0.5
    return (mean / sd) * (24 * 365) ** 0.5 if sd > 0 else None


def _equity_metrics(total_series: list, capital_base: float = 0.0):
    """Essential equity metrics over the Total series: current, period P&L (window
    Δ), peak (high-water mark), trough, and max drawdown. With a capital base set,
    also return-% (on capital), period-return-%, drawdown-% (vs peak equity), and an
    estimated annualized Sharpe. PnL is USD from 0; %-metrics need the capital base."""
    if not total_series:
        return None
    vals = [v for _, v in total_series]
    cur, start = vals[-1], vals[0]
    peak, trough = max(vals), min(vals)
    run, max_dd, dd_peak = vals[0], 0.0, vals[0]
    for v in vals:
        run = max(run, v)
        if run - v > max_dd:
            max_dd, dd_peak = run - v, run     # the running peak the largest drop fell from
    m = {
        "current": cur, "period": cur - start, "start": start,
        "peak": peak, "trough": trough, "max_drawdown": max_dd,
        "max_drawdown_pct": (max_dd / dd_peak * 100) if dd_peak > 0 else None,
        "dd_from_peak": peak - cur, "points": len(vals),
        "capital_base": capital_base or 0.0,
    }
    if capital_base and capital_base > 0:
        m["return_pct"] = cur / capital_base * 100
        m["period_return_pct"] = (cur - start) / capital_base * 100
        dd_peak_equity = capital_base + dd_peak
        m["max_drawdown_pct"] = (max_dd / dd_peak_equity * 100) if dd_peak_equity > 0 else None
        m["sharpe"] = _sharpe([capital_base + v for v in vals])
    return m


def _equity_svg(series, width: int = 920, height: int = 200, pad: int = 10):
    """Multi-series equity SVG: one polyline per series ({name: [(ts, v)]}) on a
    shared scale + a zero baseline. Accepts a bare point list (treated as the Total
    line). None when there's nothing to plot. The aggregate 'Total' is drawn last
    (on top) and thicker; venue lines are dimmer."""
    if isinstance(series, list):
        series = {"Total": series} if series else {}
    series = {k: v for k, v in series.items() if v}
    if not series:
        return None
    all_vals = [v for pts in series.values() for _, v in pts] + [0.0]
    lo, hi = min(all_vals), max(all_vals)
    span = (hi - lo) or 1.0
    all_ts = [_epoch_ts(ts) for pts in series.values() for ts, _ in pts]
    valid = [t for t in all_ts if t is not None]
    t0 = min(valid) if valid else 0.0
    tspan = (max(valid) - t0) if valid else 0.0

    def fy(v):
        return pad + (height - 2 * pad) * (1 - (v - lo) / span)

    def fx(t, i, n):
        if tspan and t is not None:
            return pad + (width - 2 * pad) * ((t - t0) / tspan)
        return pad + (width - 2 * pad) * (i / (n - 1) if n > 1 else 0.0)

    order = [k for k in series if k != "Total"] + (["Total"] if "Total" in series else [])
    palette = iter(_SERIES_PALETTE)
    lines = []
    for name in order:
        pts = series[name]
        n = len(pts)
        poly = " ".join(f"{fx(_epoch_ts(ts), i, n):.1f},{fy(v):.1f}"
                        for i, (ts, v) in enumerate(pts))
        if name == "Total":
            color = "#4ade80" if pts[-1][1] >= 0 else "#f87171"
        else:
            color = _SERIES_COLORS.get(name) or next(palette, "#9ca3af")
        lines.append({"name": name, "polyline": poly, "color": color,
                      "last": pts[-1][1], "is_total": name == "Total"})
    span_key = "Total" if "Total" in series else order[0]
    return {"lines": lines, "zero_y": round(fy(0.0), 1), "width": width,
            "height": height, "lo": lo, "hi": hi,
            "start_label": _fmt_when(series[span_key][0][0], "%m-%d %H:%M"),
            "end_label": _fmt_when(series[span_key][-1][0], "%m-%d %H:%M")}


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
def performance(request: Request, equity_window: str = Query(_EQUITY_DEFAULT_WINDOW)):
    wsel, wdelta = _resolve_window(equity_window)
    with session_scope() as db:
        perf = _performance(db, request.app.state.strategy_router)
        live_by_ex = {r["exchange"]: r["total"] for r in perf["per_exchange"]}
        series = _equity_series(db, wdelta, perf["totals"]["total"], live_by_ex)
        equity = _equity_svg(series)
        metrics = _equity_metrics(series.get("Total", []), get_settings().equity_capital_base)
        orders = _recent_orders(db, limit=50)
    resp = templates.TemplateResponse("performance.html", {
        "request": request, "perf": perf, "equity": equity, "metrics": metrics,
        "orders": orders, "equity_windows": [w for w, _ in _EQUITY_WINDOWS],
        "equity_window": wsel,
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
