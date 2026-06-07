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

## Funding-arb execution layer (the `funding-arb` branch)

A second, structurally-separate feature lives on this app: a **delta-neutral funding-arb EXECUTION
layer**. A separate app (`mochi-carry-signal`) DECIDES and POSTs; THIS app executes both legs, tracks
the pair as one unit, retries, and reports funding − fees. It is **fire-and-forget + status polling**;
there is **no auto-unwind** — a stuck leg finalizes the arb to `error` with `neutral=false` and an
urgent alert, and the signal app decides what to do (this app never auto-closes the surviving leg).
The authoritative spec is `docs/funding-arb-plan.md` (kept current with the build).

**API** (`app/routes/funding_arb.py`, schemas in `app/schemas_arb.py`): `POST /funding-arb/open
{idempotency_key, asset, notional?, size_mode, strategy_tag?, legs?}`, `POST /funding-arb/close {arb_id}`,
`GET /funding-arb/positions[/{arb_id}]` — all behind the `X-Arb-Secret` API-key header. **Auth precedence
(`require_arb_secret`):** `funding_arb_secret == ""` → **503** (an unconfigured arb API must never imply
it works, even with a header); header missing/wrong → **401**; else proceed. `GET /funding-arb` is an
**HTML report** gated EXACTLY like `/performance` (browser-nav, currently OPEN, NOT behind
`X-Arb-Secret`) — keep them in lockstep if `/performance` is ever gated.

- **Default combo** (`legs` omitted) = **single-venue Hyperliquid cash-and-carry**: long HL spot +
  short HL perp, dedicated HL `arb` account, **perp at 1×** (`_default_combo`). `size_mode`: `notional`
  (size each leg from USD; `notional` required >0) | `min` (paper mode — each leg at the exchange
  MINIMUM order size; `notional` ignored). `Asset` is `BTC|ETH|SOL`. Explicit `legs[]` must be exactly
  one `buy` + one `sell` (delta-neutral); other combos (Bybit carry, cross-exchange perp-perp, mixed)
  stay expressible.
- **Open-time symbol exclusivity (409):** reject an open whose leg `(exchange, account, symbol)` is
  already held by a non-closed arb — this is what makes account-wide funding attribution exact (HL
  `get_funding` is account-wide). Idempotency is checked FIRST (a repeat key → `duplicate` even if it
  would otherwise clash).

### Load-bearing isolation invariant (don't break this)

Arb lives in **four dedicated tables** (`app/models.py`, via `create_all`): `ArbPosition` / `ArbLeg` /
`ArbOrder` / `ArbFundingEvent`. The directional reporting (`/performance`, `/orders`,
`_execution_quality`, the `FundingEvent` sums, `_equity_curve`) and `reconcile` are **physically BLIND**
to arb rows — `Order` / `Alert` / `Position` / `StrategyPosition` / `FundingEvent` and
`app/db.py` (`_SQLITE_ADDITIVE_COLUMNS`) are **UNTOUCHED**. This is regression-proven in
`tests/test_arb_isolation.py` (directional fees/funding/equity are byte-equal with vs without arb rows).
`ArbLeg.filled_qty` is the **NET-received base** (the hedgeable quantity neutrality + close measure
against).

### Adapters / registry (`app/exchanges/`)

- Account-keyed: `get_registry().get(name, account="default")` — cache key `(name, account)`;
  `account="default"` keeps every existing directional call site byte-for-byte; `account="arb"` resolves
  the dedicated, separately-credentialed sub-account. `_guard_hyperliquid_account` **RAISES at
  construction** if the HL `arb` address == the directional HL address (HL `close_position` is
  whole-coin and `get_funding` is account-wide, so a shared address would nuke/double-count the
  directional book). `settings.account_credentials(name, account)` resolves the bucket and fails loudly
  on an empty non-`default` bucket.
