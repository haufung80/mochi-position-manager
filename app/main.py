from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .config import get_settings
from .db import init_db
from .routing import StrategyRouter
from .routes.webhook import router as webhook_router
from .routes.dashboard import router as dashboard_router
from .retry_worker import retry_loop


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger("middleware")

    init_db()

    strategies_path = Path(settings.strategies_file)
    if not strategies_path.exists():
        log.warning("strategies.yaml not found at %s — using empty router. "
                    "Copy strategies.yaml.example and configure.", strategies_path)
        # build a router pointing at a stub to keep the app booting
        stub = strategies_path.parent / ".strategies-empty.yaml"
        stub.write_text("strategies: {}\n")
        app.state.strategy_router = StrategyRouter(stub)
    else:
        app.state.strategy_router = StrategyRouter(strategies_path)
        log.info("Loaded %d strategies from %s",
                 len(app.state.strategy_router.all()), strategies_path)

    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(
        retry_loop(app.state.strategy_router, stop_event=stop_event)
    )
    log.info("Middleware ready. dry_run=%s", settings.dry_run)
    try:
        yield
    finally:
        stop_event.set()
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="TradingView Middleware",
    version="0.1.0",
    description="Webhook ingress for TradingView alerts; routes to Bybit / Hyperliquid.",
    lifespan=lifespan,
)

app.include_router(webhook_router)
app.include_router(dashboard_router)
