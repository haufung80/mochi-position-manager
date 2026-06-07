# Funding-Arbitrage Execution — Implementation Spec

> Implementation plan for adding funding-arbitrage execution to `mochi-position-manager`.
> Hand this doc to Claude Code as the spec. Build in the phase order below; **Phase 0 (swagger) is a
> review gate — stop for sign-off before writing executor/model/adapter code.** Keep `main` green
> (`pytest --cov=app --cov-fail-under=75`). See `CLAUDE.md` for repo conventions.

---

## Context

This app is today an **execution layer for directional perp strategies**: a TradingView webhook fires,
the app sizes/forwards a **perp market order** (Bybit `linear` or Hyperliquid perp), drives the `Order`
row through `pending → success | retrying | dead` (`executor.py`), mutates a per-strategy ledger
(`StrategyPosition`) + an aggregate ledger (`Position`), and reports PnL on `/performance`. A separate
`mochi-funding-signal` app will *decide* funding-arb trades and call **this** app's API to **execute and
manage** them.

The feature turns this app into the **execution + position manager** for delta-neutral funding
arbitrage. The signal app POSTs "open this arb, $X/leg" and "close arb 42", then polls a status
endpoint; this app fires both legs, retries failures, tracks the pair as one unit, and reports
funding-minus-fees PnL. It is fire-and-forget (no in-app rate poller; the signal app decides).

This spec satisfies four hard requirements: **(1) Hyperliquid trades too — first-class, not
deferred**; **(2) swagger-first — a reviewable OpenAPI artifact before any code**; **(3) all necessary
tests in CI** (Hypothesis property + behavioural/adverse + unit + API), ≥75% gate, no regression;
**(4) no tech debt** — every prior compromise replaced with the genuinely clean design.

### Grounding facts (verified against live code — they drive the decisions)

- `dashboard._execution_quality` (dashboard.py:200-216) queries `Order` where `status=="success"` with
  **no Alert join** → any arb row in `orders` would inflate the directional fees/slippage panel. *This
  is the decisive fact against reusing the `orders` table.*
- `_performance` funding sum (dashboard.py:383-387) and `_equity_curve` (dashboard.py:438) sum
  `FundingEvent` with **no exchange filter** → arb funding in that table would bleed into the directional
  headline + equity curve.
- `/orders` (dashboard.py:566-590) serializes the `Order` table directly (incl. `alert_id`), no join.
- `FundingEvent.exchange` is consumed as a real venue by `reconcile._oracle_symbol`→`symbols.symbol_for`
  and `registry.get` → a `"bybit:arb"` string would `KeyError`/mis-map. *Rules out an overloaded label.*
- HL `close_position`→`market_close(symbol)` closes the **entire coin** position on that account
  (hyperliquid.py:181); HL `get_funding` is **account-wide** `user_funding_history` filtered by coin
  (hyperliquid.py:244) → the arb on HL **must** use a distinct HL account, else it nukes/double-counts
  the directional HL book.
- `HyperliquidExchange` already implements the full `Exchange` Protocol and is constructed from
  `private_key` + `account_address` (hyperliquid.py:18-52); reads key off `self._account_address`. **A
  second HL account = just a second key+address — no new mechanism.**
- `retry_worker._run_due_retries` (retry_worker.py:36-45) rebuilds `VenueRoute` from
  `order.exchange`+`order.symbol` only — no account, no product.
- `registry.get(self, name)` and `conftest.FakeRegistry.get(self, name)` take only `name`; `FakeExchange`
  has no spot surface and is one shared instance.
- `Order.alert_id` is `NOT NULL` (models.py:44); SQLite cannot `ALTER ... DROP NOT NULL` and `db.py`'s
  migration loop (db.py:46-71) is additive-only. *Rules out nullable `alert_id`.*
- Deps already present: `pyyaml`, `hypothesis`, `pytest-asyncio`, `fastapi`. `alembic` is declared but
  unused. No `docs/`, `Makefile`, `pyproject.toml`, `pytest.ini`, or `openapi*` exist yet.

## Confirmed decisions

1. **This app owns the pairing** — an arb is a **multi-leg position**; one API call opens/closes it;
   legs are tracked + reported as a unit.