- **REAL HL spot adapter** (not deferred): `spot_market_order` uses the SDK's `market_open` (an
  aggressive **IOC limit** — HL has no separate market primitive); resolves the Unit pair via `spotMeta`
  and the spot mid keyed by the canonical `@N` name. **Fee denomination drives `filled_qty`: HL spot fee
  is quote-denominated (USDC) → `filled_qty` is GROSS base; Bybit spot fee is base-denominated → NET
  base.** Both venues enforce a $10 min order on HL.

### Executor & workers (the writer boundary)

- `app/arb_executor.py`: fire the **THINNER leg first** (spot before perp), then **hedge the ACTUAL
  fill** — re-derive leg-2's target from leg-1's net fill snapped to leg-2's grid; an un-hedgeable
  fill → `error` + `neutral=false` (no silent residual, no naked hedge if leg-1 didn't fill). **ONE
  `session_scope` per leg** (independent failure domains). It writes ONLY `ArbLeg`/`ArbOrder` — NEVER
  `_LEDGER_LOCK` / `Position` / `StrategyPosition` / `Order` / `_apply_fill_to_position`. Close: perp →
  whole-coin `close_position` (safe — dedicated account); spot → `spot_market_order` sell CLAMPED to the
  live `get_spot_balance`. Reuses only `OrderResult` + `_next_retry_delay` from `executor.py`.
- `app/retry_worker.py` has a **SEPARATE `ArbOrder` scan** (`_run_due_arb_retries`, own
  `session_scope`/limit) → `arb_executor.execute_leg`, which re-resolves the adapter from the order's OWN
  `(exchange, account)` (fail-loud, never the default account), takes no ledger lock.
- `app/funding_worker.py` has a **SEPARATE arb poll** (`poll_arb_once`, same hourly loop, own
  `session_scope`) over non-closed perp legs → `ArbFundingEvent` only (never `FundingEvent`).
- `app/arb_pnl.py` is the **pure** PnL helper (`net = funding_total − commission_total (+ basis)`;
  `directional_net = spot_unrealized + perp_unrealized` ≈ 0 is the neutrality health check). `app/notifier.py`
  adds `arb_opened` / `arb_error` (urgent) / `arb_closed`.

### Contract, tests, go-live

- **Contract:** `docs/openapi-funding-arb.{json,yaml}` is the contract-grade OpenAPI. **Drift is gated by
  a test** (`tests/test_openapi_contract.py` regenerates in a fresh process and asserts it reproduces the
  committed JSON byte-for-byte) — so a schema change that isn't re-dumped fails the suite/CI. Regenerate
  with `make openapi` (`scripts/dump_openapi.py`) and commit.
- **Tests:** `tests/test_arb_*.py` (models, registry, executor, spot adapter, reporting, API, isolation)
  + `test_openapi_contract.py`. Run as the existing suite — `.venv/bin/python -m pytest tests/ -q
  --cov=app --cov-fail-under=75` (same ≥75% gate as the directional side; currently ~85%).
- **Go-live env** (all optional so directional-only deploys still boot): `FUNDING_ARB_SECRET` (`""` →
  arb API 503s), `BYBIT_ARB_API_KEY`/`BYBIT_ARB_API_SECRET` (a Bybit **sub-account**),
  `HYPERLIQUID_ARB_PRIVATE_KEY`/`HYPERLIQUID_ARB_ACCOUNT_ADDRESS` (a **separate** HL account — distinct
  address, own margin). Resolved by `settings.account_credentials(name, account)`.
- **Locked decisions (intentional overrides of `docs/funding-arb-plan.md`):** HL spot is **first-class**
  (the plan deferred it); **single-venue HL carry is the DEFAULT combo**; **`size_mode:"min"` is the
  paper mode**.

## This is a 3-app system

`mochi-carry-backtester` (research / tuning the carry rule) → `mochi-carry-signal` (live decision +
approve-to-fire) → **`mochi-position-manager`** (this repo: delta-neutral execution + reporting). The
integration seam is the **OpenAPI funding-arb contract** (`docs/openapi-funding-arb.yaml`); the signal
rule is shared by porting from the backtester into `mochi-carry-signal`. The signal app calls this app's
`/funding-arb/*` API with `X-Arb-Secret`; its `idempotency_key` is this app's dedup key.
