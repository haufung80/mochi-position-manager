from __future__ import annotations
import json
import logging
import time
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


def _as_utc(dt):
    """A naive datetime is assumed UTC (SQLite drops tzinfo); an aware one passes
    through; None passes through. One place for the 'stored timestamps are UTC'
    assumption that the render/curve helpers all rely on."""
    if dt is not None and getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_when(dt, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Render a stored timestamp in the configured display timezone.

    Timestamps are written as UTC but SQLite drops tzinfo, so a naive value is
    assumed to be UTC. None renders empty (some columns are nullable)."""
    if dt is None:
        return ""
    return _as_utc(dt).astimezone(_display_tz()).strftime(fmt)


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


def _execution_quality_by_strategy(db) -> dict:
    """Per-strategy execution quality keyed by ``strategy_id`` — avg slippage (bps), fee
    drag, and filled/rejected/dead order counts. The EXECUTION analogue of the
    per-strategy P&L: it answers "is this strategy expensive / unreliable to TRADE",
    independent of whether its signal wins. Mirrors the portfolio-level
    ``_execution_quality`` but groups by strategy via the same ``Order -> Alert`` join
    ``_recent_orders`` uses (a rejected order carries an ``alert_id``, so it joins too).
    Reuses ``_slip_bps`` — no new slippage math.

    ``slippage_bps`` is the mean over SUCCESS fills that recorded both a signal and a fill
    price (None when none did). ``fees`` sums ``commission`` on success fills (USDT/USDC,
    ~1:1). ``rejected`` is a portfolio sizing decision (no order sent), ``dead`` is an
    order that exhausted retries — the real execution-failure signal."""
    out: dict[str, dict] = {}
    # Column-select (not whole Order entities) — only 5 scalars are needed, and this scans
    # ALL orders; hydrating full ORM rows would grow costly as order history accumulates.
    rows = (db.query(Alert.strategy_id, Order.status, Order.commission,
                     Order.fill_price, Order.signal_price, Order.side)
              .join(Alert, Order.alert_id == Alert.id).all())
    for sid, status, commission, fill_price, signal_price, side in rows:
        s = out.setdefault(sid, {"slips": [], "fees": 0.0,
                                 "filled": 0, "rejected": 0, "dead": 0})
        if status == "success":
            s["filled"] += 1
            s["fees"] += commission or 0.0
            bps = _slip_bps(fill_price, signal_price, side)
            if bps is not None:
                s["slips"].append(bps)
        elif status == "rejected":
            s["rejected"] += 1
        elif status == "dead":
            s["dead"] += 1
    return {sid: {
        "slippage_bps": (sum(v["slips"]) / len(v["slips"])) if v["slips"] else None,
        "fees": v["fees"], "filled": v["filled"],
        "rejected": v["rejected"], "dead": v["dead"],
    } for sid, v in out.items()}


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


def _by_exchange_totals(perf: dict) -> dict:
    """{exchange: total_pnl} from a _performance() result — the per-venue equity
    values. Used by BOTH the page's live tip and the snapshot writer, so the stored
    `by_exchange` history and the live right-edge can never key venues differently."""
    return {r["exchange"]: r["total"] for r in perf["per_exchange"]}


def _by_strategy_totals(perf: dict) -> dict:
    """{strategy_id: total_pnl} from a _performance() result, SORTED by id for stable
    line colors. Each `total` = realized + unrealized (live MTM) − commission (funding is
    exchange-level, never per-strategy). Used by BOTH the page's live tip and the snapshot
    writer, so stored `by_strategy` history and the live right-edge key strategies
    identically."""
    return {r["strategy_id"]: r["total"]
            for r in sorted(perf["per_strategy"], key=lambda r: r["strategy_id"])}


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
    return _equity_series(_load_snapshots(db), None, live_total=end_total).get("Total", [])


_EQUITY_WINDOWS = [
    ("24h", timedelta(hours=24)), ("7D", timedelta(days=7)),
    ("30D", timedelta(days=30)), ("90D", timedelta(days=90)),
    ("180D", timedelta(days=180)), ("365D", timedelta(days=365)),
    ("All", None),
]
_EQUITY_WINDOW_MAP = dict(_EQUITY_WINDOWS)
_EQUITY_DEFAULT_WINDOW = "30D"
# bybit brand-orange / hyperliquid teal; any other venue cycles a fallback palette.
_SERIES_COLORS = {"bybit": "#f7a600", "hyperliquid": "#26a69a"}
_SERIES_PALETTE = ["#a78bfa", "#60a5fa", "#fbbf24", "#f472b6", "#34d399"]


def _resolve_window(name: str):
    """(validated_name, timedelta|None). Unknown name -> the default window."""
    if name in _EQUITY_WINDOW_MAP:
        return name, _EQUITY_WINDOW_MAP[name]
    return _EQUITY_DEFAULT_WINDOW, _EQUITY_WINDOW_MAP[_EQUITY_DEFAULT_WINDOW]


def _epoch_ts(dt):
    dt = _as_utc(dt)
    return dt.timestamp() if dt is not None else None


def _downsample(pts: list, cap: int = 600) -> list:
    """Even-sample a long series down to <= cap points (keeps first + last)."""
    if len(pts) <= cap:
        return pts
    step = len(pts) / cap
    out = [pts[int(i * step)] for i in range(cap)]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


# --- equity dataset cache: switching timeframes / rapid reloads reuse this instead
# --- of re-fetching the exchanges and re-reading snapshots on every render. ---
_EQ_CACHE: dict = {"at": 0.0, "snapshots": None, "perf": None,
                   "strat_snapshots": None, "exec_quality": None}
_EQ_CACHE_TTL = 30.0


def _load_snapshots(db) -> list[tuple]:
    """All equity snapshots as (ts_utc, total_pnl, by_exchange_dict), time-ordered."""
    out: list[tuple] = []
    for s in db.query(EquitySnapshot).order_by(EquitySnapshot.captured_at).all():
        ts = _as_utc(s.captured_at)                      # SQLite returns naive -> assume UTC
        try:
            by = json.loads(s.by_exchange or "{}")
        except (ValueError, TypeError):
            by = {}
        out.append((ts, float(s.total_pnl), by))
    return out


def _load_strategy_snapshots(db) -> list[tuple]:
    """Per-strategy equity snapshots as (ts_utc, Σ_strategies, by_strategy_dict),
    time-ordered — the SAME 3-tuple shape `_equity_series` consumes, so the per-strategy
    curve reuses it unchanged. Σ_strategies = Σ of the per-strategy totals (realized +
    unrealized − commission; excludes exchange-level funding). Rows with NO per-strategy
    breakdown (backfilled / pre-feature) are skipped so the curve starts at the first
    populated point (no flat-zero prefix)."""
    out: list[tuple] = []
    for s in db.query(EquitySnapshot).order_by(EquitySnapshot.captured_at).all():
        try:
            by = json.loads(s.by_strategy or "{}")
            # Coerce per-value (like _load_snapshots) so a malformed/None value in one row
            # can't TypeError out of sum() and 500 the whole page — drop the bad row instead.
            by = {k: float(v) for k, v in by.items()}
        except (ValueError, TypeError, AttributeError):
            by = {}
        if not by:                                       # no per-strategy data on this row
            continue
        out.append((_as_utc(s.captured_at), float(sum(by.values())), by))
    return out


def _equity_dataset(db, router, force: bool = False):
    """(snapshots, perf, strat_snapshots, exec_quality) cached for _EQ_CACHE_TTL seconds —
    EVERYTHING the /performance render needs (both equity curves' rows, the headline, and
    the per-strategy execution table), so switching windows or reloading within the TTL
    re-fetches nothing: not the exchanges (perf), not the snapshot rows (by_exchange AND
    by_strategy), not the full orders scan (exec_quality). The funding worker calls
    _performance directly (uncached) for fresh snapshots; ?refresh=true forces a rebuild."""
    now = time.time()
    if force or _EQ_CACHE["snapshots"] is None or now - _EQ_CACHE["at"] > _EQ_CACHE_TTL:
        _EQ_CACHE["snapshots"] = _load_snapshots(db)
        _EQ_CACHE["perf"] = _performance(db, router)
        _EQ_CACHE["strat_snapshots"] = _load_strategy_snapshots(db)
        _EQ_CACHE["exec_quality"] = _execution_quality_by_strategy(db)
        _EQ_CACHE["at"] = now
    return (_EQ_CACHE["snapshots"], _EQ_CACHE["perf"],
            _EQ_CACHE["strat_snapshots"], _EQ_CACHE["exec_quality"])


def _clear_equity_cache() -> None:
    """Drop the cached dataset (test isolation / forced refresh)."""
    _EQ_CACHE.update(at=0.0, snapshots=None, perf=None,
                     strat_snapshots=None, exec_quality=None)


def _equity_series(snapshots, window_delta, live_total=None, live_by_ex=None) -> dict:
    """Per-exchange + aggregate ('Total') PnL series from in-memory `snapshots` (from
    _load_snapshots) inside the window. Each series is tipped with the live headline
    value so the right edge matches the page. Returns {name: [(ts, value)]}; empty
    when there are no snapshots in the window."""
    rows = snapshots
    if window_delta is not None:
        cutoff = datetime.now(timezone.utc) - window_delta
        rows = [r for r in snapshots if r[0] is not None and r[0] >= cutoff]
    total_pts: list = []
    ex_pts: dict[str, list] = {}
    for ts, total, by in rows:
        total_pts.append((ts, total))
        for ex, val in by.items():
            ex_pts.setdefault(ex, []).append((ts, float(val)))
    if not total_pts:
        # No snapshot inside the window. If history EXISTS (just outside this window)
        # and there's a live value, still show that single live point so a short window
        # on an account that has history never renders blank. With NO snapshots at all
        # it's the genuine empty state ("No snapshots yet") — don't draw a lone spike.
        if live_total is None or not snapshots:
            return {}
        now = datetime.now(timezone.utc)
        out = {"Total": [(now, float(live_total))]}
        for ex, tip in (live_by_ex or {}).items():
            if tip is not None:
                out[ex] = [(now, float(tip))]
        return out
    series = {"Total": total_pts, **ex_pts}
    if live_total is not None:                          # tip every series to the live edge
        anchor = max(datetime.now(timezone.utc), total_pts[-1][0])
        series["Total"] = total_pts + [(anchor, float(live_total))]
        for ex in ex_pts:
            tip = (live_by_ex or {}).get(ex)
            if tip is not None:
                series[ex] = ex_pts[ex] + [(anchor, float(tip))]
    return {k: _downsample(v) for k, v in series.items()}


def _sharpe(equity_points: list):
    """Annualized Sharpe (est.) from DAILY equity returns: resample to the last value
    per UTC day, then mean/std of day-over-day returns × √365 (risk-free = 0). Daily
    is the stable, standard basis — the snapshots are mixed daily (backfill) + hourly
    (live), so per-snapshot returns would mis-annualize. `equity_points` = [(ts, value)].
    None until there are >= 8 daily returns or variance is 0. (Still an estimate:
    smooth backfilled history understates volatility, so early values read high.)"""
    by_day: dict = {}
    for ts, v in equity_points:
        if ts is not None:
            by_day[ts.date()] = v               # last value seen for each UTC day
    vals = [by_day[d] for d in sorted(by_day)]
    rets = [(vals[i] - vals[i - 1]) / vals[i - 1]
            for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(rets) < 8:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = var ** 0.5
    return (mean / sd) * (365 ** 0.5) if sd > 0 else None


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
        m["sharpe"] = _sharpe([(ts, capital_base + v) for ts, v in total_series])
        # APR: annualize the return-on-capital over the data's actual span. Needs real
        # timestamps + >= ~half a day so the first hour doesn't annualize to nonsense.
        ts0, ts1 = total_series[0][0], total_series[-1][0]
        days = (ts1 - ts0).total_seconds() / 86400 if (ts0 and ts1) else 0.0
        if days >= 0.5:
            m["apr"] = (cur / capital_base) * (365.0 / days) * 100
            m["apr_days"] = days
    return m


def _equity_chart_payload(series, capital_base: float = 0.0) -> dict | None:
    """Convert an `_equity_series` dict ({name: [(ts, value)]}) into an ECharts-ready
    payload, rendered client-side by renderEChart in app.js (used by both /performance
    and /funding-arb). Series ordering + colors: Total last (drawn on top), green/red by
    sign; venues branded; others cycle the palette. `data` points are [epoch_ms, value]
    for ECharts' time axis. `capital_base` is carried through as the account-value basis
    (reserved for the right-axis follow-up). None when there's nothing to plot."""
    if not series:
        return None
    order = [k for k in series if k != "Total"] + (["Total"] if "Total" in series else [])
    palette = iter(_SERIES_PALETTE)
    out = []
    for name in order:
        pts = series.get(name) or []
        color = (("#4ade80" if pts[-1][1] >= 0 else "#f87171") if name == "Total"
                 else (_SERIES_COLORS.get(name) or next(palette, "#9ca3af")))
        data = [[int(e * 1000), round(float(v), 4)]
                for ts, v in pts if (e := _epoch_ts(ts)) is not None]
        if data:
            out.append({"name": name, "color": color,
                        "is_total": name == "Total", "last": pts[-1][1], "data": data})
    if not out:
        return None
    return {"series": out, "capital_base": float(capital_base or 0.0)}


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
        "fee_source": o.fee_source,            # "unavailable" -> commission is a 0 placeholder
        "realized_pnl": o.realized_pnl,        # realized this fill produced (closed portion)
        "slippage_bps": _slip_bps(o.fill_price, o.signal_price, o.side),
        "error": o.error_message,
    } for o, sid in rows]