2. **Hyperliquid is first-class and buildable now** (the combos below, including the DEFAULT
   single-venue HL cash-and-carry). HL perp is reused as-is; HL spot is a real adapter (override #4).
3. **Each venue's arb book is a dedicated, separately-credentialed account** — a Bybit **sub-account**
   (own API keys) and a **separate HL account** (own private key + address). *Not* a vault and *not* a
   `vault_address` subaccount overload (the SDK's `vault_address` targets a vault, a different product —
   unproven for personal subaccounts, so we avoid it entirely). Separate-account isolation is already
   implemented, zero new mechanism, and is the zero-tech-debt choice. The arb never nets against
   directional positions, and HL whole-coin close/account-wide funding become safe by construction.
4. **HL spot is FIRST-CLASS** (APPROVED OVERRIDE — reverses the earlier "deferred / spot leg always
   Bybit" decision). A **real Hyperliquid spot adapter** is built now: its spot Protocol methods place
   genuine HL Unit-spot orders (resolve the spot pair via `spotMeta` — Unit tokens are `'U'+base`, pairs
   named like `'@142'`; size from spot `szDecimals`; min-notional from the HL $10 floor). There is no
   `{exchange:hyperliquid, product:spot}` → 422 rejection — that leg is VALID in the contract and
   executable. **The new DEFAULT combo (legs omitted) is single-venue Hyperliquid cash-and-carry**: long
   HL spot + short HL perp on the same dedicated HL `arb` account, perp at 1×. The Bybit cash-and-carry,
   cross-exchange perp-perp, and Bybit-spot + HL-perp combos stay expressible via explicit `legs[]`.
5. **Separate auth secret** `FUNDING_ARB_SECRET` as an **`X-Arb-Secret` API-key header** (so it shows in
   the OpenAPI security scheme; the signal app is our code and can send headers, unlike TradingView).
6. **Fire-and-forget** open/close via `BackgroundTasks` (threadpool, like `_run_fan_out`) + a status
   endpoint the signal app polls.
7. **Market orders only**; **perp legs at 1× leverage**; run the Bybit arb sub-account in
   cross/portfolio margin so `$X` funds an `$X` pair.
8. **No auto-unwind**: a stuck leg → arb `status=error`, `neutral=false` + per-leg status/skew exposed +
   urgent notifier; the **signal app** decides whether to `/close`.
9. **Separate `/funding-arb` reporting page** (the PnL model differs: cost-basis spot, funding-is-the-
   point, per-pair + per-venue netting).

### Resolved design conflicts (one choice each)

- **Order linkage → a dedicated `ArbOrder` table** (not `orders` reuse, not the `alert_id=0` sentinel,
  not nullable `alert_id`). Reusing `orders` leaks arb rows into `_execution_quality` + `/orders` (no
  Alert join); nullable `alert_id` is impossible additively in SQLite. A separate table makes directional
  isolation **structural** and removes the sentinel/orphan-guard. It reuses `executor._next_retry_delay`
  + the `retrying→dead` ladder by import (a thin query, not the backoff machinery). `Order`/`Alert`
  untouched.
- **Arb funding → a dedicated `arb_funding_events` table** (not an overloaded `FundingEvent.exchange`).
  Keeps that column meaning one thing and keeps arb funding out of the directional funding/equity queries
  by construction.
- **Swagger gate → drift-gate ONLY `docs/openapi-funding-arb.*`** (the contract-grade half). The
  existing-API spec is generated as a **non-gated convenience artifact** and documented as untyped, so
  edits to the ~10 legacy raw-dict routes never red CI.

## Scope and phasing

**Buildable now (Phase A)** — all combos (the default HL cash-and-carry + the explicit ones), each a list of explicit leg tuples
`(exchange, account, product, side, target_qty)`. The signal app picks the combo via `legs[]`; the open
endpoint validates the legs form a delta-neutral pair (exactly one long + one short, equal target base
qty):

| Combo | Leg 1 | Leg 2 |
|---|---|---|
| **0. Single-venue Hyperliquid cash-and-carry** (DEFAULT) | `(hyperliquid, arb, spot, buy, q)` | `(hyperliquid, arb, perp, sell, q)` |
| **1. Bybit cash-and-carry** | `(bybit, arb, spot, buy, q)` | `(bybit, arb, perp, sell, q)` |
| **2. Cross-exchange perp-perp** | `(hyperliquid, arb, perp, buy, q)` | `(bybit, arb, perp, sell, q)` |
| **3. Cross-exchange spot+perp** | `(bybit, arb, spot, buy, q)` | `(hyperliquid, arb, perp, sell, q)` |

`legs` omitted ⇒ DEFAULT combo 0 (single-venue Hyperliquid cash-and-carry). Combo 0 and combo 3 use the
real HL spot adapter.

**Sizing (`size_mode`).** `size_mode: "notional"` (default) sizes each leg from `notional` (USD, `>0`
required). `size_mode: "min"` is a **paper mode**: ignore `notional` and size each leg to the exchange's
MINIMUM order size (real but tiny orders) — so `notional` becomes optional. Both modes still produce a
delta-neutral equal-base-qty pair.

**Future (additive, no migration):** N>2 legs, more venues, HL spot — identity lives on the leg, and the
leg schema already carries `exchange + account + product`.

## Phase 0 — Swagger/OpenAPI deliverable (review gate; no behaviour change)

Produces a reviewable artifact **before** any executor/model/adapter code, then **stops for sign-off**.
The arb request/response models are designed first (this also locks the leg shape, idempotency, auth, and
status contract before any code depends on them).

1. **`app/schemas_arb.py`** — every `/funding-arb/*` request + response model (Design §3). HL/cross-
   exchange are first-class in the contract (`LegSpec.exchange: Literal["bybit","hyperliquid"]`,
   `product: Literal["spot","perp"]`); `{hyperliquid, spot}` is a VALID leg (no 422); `asset` is `Literal` over supported
   assets; `notional = Field(gt=0)`.
2. **`app/routes/funding_arb.py`** — router with `response_model=` on every route + the `X-Arb-Secret`
   `APIKeyHeader` dependency (so the spec emits `securitySchemes.ArbSecret` + per-route `security`). In
   Phase 0 handlers are thin typed stubs purely to emit a contract-grade spec; real bodies land in A.5.
   Included in `main.py`.
3. **`scripts/dump_openapi.py`** — imports the app under `DRY_RUN=1` + a dummy `FUNDING_ARB_SECRET`
   (never touches exchanges), **sets `app.openapi_schema=None`** (FastAPI memoizes — clearing is required
   or the dump is stale), calls `app.openapi()`, writes the files. The **existing** spec is dumped as-is
   with a header note: "legacy response bodies are untyped raw dicts; out of scope." The **funding-arb**
   spec is contract-grade.
4. **`Makefile`** — `openapi:` target (`.venv/bin/python scripts/dump_openapi.py`).
5. Commit `docs/openapi-existing.{json,yaml}` (convenience) + `docs/openapi-funding-arb.{json,yaml}`
   (gated). **STOP for review:** user reads `docs/openapi-funding-arb.yaml` and approves the contract
   (leg shape, the default HL cash-and-carry combo + the explicit combos, HL-spot ACCEPTED,
   `size_mode`, `pnl{}`/`funding_by_leg`, `X-Arb-Secret`) before A.1+.

## Design

### 1. Multi-leg data model (zero tech debt)

Four new tables in `app/models.py`, all created by `create_all` — **no `_SQLITE_ADDITIVE_COLUMNS` edits,
no change to `Order`/`Alert`/`Position`/`StrategyPosition`/`FundingEvent`**:

- **`ArbPosition`**: `id, idempotency_key (UNIQUE), asset, strategy_tag, notional_target, status
  (opening|open|closing|closed|error), realized_pnl, opened_at, closed_at, error_message, timestamps`.
  `UNIQUE(idempotency_key)` is the dedup gate (mirrors `Alert`).
- **`ArbLeg`**: `id, arb_id, exchange, account, product, symbol, side, target_qty, filled_qty, avg_fill,
  commission, commission_asset, funding, status, error_message, timestamps`. **Identity on the leg** —
  fully self-describing `(exchange, account, product, symbol, side, target_qty)`, which is what lets one
  schema express every combo and any future N-leg arb. `UNIQUE(arb_id, exchange, product, symbol)`
  is the leg-level idempotency guard (a re-entered open task can't create a second leg set).
  **`filled_qty` stores the NET-received base quantity** — for a Bybit spot BUY whose fee is charged in
  the base coin, `filled_qty = ordered_base − base_fee` (the actually-held, hedgeable quantity). This is
  the quantity neutrality and close are measured against.
- **`ArbOrder`**: mirrors `Order`'s execution fields + **`arb_leg_id` (NOT NULL, indexed)** + first-class
  **`account`** and **`product`** columns (so a retry re-resolves the exact `(exchange, account)` adapter
  and `BTCUSDT` spot vs linear never collide). Structurally invisible to every directional query.
- **`ArbFundingEvent`**: `id, arb_id (nullable), exchange, account, symbol, funding_time, amount,
  created_at`, `UNIQUE(exchange, account, symbol, funding_time)`. Keeps `FundingEvent` pure.

**Open-time symbol exclusivity:** the open endpoint **rejects** a new arb whose leg `(exchange, account,
symbol)` is already held by a non-closed `ArbPosition`. This keeps the account-wide-funding attribution
unambiguous (HL `get_funding` is account-wide; two concurrent BTC arbs on the one HL arb account would
otherwise double-count the same settlement). Documented next to the "no migration" claim; tested.

### 2. Spot + multi-account adapters (Bybit AND Hyperliquid)

**Protocol (`base.py`)** — add a total spot surface (implemented by BOTH venues): a missing
implementation is a clear error, not an `AttributeError`: `spot_market_order(symbol, side, qty)`,
`get_spot_balance(base_asset)`,
`get_spot_step_size(symbol)`, `get_spot_min_notional(symbol)`. Spot symbol mapping goes in
**`symbols.py`** (its documented role): `spot_symbol_for(exchange, base)`.

**Bybit spot (`bybit.py`)** — `SPOT_CATEGORY="spot"` + the five methods (do not overload the `linear`
methods; `CATEGORY="linear"` is hard-coded in ~12 places). Correctness specifics:
- **Separate spot instrument cache keyed by `(category, symbol)`** — `BTCUSDT` exists in both spot and
  linear; the current cache keys by symbol only and would return wrong filters.
- Spot BUY passes `marketUnit="baseCoin"` (qty in base units) and snaps to `lotSizeFilter.basePrecision`
  /`minOrderQty` (a spot rounder, not the linear `_round_qty`).
- `get_spot_min_notional` reads the spot filter's `minOrderAmt` (quote-side min).
- `get_spot_balance` returns the **free/available** base balance (not `walletBalance`).
- Spot BUY fee is base-denominated → surface the true `commission_asset` in `OrderResult` and record
  `ArbLeg.filled_qty` net of it (see §1). Parameterize `_fill_details(category=…)`.

**Hyperliquid (`hyperliquid.py`)** — perp reused unchanged (1× via the existing `update_leverage` call;
the executor passing `leverage=1.0` suffices). **Spot is a REAL, first-class adapter** (APPROVED
OVERRIDE — not a `NotImplementedError` stub). All four spot methods are implemented for HL Unit spot:
- Resolve the spot pair via `info.spot_meta()` — the base token is `'U'+base` (e.g. `UBTC`/`UETH`/`USOL`),
  the USDC quote pair has a canonical `name` like `'@142'`; that `name` is the order coin. The SDK's
  `name_to_coin`/`coin_to_asset` route spot vs perp automatically (spot asset ids start at 10000), so the
  same `market_open` path is reused. The order is an **aggressive IOC limit** (HL has no true "market"
  primitive — `market_open` itself is an IOC limit at the slippage-adjusted mid; documented in the code).
- Step size from the base token's spot `szDecimals`; min-notional from the HL $10 floor.
- `get_spot_balance` reads `info.spot_user_state(address)["balances"]` → free = `total − hold` for the
  base coin. HL spot fees are quote-denominated (USDC), so `filled_qty` is the gross filled base; the
  true `commission_asset` is still surfaced on the `OrderResult`.
- `DRY_RUN` (or no private key) short-circuits to a simulated net-base `OrderResult` with **no network**,
  mirroring the perp path.

**Multi-account registry (`registry.py`)** — `get(name, account="default")`, cache key `(name, account)`,
credentials resolved via a `settings.account_credentials(name, account)` helper (no literal-`"arb"`
branch — that wouldn't generalize). `account="default"` keeps **every existing call site byte-for-byte**
(executor.py:209, reconcile.py:73/135/145/193/233, portfolio.py:107, dashboard.py:338,
funding_worker.py:45). **Construction-time isolation guard:** when building the HL `arb` adapter, assert
its resolved `account_address != ` the directional HL address and raise a clear config error — this
protects **both** the executor (close/open) and the funding poller with one check, at the only place that
matters (before any order is sent). Unknown/empty-cred `(name, account)` raises clearly.

**Config (`config.py` + `.env.example`)** — add `bybit_arb_api_key`, `bybit_arb_api_secret`,
`hyperliquid_arb_private_key`, `hyperliquid_arb_account_address`, and `funding_arb_secret`. Make
`funding_arb_secret` **optional (`""`)** so directional-only deploys still boot (preserves "boot with
partial setup"); the arb router 503s when unset. Reuse the single `dry_run`/`*_testnet` switches.

### 3. API surface (`app/schemas_arb.py`)

Auth: `APIKeyHeader(name="X-Arb-Secret", auto_error=False)` validated in a `Depends`. **Precedence
(explicit):** if `funding_arb_secret == ""` → **503** regardless of header (an unconfigured arb API must
not imply it works); else if header `!=` secret (incl. missing/`None`) → **401**; else proceed. Writes
are fire-and-forget via `BackgroundTasks`; reads set `Cache-Control: no-store`; `not_found`→404,
`already_closed`→409 (`HTTPException` + `responses{}` in the spec, plus a `status` field for parity).

Models: `LegSpec{exchange,product,side}` (`{hyperliquid,spot}` is VALID — no rejection); `ArbOpenRequest
{idempotency_key, asset:Literal, notional:gt0, strategy_tag?, legs?:list[LegSpec]}` (omit `legs` ⇒ combo
1; `Field(examples=[…])` shows a default + a cross-exchange example); `ArbOpenResponse{status:
accepted|duplicate, arb_id, idempotency_key, legs:list[SizedLeg]}`; `ArbCloseRequest{arb_id}` /
`ArbCloseResponse{status, arb_id}`; `ArbLegView{…, funding:Optional, status}`; `ArbPnL{funding_total,
funding_by_leg:dict, commission_total, spot_unrealized, perp_unrealized, directional_net, net}`;
`ArbPositionView{arb_id, asset, status, neutral, neutrality_skew, legs, pnl, opened_at, closed_at,
error_message}`.

Endpoints (`routes/funding_arb.py`, included in `main.py`):
- **`POST /funding-arb/open`** (`response_model=ArbOpenResponse`): authorise → resolve combo → reject if
  a leg symbol is already open in the arb book (§1) → **size the pair** (§4) → persist `ArbPosition` +
  `ArbLeg`s in one short txn (dedup via `IntegrityError`) → return → schedule `_run_open`.
- **`POST /funding-arb/close`** (`ArbCloseResponse`, 404/409): set `status=closing`, schedule `_run_close`.
- **`GET /funding-arb/positions[/{id}]`** (`list[ArbPositionView]` / `ArbPositionView`, 404).
- **`GET /funding-arb`** → dark-theme HTML reporting page.

### 4. Arb executor + retry (`app/arb_executor.py`)

A sibling to `executor.py` (which is hard-wired to `Alert`+`VenueRoute`). Reuses only low-level pieces
(`OrderResult`, `_next_retry_delay`, the `retrying→dead` ladder) and writes `ArbLeg`/`ArbOrder` only —
**never `_apply_fill_to_position`, never `_LEDGER_LOCK`** (it touches no `Position`/`StrategyPosition`).

**Pair sizing (initial):** legs price off different venues (HL mid vs Bybit mark) and snap to different
grids (HL `szDecimals` vs Bybit `qtyStep`/`basePrecision`). So: (1) compute one target base qty from
`notional` + a reference price; (2) clamp to the **coarser** of the two grids (`max(stepA, stepB)`) so
both legs can hold the **same** quantity; (3) re-check each leg's min-notional at **its own** venue and
**reject** (don't silently shrink one leg) if either fails. Both legs get the **same** `target_qty`.
`compute_managed_qty` is reused as the notional→base helper.

**`_run_open(arb_id)`** (a plain sync function, run in the BackgroundTasks threadpool like `_run_fan_out`
— so it is unit-tested directly, no async test needed):
- Fire the **thinner-liquidity leg first** (e.g. HL perp before Bybit perp; Bybit spot before Bybit perp)
  to minimize the naked window; rationale documented.
- **Hedge the actual fill:** after leg-1 fills `f1` (net of base fee for spot), **re-derive leg-2's
  target from `f1`** snapped to leg-2's grid (true delta hedge of what filled, not the original target).
  If `f1` can't be hedged within one step at leg-2's venue (min-notional/grid), mark `error` +
  `neutral=false` (no silent residual). This is the explicit partial-fill policy.
- **One `session_scope()` per leg** (like `_run_fan_out`) so one leg's failure can't roll back the
  other's recorded fill; then `_finalize_open_status` sets `open` or `error`.
- `neutrality_skew = long_leg.filled − short_leg.filled` (base, net of fees) is continuously visible.
- `execute_leg(db, leg, existing_order=None)`: resolve `get_registry().get(leg.exchange, leg.account)`;
  create/reuse an `ArbOrder` (side/qty frozen, `account`/`product` persisted); call `spot_market_order`
  (spot) or `market_order(…, leverage=1.0)` (perp); on success record net `filled_qty`/`avg_fill`/
  `commission`; on failure walk the ladder.

**`_run_close(arb_id)`:** perp → `close_position(symbol)` (whole-coin close is safe — the arb leg owns a
dedicated account); spot → `spot_market_order(sell, qty=filled)` **clamped to live `get_spot_balance`**.

**Retry (`retry_worker.py`):** add a **separate `ArbOrder` scan in its own `session_scope`** (own
`.limit`) after the existing `Order` scan, dispatching to `arb_executor.execute_leg`. This (a) leaves the
existing `Order`/`Alert` orphan logic untouched (no sentinel guard), (b) re-resolves the adapter from the
`ArbOrder`'s own `exchange`+`account` columns (an arb retry can never fall back to the default account —
it fails loud), and (c) does **not** take `_LEDGER_LOCK`, so it can't lengthen the directional ledger
critical section. *(If arb retry volume ever competes with directional retries, split into its own
worker — noted, not built.)*

### 5. Funding attribution across venues (`funding_worker.py` + `ArbFundingEvent`)

A dedicated arb poll iterates **arb perp legs** (`ArbLeg where product=='perp'`) across **both** exchanges
(`get("bybit","arb")`, `get("hyperliquid","arb")`) and writes `ArbFundingEvent` keyed by
`(exchange, account, symbol, funding_time)` — independent of the directional `_venue_pairs` poll (the arb
book has no `strategies.yaml` entry). The directional `funding_events` + `_performance`/`_equity_curve`
are untouched and **cannot** see arb funding (separate table).

**Per-arb funding** = Σ over perp legs of Σ `ArbFundingEvent.amount` for `(leg.exchange, leg.account,
leg.symbol)` within `[opened_at, closed_at|now]`. Spot legs contribute 0. Combo 2 sums both perp legs
(long-HL + short-Bybit fundings net to the harvested carry); combos 1/3 are the single short perp.
`funding_by_leg` surfaces the per-venue split. Sign: `amount` is +received/−paid. The open-time symbol
exclusivity (§1) keeps the account-wide settlement attributable to exactly one arb. The HL same-account
catastrophe is prevented at **construction** (§2), not here — the poller keeps a defense-in-depth check.

### 6. Reporting page (`templates/funding_arb.html` + `_arb_performance`)

Reuses `performance.html`'s dark-theme CSS; one row per `ArbPosition` with nested legs. Headline =
**funding harvested − fees**; per-leg `funding` (spot shows 0), `spot_unrealized` (cost-basis),
`perp_unrealized` (venue `get_position_detail`), `directional_net` (≈0 health check), `neutrality_skew`.
Reciprocal nav links with `dashboard.html`. Structurally unaffected `/performance` (regression-tested).

## Phased delivery (each phase green on the global 75% gate)

Coverage is **global across `app/`** — so each phase must cover enough of its own new lines to keep the
whole ≥75% (the real HL spot adapter and the reporting helper are explicitly hit by tests so they don't
drag it). Estimated per-phase: each ships ≥ its own new-line coverage; net non-negative.

- **Phase 0 — Swagger (review gate):** `schemas_arb.py`, stubbed typed `routes/funding_arb.py`,
  `scripts/dump_openapi.py`, `Makefile`, committed `docs/openapi-*`. Tests: `test_openapi_contract.py`
  (all `/funding-arb/*` paths present + request/response schemas; `ArbSecret` on writes; `app.openapi()`
  succeeds; `{hyperliquid,spot}` ACCEPTED) + a **fresh-import staleness test** (subprocess /
  `importlib.reload`, cache cleared) asserting the generated funding-arb schema equals the committed file.
  **STOP for user approval.**
- **A.1 Config + multi-account registry:** `config.py`, `registry.py`, `.env.example`. Tests:
  `get("bybit")==get("bybit","default")`; `get("bybit","arb")` and `get("hyperliquid","arb")` distinct;
  **HL arb address == directional address → construction raises**; unknown account raises;
  `reset_registry()` works.
- **A.2 Spot adapters + Protocol (Bybit AND real HL spot):** `base.py`, `bybit.py` (spot, separate
  instrument cache, `minOrderAmt`, free balance, base-fee net), `hyperliquid.py` (REAL spot adapter via
  `spotMeta`), `symbols.py`. Rework `conftest`: `FakeRegistry.get(name, account="default")` returns a
  **distinct `FakeExchange` per `(name,account)`**; spot methods + `spot_balances` + a base-fee knob; the
  HL fake's spot WORKS (net-base `OrderResult`). Tests: DRY_RUN spot order returns net-base `OrderResult`
  on BOTH venues; balance read; Bybit base-fee net; spot rounding/`minOrderAmt`; HL spot
  symbol-resolution + sizing.
- **A.3 Models:** the four tables via `create_all`. Tests: tables created; `idempotency_key` UNIQUE;
  `ArbLeg UNIQUE(arb_id,exchange,product,symbol)`; `Order` schema **unchanged** (regression).
- **A.4 Arb executor + retry scan:** `arb_executor.py`, `retry_worker.py` (separate `ArbOrder` scan).
  Tests: spot+perp legs record fills and **create no `StrategyPosition`/`Position`/`Order` rows**
  (isolation); initial sizing yields **equal ordered base qty** across two grids; partial-fill re-hedge
  (leg-1 fills 0.4/0.5 → leg-2 target re-derived to 0.4); failure→`retrying`→`dead`; **arb retry runs
  WITHOUT acquiring `_LEDGER_LOCK` and without writing Position/StrategyPosition, on the correct
  account**; **shared-writer smoke** (a due `ArbOrder` + a due `Order` in one pass both replay, neither
  corrupts the other).
- **A.5 API router (real bodies) + main wiring:** flesh out `routes/funding_arb.py`. Tests (TestClient):
  **secret unset → 503** (any/no header); secret set + missing/bad header → 401; correct → 200; open →
  `accepted` + sized legs; duplicate key → `duplicate` (one `ArbPosition`); symbol already open →
  rejected; status shows legs `success`, `neutral:true`; close fires correct per-leg calls; unknown id →
  404; already closed → 409; `{hyperliquid,spot}` ACCEPTED (200, real HL spot leg); **shared HL address + HL leg → open refused
  before any order reaches the fake**.
- **A.6 Funding attribution + reporting:** `funding_worker.py` (arb poll → `ArbFundingEvent`),
  `_arb_performance`, `funding_arb.html`, dashboard link. Tests: per-arb funding for combos 1/2/3
  (spot=0); **two concurrent BTC arbs can't double-count** (exclusivity); page renders 200 `no-store`.

## Test matrix (property + behavioural + unit + API; CI-wired; ≥75%)

**Infra:** a new `pyproject.toml`/`pytest.ini` with a `[tool.pytest.ini_options]` block registering a
Hypothesis **`ci` profile** (`max_examples=200, deadline=None`) loaded when `CI` is set (determinism on
the runner) + `property`/`behavioural`/`arb` markers. **Do NOT set `asyncio_mode=auto`** — the arb
executor/poller are tested through their **synchronous** entrypoints exactly as the existing workers are
(`test_funding.py` calls `poll_once`; `test_webhook.py` calls `_run_due_retries`), so no async-collection
mode is needed (and flipping it would change collection for the whole suite).

- **Property (`test_arb_pnl_properties.py`)** (bounded float strategies reused from
  `test_pnl_properties.py`): (1) **initial sizing** yields equal **ordered** base qty across any two
  `(price, step)` grids, `|ordered skew| == 0` after coarse-clamp *(stated as ordered, not filled —
  filled neutrality is a behavioural test because market orders can partial-fill)*; (2) arb net identity
  `net == funding_total − commission_total` within tol; (3) spot cost-basis identity
  (`realized − net*avg == cash_flow`); (4) close-drives-flat: clamped spot sell never exceeds
  `get_spot_balance` and both legs end flat.
- **Behavioural / adverse (`test_arb_cross_exchange.py`, `test_arb_isolation.py`):**
  - Combos 2 & 3 resolve to **distinct** `(exchange,account)` fakes, opposite sides, equal base qty.
  - **Partial-fill skew + re-hedge:** leg-1 fills < target → leg-2 re-derived to leg-1's net fill;
    if unhedgeable, `status=error` + `neutral=false` (never silently `open`).
  - **Spot base-fee neutrality:** with a non-zero base-coin commission, `|spot_net_filled −
    perp_filled| ≤ one step` (not biased by the fee) and `_run_close` flattens **both** legs.
  - **Two-account leg-risk:** leg-1 fills, leg-2 fails → `error`, `neutral=false`, only the failed leg
    retries (on the correct account), filled leg untouched.
  - **HL shared-address refusal:** arb HL address == directional → registry construction raises and
    `/open` with an HL leg is refused before any `close_position`/`market_open` reaches the fake.
  - **Account isolation:** arb fills create no `StrategyPosition`/`Position`/`Order`; closing the arb HL
    leg doesn't touch a directional HL position on the same coin/different account.
  - **Concurrent-arb funding:** two BTC arbs can't both claim one account-wide settlement (exclusivity).
  - **Regression isolation (load-bearing) — under a deterministic multi-account stub** so
    `get_position_detail` is identical across runs: assert `_execution_quality` fees/slippage are equal
    with vs without arb rows (the real bleed vector); assert `Σ FundingEvent` (and `_equity_curve`
    points) are equal with arb `ArbFundingEvent` rows present (proves the directional funding/equity
    queries are blind to arb); assert `reconcile.audit_pnl` does not read `arb_*` tables (structural).
- **Unit:** registry account keying (both venues) + HL same-address raise; Bybit spot rounding /
  `minOrderAmt` / free-balance / base-fee net; HL spot symbol-resolution + sizing + DRY_RUN net-base
  result; pair-sizing reject-on-min-notional; auth 503/401/200 precedence (empty-vs-None guarded).
- **API (`test_arb_api.py`):** the A.5 cases.
- **OpenAPI (`test_openapi_contract.py`):** schema presence + the fresh-import staleness check.

**CI (`.github/workflows/ci-cd.yml`, existing test job):** after install, `python scripts/dump_openapi.py
&& git diff --exit-code docs/openapi-funding-arb.*` (gate **only** the contract-grade file; message:
"run make openapi and commit"). The existing convenience spec is not gated. All pytest suites ride the
existing `pytest tests/ -q --cov=app --cov-fail-under=75`.

## Critical files

**New:** `app/schemas_arb.py`, `app/routes/funding_arb.py`, `app/arb_executor.py`,
`app/templates/funding_arb.html`, `scripts/dump_openapi.py`, `Makefile`, `pyproject.toml` (Hypothesis
profile + markers only), `docs/openapi-existing.{json,yaml}`, `docs/openapi-funding-arb.{json,yaml}`, and
tests: `test_openapi_contract.py`, `test_arb_registry.py`, `test_arb_spot_adapter.py`,
`test_arb_models.py`, `test_arb_executor.py`, `test_arb_cross_exchange.py`, `test_arb_isolation.py`,
`test_arb_api.py`, `test_arb_pnl_properties.py`, `test_arb_reporting.py`.

**Changed:** `app/models.py` (4 tables; `Order` untouched), `app/config.py` (arb creds + optional
`funding_arb_secret` + `account_credentials`), `app/exchanges/registry.py` (account-keyed `get` + HL
same-address guard), `app/exchanges/base.py` (spot Protocol), `app/exchanges/bybit.py` (spot methods,
separate instrument cache, `minOrderAmt`, free balance, base-fee net, `_fill_details(category=)`),
`app/exchanges/hyperliquid.py` (REAL spot adapter via `spotMeta`; separate-account docstring),
`app/exchanges/symbols.py` (`spot_symbol_for`), `app/retry_worker.py` (separate `ArbOrder` scan),
`app/funding_worker.py` (arb poll → `ArbFundingEvent`), `app/routes/dashboard.py` (nav link only),
`app/main.py` (include arb router), `tests/conftest.py` (per-`(name,account)` fakes + spot + base-fee +
Hypothesis profile), `.github/workflows/ci-cd.yml` (OpenAPI drift step), `.env.example`, `README.md`.
**`app/db.py` unchanged.**

## Risks & mitigations

1. **Leg-risk / partial fill across two failure domains (highest):** fire the thinner leg first; re-hedge
   leg-2 to leg-1's actual net fill; per-leg retry; on hard failure `error` + `neutral=false` + urgent
   notifier; no auto-unwind. Tested.
2. **HL whole-coin close / account-wide funding:** the arb HL book is a **separate account**; a
   shared-address config **raises at registry construction** (before any order). Tested.
3. **Delta-neutrality across two grids + market slippage:** equal *ordered* qty (coarse-clamp, property-
   tested) + re-hedge to actual fills + base-fee-net filled qty (behavioural-tested). Skew always exposed.
4. **Hidden bleed into directional dashboard/reconcile:** dedicated `arb_orders` + `arb_funding_events`
   tables make `_execution_quality`, `_performance`, `_equity_curve`, `/orders`, `reconcile` physically
   blind to arb rows; the regression test pins `_execution_quality` + funding sums under a stub.
5. **Spot semantics:** Bybit — separate spot instrument cache, `minOrderAmt`, free balance, base-fee net;
   Hyperliquid — `spotMeta` pair resolution (Unit `'U'+base` → `'@NNN'`), spot `szDecimals`, IOC market.
6. **OpenAPI drift:** generator clears the memoized schema; CI gates only the contract-grade file; a
   fresh-import test catches staleness; the untyped legacy spec is non-gated.
7. **Coverage:** global ≥75%; each phase covers its own new lines (incl. the real HL spot adapter).

## Verification

1. **Phase 0 gate:** `make openapi` → `docs/openapi-funding-arb.yaml` reviewed + approved (leg shape,
   the default HL cash-and-carry combo + the explicit combos, HL-spot ACCEPTED, `size_mode`,
   `pnl{}`/`funding_by_leg`, `X-Arb-Secret`) **before** A.1.
2. `.venv/bin/python -m pytest tests/ -q --cov=app --cov-fail-under=75` → all green, gate met.
3. CI: `python scripts/dump_openapi.py && git diff --exit-code docs/openapi-funding-arb.*` passes.
4. Local `uvicorn` (DRY_RUN): DEFAULT combo `{asset:BTC,notional:1000}` (legs omitted) → `accepted` with
   the HL spot+perp pair; combos 1/2/3 via `legs[]` → two `success` legs, `neutral:true`; `/positions`
   shows per-leg funding (spot=0) + skew; `/close` → correct per-leg closes; secret unset → 503; bad header → 401; dup key →
   `duplicate`; `{hyperliquid,spot}` → 200 (real HL spot leg, the default combo).
5. `GET /funding-arb` renders; `GET /performance` totals + equity + execution-quality panel **unchanged**
   (proven by `test_arb_isolation.py`).
6. Ship via CI → PR (tests + drift gate) → merge to main (auto-deploys). **Before live:** create the
   Bybit arb sub-account (own API keys) + a **separate HL account** (own key+address — note it has its
   own margin, not shared with the directional HL account); set `BYBIT_ARB_*`, `HYPERLIQUID_ARB_*`,
   `FUNDING_ARB_SECRET` in `~/mochi/.env`; set the Bybit sub-account to cross/portfolio margin; smoke-test
   one small pair per combo, confirm both legs + neutrality + per-venue funding on `/funding-arb`, close.
