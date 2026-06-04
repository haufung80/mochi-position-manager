# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FastAPI middleware between TradingView alerts and exchanges (Bybit + Hyperliquid). It persists every
signal to SQLite first, deduplicates, retries failed orders with backoff, and Telegram-alerts on
events. It fires **market orders only** and tracks *intent* (signal-derived net), not real exchange
state — the two can drift if you also trade the account manually.

Two execution modes per strategy, chosen by the `sar` flag: **alert-driven** (`sar=true` — submit the
alert's quantity) and **managed** (`sar=false` — the portfolio manager sizes from a per-strategy USDT
`position_size`: open-when-flat / close-on-opposite / reject-pyramiding). It also tracks per-strategy
realized + unrealized PnL with a commission / slippage / funding breakdown on a `/performance` page.

## Commands

```bash
# Tests (DRY_RUN + a temp SQLite DB are forced in tests/conftest.py — no real creds needed)
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m pytest tests/test_webhook.py -v                       # one file
.venv/bin/python -m pytest tests/test_webhook.py::test_name -v            # one test

# Dev server (auto-reload)
.venv/bin/uvicorn app.main:app --reload --port 8000
# or: .venv/bin/python run.py   (run.py chdirs to repo root so .env / strategies.yaml / data/ resolve)

# Install
.venv/bin/pip install -r requirements.txt
```

There is no linter/formatter configured and no pytest config file; pytest discovery uses defaults.

## Configuration (two separate sources — don't conflate them)

- **`.env`** → `app/config.py` (`Settings`, pydantic-settings, cached via `get_settings()`): secrets,
  retry policy, `DRY_RUN`, `DISPLAY_TIMEZONE`. `get_settings()` is `@lru_cache`d — tests call
  `get_settings.cache_clear()` after mutating env.
- **`strategies.yaml`** → routing + sizing. **Schema (current):** per-strategy `base_asset`
  (BTC/ETH/SOL/BNB/XRP) + `venues: {exchange: bool}` + optional `sar: bool` + optional
  `position_size: float` (USDT notional). The README's `exchange/symbol/quantity_usd/leverage` shape is
  OUTDATED. **The `sar` flag picks the execution mode** (`app/portfolio.py::decide`): `sar=true` =
  alert-driven (submit the alert's `quantity`, which is then REQUIRED); `sar=false` = managed (size from
  `position_size` per venue — open-flat / close-opposite / reject-pyramid; no `position_size` → paper
  mode = one min-unit order + Telegram warning). Parsed into `StrategyRoute.{sar, position_size}`.

```yaml
strategies:
  MR_VOTING_BTC_6H:
    base_asset: BTC          # canonical: BTC / ETH / SOL / BNB / XRP (app/exchanges/symbols.py)
    position_size: 1000      # USDT notional, per venue; omit -> paper mode (sar=false managed)
    venues:
      bybit: true            # symbol resolved at load time: BTC -> BTCUSDT (bybit), BTC (hyperliquid)
      hyperliquid: false
```

## Request flow (the core architecture)

`POST /webhook/tradingview` (`app/routes/webhook.py`) is the only inbound endpoint and is split into a
**fast synchronous path** and a **background fan-out** — this split is load-bearing:

1. **Synchronous, must be sub-second** (TradingView's webhook times out in a few seconds; a slow
   response is logged as a failure even when the order filled): parse → `_authorise` (secret check) →
   compute `idempotency_key` → `_persist_alert`. The dedup gate is the `UNIQUE` constraint on
   `alerts.idempotency_key`: a duplicate raises `IntegrityError`, caught and returned as
   `{status: duplicate}`. The request returns `accepted` immediately.
2. **Background fan-out** (`_run_fan_out` via FastAPI `BackgroundTasks`): the blocking exchange SDKs
   (pybit, hyperliquid) run here. FastAPI executes sync background functions in a **threadpool**, so
   they never block the event loop. One **independent DB transaction per venue** (`session_scope()`),
   so a failure on one exchange can't roll back another's fill. Per venue, `app/portfolio.py::decide`
   computes the order intent first (managed sizing for `sar=false`; the alert quantity for `sar=true`);
   a REJECT records a `status="rejected"` audit Order + Telegram alert instead of placing one.

`execute_order` (`app/executor.py`) drives one Order row through `pending → success | retrying | dead`,
and mutates the ledger **only on a successful fill**. Same function handles first attempt and retries
(`existing_order=` param).

## Background retry worker

`app/retry_worker.py` `retry_loop` is started in `app/main.py`'s lifespan. Every 5s it polls
`status=retrying AND next_retry_at <= now` and replays via `execute_order`. The actual work runs in
`asyncio.to_thread` (blocking SDK calls). Backoff is exponential (`_next_retry_delay`: base·3^(n-1),
capped); after `RETRY_MAX_ATTEMPTS` the order is marked `dead` + Telegram-pinged (no auto-replay).
**Retries reconstruct the `VenueRoute` from the Order's own frozen fields**, so they survive strategy
reconfiguration between fire and retry — they don't re-read `strategies.yaml`.

## Background funding poller

`app/funding_worker.py` `funding_loop` is started in lifespan alongside the retry worker (hourly,
**sleep-first** so startup stays network-free). It records funding payments into `funding_events`
(idempotent via `UNIQUE(exchange, symbol, funding_time)` + insert-or-ignore) so `/performance` can sum
funding without a per-request API call. Per-strategy funding is attributed only when a
`(exchange, symbol)` is solely owned (`reconcile.single_owner_map`), else exchange/portfolio-level only.