@router.get("/performance", response_class=HTMLResponse)
def performance(request: Request, equity_window: str = Query(_EQUITY_DEFAULT_WINDOW),
                refresh: bool = Query(False)):
    wsel, wdelta = _resolve_window(equity_window)
    with session_scope() as db:
        # Cached dataset: snapshots + perf + per-strategy snapshots + exec-quality, reused
        # across timeframe switches so changing the window re-fetches nothing within the
        # TTL (one orders scan, not one per render). ?refresh=true forces it.
        snapshots, perf, strat_snapshots, exec_quality = _equity_dataset(
            db, request.app.state.strategy_router, force=refresh)
        live_by_ex = _by_exchange_totals(perf)
        series = _equity_series(snapshots, wdelta, perf["totals"]["total"], live_by_ex)
        cap = get_settings().equity_capital_base
        equity = _equity_chart_payload(series, capital_base=cap)   # ECharts payload (app.js draws it)
        metrics = _equity_metrics(series.get("Total", []), cap)
        # Per-strategy curve: one line PER strategy, fed the by_strategy breakdown. The
        # aggregate ("Total"/"Σ strategies") is dropped — it excludes exchange-level
        # funding, so it's a misleading partial; the true Total (with funding) + its
        # metric cards live on the by-exchange chart and the headline.
        strat_live = _by_strategy_totals(perf)
        strat_series = _equity_series(
            strat_snapshots, wdelta,
            sum(strat_live.values()) if strat_live else None, strat_live)
        strat_equity = _equity_chart_payload(
            {k: v for k, v in strat_series.items() if k != "Total"})
        # Execution-quality row order: the by-strategy P&L order (perf.per_strategy) FIRST,
        # then any strategy that has orders but no fill/position (all rejected/dead) — else
        # a fail-only strategy, the table's whole point, would never render.
        seen = {r["strategy_id"] for r in perf["per_strategy"]}
        exec_order = ([r["strategy_id"] for r in perf["per_strategy"]]
                      + sorted(s for s in exec_quality if s not in seen))
        orders = _recent_orders(db, limit=50)
    resp = templates.TemplateResponse("performance.html", {
        "request": request, "perf": perf, "equity": equity, "metrics": metrics,
        "strat_equity": strat_equity,
        "exec_quality": exec_quality, "exec_order": exec_order,
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
