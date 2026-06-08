import os
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

if _settings.database_url.startswith("sqlite"):
    db_path = _settings.database_url.replace("sqlite:///", "", 1)
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
    future=True,
)

if _settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # WAL keeps reads (dashboard, retry-worker scans) from blocking the
        # writer; busy_timeout makes concurrent writers WAIT for a lock instead
        # of erroring "database is locked". Both matter now that order placement
        # and the retry worker write SQLite from multiple threads.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=20000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# Additive columns introduced after the initial schema. create_all() only
# creates MISSING tables — it never alters an existing one — so a live SQLite DB
# won't gain these columns on deploy without an explicit ADD COLUMN. Each entry
# is idempotent (skipped if the column already exists) and additive only.
_SQLITE_ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "alerts": [("signal_price", "FLOAT")],
    "orders": [
        ("signal_price", "FLOAT"),
        ("fill_price", "FLOAT"),
        ("commission", "FLOAT NOT NULL DEFAULT 0.0"),
        ("commission_asset", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ],
    "strategy_positions": [
        ("avg_entry_price", "FLOAT NOT NULL DEFAULT 0.0"),
        ("realized_pnl", "FLOAT NOT NULL DEFAULT 0.0"),
    ],
    "equity_snapshots": [
        ("by_exchange", "TEXT NOT NULL DEFAULT '{}'"),
    ],
}


def _migrate_sqlite_columns() -> None:
    """Add new nullable/defaulted columns to existing SQLite tables in place.
    No-op when a column is already present, so it's safe to run on every boot."""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, cols in _SQLITE_ADDITIVE_COLUMNS.items():
            present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in present:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db() -> None:
    from . import models  # noqa: F401 — ensure models are registered
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_columns()


@contextmanager
def session_scope():
    s: Session = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
