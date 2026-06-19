from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, DateTime, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Alert(Base):
    """Every distinct webhook hit lands here exactly once (dedup gate)."""
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_alerts_idemp"),
        Index("ix_alerts_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # buy / sell / close
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    # Signal price from the TV payload ({{close}} of the triggering bar); the
    # reference we measure fill slippage against. Nullable: older alerts and any
    # payload without a `price` field leave it unset.
    signal_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    source_ip: Mapped[str] = mapped_column(String(64), default="")


class Order(Base):
    """One row per attempt to push the alert to an exchange.
    An Alert may have several Orders if retries occur, but at most one with
    status=success."""
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_status", "status"),
        Index("ix_orders_alert_id", "alert_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy / sell
    qty_usd: Mapped[float] = mapped_column(Float, nullable=False)
    qty_base: Mapped[float] = mapped_column(Float, default=0.0)  # filled in once we know price
    reduce_only: Mapped[bool] = mapped_column(default=False)
    leverage: Mapped[float] = mapped_column(Float, default=1.0)

    # --- execution quality (live-vs-backtest monitoring) ---
    # signal_price: copied from the alert ({{close}}); fill_price: actual VWAP
    # fill from the exchange. slippage = fill vs signal. commission: real fee
    # charged for this fill, in commission_asset units (USDT bybit / USDC HL).
    signal_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commission_asset: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # Fidelity of `commission` (and `fill_price`): "exchange" = real fee fetched from
    # the venue at fill time; "backfill" = recovered later from trade history;
    # "unavailable" = the post-fill enrichment fetch failed, so commission is a 0
    # PLACEHOLDER (and on Bybit fill_price is a mark estimate); "dry_run" = simulated.
    # Makes a 0 fee unambiguous (real zero vs. not-yet-captured) and is the backfill's
    # work-list. Empty "" = legacy row written before this column (treat as unknown).
    fee_source: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # Realized PnL this fill produced (the portion of the position it CLOSED), USDT,
    # gross of fees — the per-fill `realized_delta` from `_fill_math`, the same number
    # the per-strategy `realized_pnl` accumulates. 0.0 for an open/increase (nothing
    # closed) and for rejected/paper orders (never filled).
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # pending -> success | failed | retrying | dead
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    exchange_order_id: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Position(Base):
    """Internal ledger — net position size per (exchange, symbol).
    Updated only on successful order fills.
    Source of truth for the dashboard, NOT for execution decisions."""
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", name="uq_positions_exchange_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    net_qty_base: Mapped[float] = mapped_column(Float, default=0.0)  # positive=long, negative=short
    net_qty_usd: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StrategyPosition(Base):
    """Net position per (strategy, exchange, symbol) — like Position, but
    attributed to the strategy whose fills produced it, so the dashboard can
    show each strategy's own exposure. Updated on fills; can be re-baselined
    to live exchange state via the admin 'sync to exchange' action."""
    __tablename__ = "strategy_positions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "exchange", "symbol", name="uq_stratpos"),
        Index("ix_stratpos_strategy", "strategy_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    net_qty_base: Mapped[float] = mapped_column(Float, default=0.0)
    net_qty_usd: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    # Volume-weighted entry price of the OPEN position + cumulative realized PnL
    # (USDT, gross of fees — fees are tracked on Order + FundingEvent).
    avg_entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class FundingEvent(Base):
    """One funding payment per (exchange, symbol, funding_time), polled from the
    exchange so the performance page can sum funding without an API call per
    request. `amount` is signed USDT/USDC: positive = received, negative = paid.
    The unique constraint makes the poller idempotent (insert-or-ignore)."""
    __tablename__ = "funding_events"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "funding_time", name="uq_funding_event"),
        Index("ix_funding_exchange_symbol", "exchange", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


# ===========================================================================
# Funding-arbitrage execution (multi-leg). These four tables are created by
# create_all and are STRUCTURALLY ISOLATED from the directional tables above:
# no directional query (orders/_execution_quality, funding_events/_performance/
# _equity_curve, reconcile) touches them, so arb rows can never bleed into the
# directional dashboard. Nothing here edits Order/Alert/Position/
# StrategyPosition/FundingEvent or app/db.py / _SQLITE_ADDITIVE_COLUMNS.
# ===========================================================================


class ArbPosition(Base):
    """One delta-neutral funding-arb position (a pair of legs tracked as a unit).

    `idempotency_key` is the dedup gate (mirrors Alert): a repeated open returns
    'duplicate' instead of opening a second pair. `notional_target` is the
    per-leg USD notional the signal app asked for; `realized_pnl` accrues on
    close. `status`: opening | open | closing | closed | error."""
    __tablename__ = "arb_positions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_arb_positions_idemp"),
        Index("ix_arb_positions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notional_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    # opening -> open | closing -> closed | error
    status: Mapped[str] = mapped_column(String(16), default="opening", nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class ArbLeg(Base):
    """One self-describing leg of an arb pair: (exchange, account, product,
    symbol, side, target_qty). Identity on the leg is what lets one schema
    express every combo (single-venue HL/Bybit cash-and-carry, cross-exchange
    perp-perp, spot + cross-perp) and any future N-leg arb.

    `filled_qty` stores the NET-received base quantity — for a Bybit spot BUY
    whose fee is base-denominated, `filled_qty = ordered_base - base_fee` (the
    actually-held, hedgeable amount that neutrality + close are measured against).
    UNIQUE(arb_id, exchange, product, symbol) is the leg-level idempotency guard
    (a re-entered open task can't create a second leg set)."""
    __tablename__ = "arb_legs"
    __table_args__ = (
        UniqueConstraint("arb_id", "exchange", "product", "symbol", name="uq_arb_legs_identity"),
        Index("ix_arb_legs_arb_id", "arb_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    account: Mapped[str] = mapped_column(String(32), nullable=False, default="arb")
    product: Mapped[str] = mapped_column(String(8), nullable=False)   # spot | perp
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)      # buy | sell
    target_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # NET base received
    avg_fill: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Reference mid at order time (the leg's own venue), captured so execution
    # slippage = avg_fill vs ref can be reported. 0 = not captured (legacy legs).
    ref_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Base grid step in force when the leg was placed, captured so the neutrality
    # tolerance (|skew| <= coarse_step/2) is judged against the OPEN-time grid with NO
    # read-path exchange call (and is stable across an adapter being down / re-tiered).
    # 0 = not captured (legacy legs) -> reporting falls back to a live lookup.
    grid_step: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commission: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commission_asset: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    funding: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Realized directional P&L captured at CLOSE = signed_open_qty·(close mark − avg_fill).
    # Lets a closed leg KEEP the basis P&L it earned, instead of its live MTM dropping to
    # 0 at close (0 while the leg is still open). Per-leg so by-venue + half-closed
    # ("error") arbs attribute exactly.
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # pending -> success | retrying | dead | error
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class ArbOrder(Base):
    """One attempt to push an arb LEG to an exchange — mirrors Order's execution
    fields but adds first-class `account` and `product` columns (so a retry
    re-resolves the exact (exchange, account) adapter and BTCUSDT spot vs linear
    never collide) and links to `arb_leg_id` (NOT NULL, indexed) instead of an
    `alert_id`. Structurally invisible to every directional query (it is NOT the
    `orders` table), so arb fills never inflate `_execution_quality` / `/orders`."""
    __tablename__ = "arb_orders"
    __table_args__ = (
        Index("ix_arb_orders_status", "status"),
        Index("ix_arb_orders_leg_id", "arb_leg_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arb_leg_id: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    account: Mapped[str] = mapped_column(String(32), nullable=False, default="arb")
    product: Mapped[str] = mapped_column(String(8), nullable=False)   # spot | perp
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)      # buy | sell
    qty_base: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_fill: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commission_asset: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    # pending -> success | retrying | dead
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    exchange_order_id: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class ArbFundingEvent(Base):
    """One funding payment for an arb perp leg, keyed by (exchange, account,
    symbol, funding_time). `arb_id` is nullable so a settlement can be recorded
    even before it is attributed. Keeps the directional FundingEvent table pure —
    arb funding can never reach the directional funding/equity queries. `amount`
    is signed (+received, -paid)."""
    __tablename__ = "arb_funding_events"
    __table_args__ = (
        UniqueConstraint("exchange", "account", "symbol", "funding_time",
                         name="uq_arb_funding_event"),
        Index("ix_arb_funding_arb_id", "arb_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    account: Mapped[str] = mapped_column(String(32), nullable=False, default="arb")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ArbEquitySnapshot(Base):
    """Periodic capture of the funding-arb book's NET (funding − commission, + basis)
    at a point in time — the points the /funding-arb equity curve plots. Mirrors
    EquitySnapshot but lives in its OWN table, so the directional /performance curve
    (which reads only `equity_snapshots`) stays physically blind to the arb book (the
    isolation invariant). Built forward from the first arb; no exchange backfill (the
    arb book has no pre-history)."""
    __tablename__ = "arb_equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    net: Mapped[float] = mapped_column(Float, nullable=False)
    funding: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commission: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Per-venue net {exchange: funding − commission} as JSON, so the curve draws one
    # line per venue plus the aggregate (single-venue HL today → one line).
    by_venue: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="live", nullable=False)


class EquitySnapshot(Base):
    """Periodic capture of the TRUE total PnL (realized + unrealized + funding −
    commission) at a point in time, written hourly by the funding worker. The
    equity curve plots these directly — each point is the real total at capture
    time, so the line needs no fill-replay reconstruction (which can't handle
    fills that predate fill-price capture). Forward-looking: the curve builds from
    the first capture after deploy; earlier history isn't reconstructed."""
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    total_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    realized: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unrealized: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    funding: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    commission: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Per-exchange total PnL at capture time as a JSON object {exchange: total},
    # so the curve can draw one line per venue plus the aggregate. JSON keeps it
    # flexible as venues come and go without a schema change.
    by_exchange: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # Per-strategy total PnL at capture time as a JSON object {strategy_id: total},
    # where total = realized + unrealized (live MTM) − commission (funding is
    # exchange-level, never per-strategy). Powers the per-strategy equity curve. Only
    # populated by the live worker (forward-looking) — backfilled rows leave it "{}"
    # since per-strategy history isn't on the exchange to reconstruct.
    by_strategy: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # "live" = captured by the worker; "backfill" = imported from the exchanges'
    # own history. Lets a re-run replace only the backfilled rows, never live ones.
    source: Mapped[str] = mapped_column(String(16), default="live", nullable=False)


class AppMeta(Base):
    """Tiny key-value store for app-level markers that must survive restarts but
    don't fit another table — e.g. the equity-backfill fingerprint, so the one-time
    backfill re-runs when its version/start changes (a code fix) without needing
    manual DB surgery, but stays one-time across ordinary reboots."""
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
