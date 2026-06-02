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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
