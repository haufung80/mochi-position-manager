# Session Handoff — Mochi Position Manager

> Pooled digest of two prior Claude Code sessions, written 2026-06-02. Read this first, then
> delete or archive it once you're caught up. It is a summary, not a spec — trust the code over
> this doc where they disagree.

## What the project is
FastAPI middleware: TradingView webhooks → Bybit + Hyperliquid. Persists every signal to SQLite
first, dedupes on `alerts.idempotency_key`, retries failed orders with exponential backoff,
Telegram-alerts on events. Fires **market orders only**, tracks signal-derived *intent* (not live
exchange state). See `CLAUDE.md` for the architecture of record.

---

## Session A — "TradingView webhook position manager" (the big build, 114 turns)
Source: `local_5daa7180…` / cli `e1c144d4-8213-4b28-8f6f-f1cd6e3d7b3b`, cwd `algo-trade-backtesting`.
This session built essentially the whole app and deployed it. Chronological highlights:

**Build**
- Core middleware: dedup, retry worker, executor, per-venue fan-out, two ledgers
  (`Position` + `StrategyPosition`), reconcile + "sync to exchange" admin action.
- Admin UI to CRUD strategies (authorized by `WEBHOOK_SECRET` as a form field).
- Symbol dropdown BTC/ETH/SOL/BNB with venue mapping; per-signal quantity comes from the
  TradingView payload (`{{strategy.order.contracts}}`), NOT YAML.
- Telegram bot wired (`@mochi_position_manager_bot`); resolved the 403 "can't send to bot" issue.
- Default leverage was found to be 1x; user wants **2x** (verify this is actually applied).
- Per-strategy net $ / coin-unit tracking on the dashboard; fixed `-0$` display + dimming of flat
  positions; trailing-zero formatting; timezone of "When" column (Toronto / `DISPLAY_TIMEZONE`).
- Audited tech debt + bugs, rebuilt test coverage (`test_admin/dedup/routing/webhook`).

**Infra / deploy (important — current state)**
- Migrated OFF Fly.io (user hit SJC edge TLS resets / reliability issues) → **AWS Lightsail**.
- Live host: `mochi-position-manager.duckdns.org` → **54.254.184.227** (SIN). Caddy in front.
- The repo was renamed/moved from `algo-trade-backtesting/tradingview_middleware` to
  `~/Desktop/Algo Trading/mochi-position-manager` (this repo). GitHub: `haufung80/mochi-position-manager`.
- Cleaned up unrelated AWS billing resources.

**OPEN THREAD at session end (most important to resume):**
- New feature: track signal `{{price}}` vs actual exchange fill price (slippage) + commission per
  exchange, surfaced as new columns on the dashboard `/orders` view.
- At session end the `/orders` list looked **empty in the user's browser**, but the server was
  verified serving 25–36 valid rows. Diagnosis: a cache between browser and box; a
  `cache-control: no-store, must-revalidate` header was just deployed. User was asked to hard-reload
  / try `https://mochi-position-manager.duckdns.org/orders?x=1` / incognito / cellular.
- A background watcher `/tmp/mochi_watch.sh` was polling `/orders` every 90s to catch the first
  post-feature fill and report signal/fill/slippage/fee. (That temp script is almost certainly gone
  now — re-establish monitoring if still needed.)

---

## Session B — this session (docs cleanup, short)
cwd `mochi-position-manager`. Work done:
- **Created `CLAUDE.md`** (architecture of record for future sessions).
- **Brought `README.md` back in sync with the code:**
  - `strategies.yaml` schema → `base_asset` + `venues: {exchange: bool}` (old
    exchange/symbol/quantity_usd/leverage shape was stale).
  - TradingView alert body now documents the **required `quantity`** field
    (`{{strategy.order.contracts}}`), `alert_id` → single `{{time}}`, dropped unsupported
    `close/close_long/close_short` actions (buy/sell only).
  - Architecture diagram → fast sync response + background threadpool fan-out + dual-ledger.
  - Persistence: 3 → 4 tables (added `strategy_positions`, noted WAL).
  - Endpoints table + project layout updated; stale `tradingview_middleware` dir name fixed.

**Loose end I flagged (not yet resolved):**
- `/admin/reload-strategies` is defined in **both** `app/routes/dashboard.py:340` and
  `app/routes/admin.py:131` — likely a duplicate route registration (admin's include_router runs
  after dashboard's, so one shadows the other). Decide which to keep.

---

## Suggested next actions for the pooled session
1. Confirm the slippage/commission columns now populate on the live `/orders` dashboard (resolve the
   cache/empty-list thread). Re-arm a fill watcher if you still want auto-reporting.
2. Verify the 2x default leverage is actually being set on order placement.
3. Resolve the duplicate `/admin/reload-strategies` route.
4. Keep `CLAUDE.md` / `README.md` in sync as features land.