## Two ledgers (both updated on every fill)

`_apply_fill_to_position` updates both:
- `Position` — net per `(exchange, symbol)` via an atomic in-SQL UPSERT (`_bump_position`), so concurrent
  fills from different strategies on the same row can't lose increments.
- `StrategyPosition` — net + `avg_entry_price` + `realized_pnl` per `(strategy_id, exchange, symbol)`,
  via read-modify-write (`_apply_strategy_fill` → pure `_fill_math`: weighted-avg entry, realize on
  close, cross-zero reversal) **serialized by `_LEDGER_LOCK`** — a process-wide lock held across a
  snapshot-refreshing `commit → read → apply → commit`, because SQLite deferred transactions + WAL
  don't stop a stale read from losing an update (the aggregate UPSERT is immune; this RMW is not).
  PnL is gross of fees; commission/slippage live on `Order`, funding in `funding_events`; `/performance`
  (`_performance`/`_equity_curve` in `app/routes/dashboard.py`) sums them.

The exchange has no concept of "which strategy"; `app/reconcile.py` re-baselines `StrategyPosition` to
live exchange state, but **skips any `(exchange, symbol)` claimed by >1 strategy** (an aggregate can't be
attributed). Triggered from the admin UI (`/admin/strategies/sync-positions`).

## Routing & symbols

- `app/routing.py` `StrategyRouter` parses YAML into immutable `StrategyRoute`/`VenueRoute` and holds an
  **in-memory cache** on `app.state.strategy_router`. The hot path only reads memory; disk I/O happens
  only on `.reload()`, which the admin endpoints call after every write. Bad individual entries are
  logged and skipped — never fatal at startup.
- `app/exchanges/symbols.py` is the single source of truth for supported assets/venues. Adding an
  exchange = one entry in `EXCHANGE_QUOTE_SUFFIX` + an adapter under `app/exchanges/`.

## Exchanges

`app/exchanges/base.py` defines the `Exchange` Protocol (`market_order`, `close_position`, `get_position`).
`registry.py` lazy-constructs one adapter per exchange on first use (singleton via `get_registry()`), so
the app boots even with one exchange's creds missing. `reset_registry()` is the test hook. New adapters
(bybit/hyperliquid) return the normalized `OrderResult` (`app/schemas.py`) with **actual VWAP fill price
+ real commission**, fetched best-effort after the fill (fall back to 0 / mark; never block the order).

## Database notes

- SQLite with `PRAGMA journal_mode=WAL` + `busy_timeout=20000` (`app/db.py`) — required because order
  placement and the retry worker write from multiple threads.
- **Schema migrations are hand-rolled and additive only.** `Base.metadata.create_all()` only creates
  missing *tables*, never alters existing ones. New columns must be added to `_SQLITE_ADDITIVE_COLUMNS`
  in `app/db.py`; `_migrate_sqlite_columns()` runs idempotent `ALTER TABLE ADD COLUMN` on every boot.
  Alembic is in requirements but not wired up. When you add a model column, add it there too or live
  deploys won't get it.

## Admin

`app/routes/admin.py` serves an HTML UI at `/admin/strategies` (Jinja templates in `app/templates/`) to
CRUD strategies and toggle venues. **Writes are authorized by the same `WEBHOOK_SECRET`** (submitted as a
form field), not a separate password. All persistence goes through `app/strategy_store.py` (atomic
temp-file-then-rename YAML writes); admin handlers only do HTTP shape + validation + router reload.

## Testing conventions

`tests/conftest.py` sets `WEBHOOK_SECRET`/`DRY_RUN`/`DATABASE_URL` **before** app import (pydantic reads
env at import time), recreates the DB per test (`_clean_db` autouse fixture), and provides fixtures:
`strategies_yaml` (TEST_BTC / TEST_MULTI / TEST_DISABLED), `stub_exchange` (recording FakeExchange that
replaces the registry), `silent_notifier`. Use these rather than touching real creds/exchanges.

## Deploy

Runs on a single **AWS Lightsail** VM (Singapore, for Bybit latency) as a two-container Docker Compose
stack (`docker-compose.prod.yml`): the FastAPI `app` (not exposed to the host) behind a **Caddy**
reverse proxy that terminates HTTPS for `mochi-position-manager.duckdns.org` (auto Let's Encrypt).
SQLite (`middleware.db`) + `strategies.yaml` live on the host at `./data` (bind-mounted to `/app/data`),
so they survive restarts/redeploys; Caddy's certs persist in the `caddy_data` volume.

**CI/CD (`.github/workflows/ci-cd.yml`):** every PR and push to `main` runs the suite with a coverage
gate (`pytest --cov=app --cov-fail-under=75`). On a green **push to main** the workflow SSH-deploys to
the box (`git pull --ff-only && docker compose -f docker-compose.prod.yml up -d --build`) and
health-checks it. Needs repo secrets `LIGHTSAIL_HOST` / `LIGHTSAIL_USER` / `LIGHTSAIL_SSH_KEY`; absent
them the deploy job no-ops (tests still gate). **Keep `main` green — a red test/coverage run blocks the
deploy.** The box checkout lives at `/home/ubuntu/mochi`.

**Manual fallback** (on the box): `cd ~/mochi && git pull && docker compose -f docker-compose.prod.yml
up -d --build`. (`fly.toml` is a vestige of the earlier Fly.io setup.) See README.md for full
deploy/TradingView-alert setup.
