# Mochi Position Manager

FastAPI middleware that sits between TradingView alerts and your exchanges (Bybit + Hyperliquid).
It eliminates the "ghosted signal" problem of the default TradingView → Bybit direct webhook by giving you
**observability, deduplication, retries, and Telegram alerting** on every signal.

Companion to [Mochi Portfolio](../web_app/README.md) — Mochi tells you *which* strategies to keep running;
this app actually *runs* them.

---

## What problem this solves

The default TradingView → Bybit native integration is opaque. When something goes wrong, you don't
hear about it until you reconcile your account days later. Symptoms this middleware fixes:

| Failure mode | Why it happens | What this middleware does |
|---|---|---|
| **Ghost signal** — TV claims fired, no exchange order | Bybit webhook silently dropped (downtime, rate limit, bad symbol) | Every signal hits SQLite first → you can see exactly what was received and what happened next |
| **Duplicate fires** — same alert delivered twice | TV alert delivery retries, bar repaint, double-click on "Test" | Idempotency key `(strategy_id, alert_id)` → second hit returns `{status: duplicate}` |
| **Transient failures** — exchange returns 5xx or rate-limit | Bybit/HL maintenance, network blip | Auto-retry with exponential backoff (configurable), Telegram alert after final dead-letter |
| **No visibility** — what positions are open right now? | TV doesn't expose this | `/positions` endpoint + dashboard shows internal net per `(exchange, symbol)` |

---

## Architecture

```
TradingView alert
       │   HTTPS POST {strategy_id, action, alert_id, secret}
       ▼
┌─────────────────────────────────────┐
│  POST /webhook/tradingview          │
│  fast + synchronous (sub-second):   │
│   1. Validate secret + schema       │
│   2. Insert Alert (UNIQUE idemp_key)│──► duplicate? return 200 {duplicate}
│   3. Look up route in strategies.yaml│──► unknown/disabled? log + Telegram
│   4. return 200 {accepted}          │  ◄── TradingView gets an answer NOW
└────────────┬────────────────────────┘
             │ background task (threadpool — blocking SDK calls):
             ▼
   fan out across the strategy's enabled venues, one txn each:
     • Call exchange adapter (market order)
     • On success: update Position + StrategyPosition ledgers
     • On failure: mark retrying
             │
             │ background:
             ▼
   retry_worker (every 5s)
   picks orders where status=retrying AND next_retry_at <= now
   exponential backoff: 2s → 6s → 18s → 60s (capped) → dead
                                                   │
                                                   ▼
                                       Telegram: "🚨 Order DEAD"
```

**Persistence**: SQLite (`./data/middleware.db`, WAL mode) with four tables — `alerts`, `orders`,
`positions` (net per exchange+symbol), and `strategy_positions` (net per strategy+exchange+symbol).
No external services required.

---

## Quick start (local dev)

```bash
cd mochi-position-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env                           # fill in secrets
cp strategies.yaml.example strategies.yaml     # configure your routing

# Run with auto-reload
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/` for the dashboard.

To test a webhook locally (no exchange will be touched if `DRY_RUN=true` in `.env`):

```bash
curl -X POST http://localhost:8000/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "your-webhook-secret",
    "strategy_id": "MR_VOTING_BTC_6H",
    "action": "buy",
    "quantity": 0.001,
    "alert_id": "test-1"
  }'
```

---

## Production deploy (Docker)

```bash
cp .env.example .env             # configure with real API keys, NOT testnet
cp strategies.yaml.example strategies.yaml
docker compose up -d --build
docker compose logs -f middleware
```

### Exposing to TradingView

TradingView only POSTs to **public HTTPS URLs**. Options:

| Option | Cost | Ease | Notes |
|---|---|---|---|
| **Cloudflare Tunnel** (recommended) | Free | ★★★★★ | No open ports, free TLS cert, kills laptop sleep risk if VPS. Uncomment the `cloudflared` service in `docker-compose.yml`. |
| **Caddy reverse proxy** | $5/mo VPS | ★★★★ | Automatic Let's Encrypt; needs port 80/443 open. |
| **ngrok (paid tier for static URL)** | $8/mo | ★★★★★ | Easiest for dev/test, but reserve a static URL — free ngrok URLs rotate. |

### TradingView alert configuration

In TradingView → Alert → *Notifications* tab:

- **Webhook URL**: `https://your-domain.example.com/webhook/tradingview`
- **Message** (paste verbatim):

```json
{
  "secret": "your-webhook-secret",
  "strategy_id": "MR_VOTING_BTC_6H",
  "action": "{{strategy.order.action}}",
  "quantity": {{strategy.order.contracts}},
  "alert_id": "{{time}}",
  "price": {{close}}
}
```

> **Important.** Use `{{strategy.order.action}}` in pinescript strategies — it auto-fills as `buy` or `sell`.
> For pure indicator alerts (not strategies), hardcode `"action": "buy"` / `"sell"`.

Payload fields the middleware reads:
- `action` — `buy` or `sell` only. The middleware always places a plain **market order** in that
  direction. On one-way-mode perps (Bybit/HL defaults) a sell against an open long naturally closes it;
  a sell against a flat position opens a short. Your pine script owns the entry/exit action sequence.
- `quantity` — **required, > 0**, in **base-asset units** (e.g. `0.001` = 0.001 BTC), surfaced via
  `{{strategy.order.contracts}}`. This is NOT a USD amount — if your pine script sizes in dollars,
  convert to base before sending (`qty := cash_size / close`).
- `alert_id` — the dedup key. Keep it a **single** placeholder; `{{time}}` (the bar's timestamp) is
  enough to collapse a repainting bar's re-fires to one alert while letting the next bar through.
- `price` — optional `{{close}}`; recorded as the signal price to measure fill slippage against.

---

## Deploy (AWS Lightsail)

The live instance runs on a single **AWS Lightsail** VM in the Singapore region (for sub-10ms latency
to Bybit's matching engine), as a two-container Docker Compose stack (`docker-compose.prod.yml`):

- **app** — the FastAPI middleware. Not exposed to the host; only reachable on the internal compose network.
- **caddy** — reverse proxy that terminates HTTPS, auto-provisioning + renewing a Let's Encrypt cert
  for `mochi-position-manager.duckdns.org`. Only its 80/443 are public.

SQLite (`middleware.db`) and `strategies.yaml` live on the host at `./data` (bind-mounted to
`/app/data`), so they survive restarts and redeploys. Caddy's certs persist in the `caddy_data` named
volume — **don't delete it** or you'll burn Let's Encrypt rate limits re-issuing.

### One-time setup (on the VM)

```bash
# DNS: point mochi-position-manager.duckdns.org at the VM's static IP; open ports 80 + 443.
git clone https://github.com/haufung80/mochi-position-manager.git
cd mochi-position-manager

cp .env.example .env                              # real API keys, DRY_RUN=false, WEBHOOK_SECRET, Telegram
mkdir -p data
cp strategies.yaml.example data/strategies.yaml   # then edit, or manage via the /admin UI

docker compose -f docker-compose.prod.yml up -d --build
```

Secrets are read from `.env` on the box (`env_file: .env` in the compose file) — they never enter the
image or git.

### Deploying code changes

**CI/CD (recommended):** `.github/workflows/ci-cd.yml` runs the test suite + coverage gate
(`pytest --cov=app --cov-fail-under=75`) on every PR and push to `main`. When **main** goes green it
auto-deploys to the Lightsail box over SSH (`git pull --ff-only && docker compose -f docker-compose.prod.yml up -d --build`)
and health-checks it — so a merge to `main` ships. A red test/coverage run blocks the deploy.

One-time, add three repo secrets under **Settings → Secrets and variables → Actions** (until they
exist the deploy job safely no-ops while tests still gate):

| Secret | Value |
|---|---|
| `LIGHTSAIL_HOST` | the VM's IP / hostname |
| `LIGHTSAIL_USER` | SSH user (e.g. `ubuntu`) |
| `LIGHTSAIL_SSH_KEY` | full contents of the private key `.pem` |

**Manual fallback** — SSH to the VM, then:

```bash
cd ~/mochi          # the repo checkout on the box
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

The `./data` bind-mount means the ledger DB and strategies survive the rebuild.

### Operating it

```bash
docker compose -f docker-compose.prod.yml logs -f app    # tail app logs
docker compose -f docker-compose.prod.yml restart app    # restart app only
curl https://mochi-position-manager.duckdns.org/health
open  https://mochi-position-manager.duckdns.org/        # the dashboard
sqlite3 data/middleware.db                               # inspect the ledger
```

> **Legacy:** `fly.toml` remains from an earlier Fly.io deployment the project migrated off. It's
> vestigial — the live deploy is Lightsail, as above.

---

## `strategies.yaml`

A strategy declares a canonical `base_asset` and the `venues` it fans out to. The exchange-native
symbol is resolved at load time (`BTC` → `BTCUSDT` on Bybit, `BTC` on Hyperliquid). Per-signal order
**size is NOT stored here** — it comes from the TradingView alert payload (`quantity`, in base-asset
units), so your pine-script sizing logic owns it per signal. An optional `sar` flag marks a
stop-and-reverse / always-in-market strategy — a **label only** today, it doesn't change order handling.

```yaml
strategies:
  MR_VOTING_BTC_6H:
    base_asset: BTC          # canonical ticker — one of: BTC / ETH / SOL / BNB
    sar: false               # optional; stop-and-reverse marker (label only)
    venues:
      bybit: true            # fan out to both venues; flip to false to disable one
      hyperliquid: false
```

Supported base assets and venues live in `app/exchanges/symbols.py` — to add an exchange, add one
entry there plus an adapter under `app/exchanges/`.

Edit via the admin UI at `/admin/strategies` (create/update/delete/toggle, plus "sync positions"), or
edit the file and reload without restarting:
```bash
curl -X POST http://localhost:8000/admin/reload-strategies -d "secret=your-webhook-secret"
```

---

## Telegram alerts setup

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → grab the token.
2. Send any message to your new bot.
3. `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → find your `chat.id`.
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

You'll get notified on: order failures (each retry), dead-letters (after max attempts),
unknown strategy IDs (likely misconfigured TV alert), and successful fills.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/tradingview` | TradingView posts here |
| GET  | `/` | HTML dashboard (positions, recent alerts, recent orders) |
| GET  | `/health` | Liveness probe |
| GET  | `/positions` | JSON: net position per (exchange, symbol) |
| GET  | `/strategy-positions` | JSON: net position per (strategy, exchange, symbol) |
| GET  | `/alerts?limit=100` | JSON: recent alerts |
| GET  | `/orders?status=retrying` | JSON: orders, filterable |
| GET  | `/network/egress-ip` | JSON: the app's outbound IP (for exchange API allowlists) |
| GET  | `/admin/strategies` | HTML UI to create / update / delete / toggle strategies |
| POST | `/admin/strategies`, `/admin/strategies/delete/{sid}`, `/admin/strategies/toggle/{sid}/{exchange}` | Strategy mutations (require `secret` form field) |
| POST | `/admin/strategies/sync-positions` | Re-baseline per-strategy ledger to live exchange state |
| POST | `/admin/reload-strategies` | Re-read `strategies.yaml` without restart (requires `secret`) |

---

## Operational notes

- **Always-market-order semantics.** This middleware fires market orders only — no limit orders, no SL/TP, no
  exchange-side reconciliation. The `Position` table tracks *intent* (signal-derived net), not the actual
  exchange state. If you trade the same account manually, the two will drift.
- **Dead-lettered orders are not auto-replayed.** After `RETRY_MAX_ATTEMPTS` (default 4), the order is marked
  `dead` and Telegram-pings you. You manually decide whether to replay (DB update) or skip.
- **Idempotency.** Set `alert_id` to a **single** placeholder unique per intended fire — `{{time}}` (the
  triggering bar's timestamp) is recommended; TradingView's alert editor flags a JSON warning if you
  concatenate several placeholders. The key collapses a repainting bar's re-fires to one alert while
  letting the next bar through. If you omit `alert_id`, the middleware falls back to a hash of
  `(strategy_id, action, bar_time)`.
- **Dry-run mode.** Set `DRY_RUN=true` to log every order without touching exchanges. Position ledger still
  updates so you can verify routing end-to-end.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -v
```

Covers: dedup logic, YAML routing, multi-venue fan-out, full webhook → executor → position flow (with a
mocked exchange), duplicate handling, unknown/disabled strategies, failure → retry transition, and the
admin strategy CRUD endpoints. Tests force `DRY_RUN` + a temp SQLite DB (see `tests/conftest.py`).

---

## Project layout

```
mochi-position-manager/
├── app/
│   ├── main.py              FastAPI app + lifespan (boots retry worker)
│   ├── config.py            pydantic-settings (env-driven)
│   ├── db.py                SQLAlchemy session + init + additive SQLite migrations (WAL)
│   ├── models.py            Alert / Order / Position / StrategyPosition tables
│   ├── schemas.py           TradingView payload + OrderResult
│   ├── routing.py           YAML → in-memory StrategyRoute / VenueRoute cache
│   ├── strategy_store.py    YAML read/write (atomic) — single source for on-disk shape
│   ├── dedup.py             idempotency_key
│   ├── executor.py          Alert + Venue → exchange call → DB update (one venue)
│   ├── retry_worker.py      Async background poller for retrying orders
│   ├── reconcile.py         Re-baseline per-strategy ledger to live exchange positions
│   ├── network.py           Outbound egress-IP lookup
│   ├── notifier.py          Telegram client
│   ├── exchanges/
│   │   ├── base.py          Exchange protocol
│   │   ├── symbols.py       Canonical base asset → exchange-native symbol (source of truth)
│   │   ├── bybit.py         pybit V5 adapter
│   │   ├── hyperliquid.py   hyperliquid-python-sdk adapter
│   │   └── registry.py      Lazy singleton per exchange
│   ├── routes/
│   │   ├── webhook.py       POST /webhook/tradingview (fast path + background fan-out)
│   │   ├── dashboard.py     GET / + JSON endpoints
│   │   └── admin.py         /admin/strategies HTML CRUD (secret-gated)
│   └── templates/           dashboard.html + admin_strategies.html
├── tests/
├── strategies.yaml.example
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Known limitations / roadmap

- **No exchange-side reconciliation.** Position drift between internal ledger and actual exchange state
  goes undetected. *Future:* add a periodic reconciler that hits `/positions` on each exchange and pages
  if mismatch > 5%.
- **Single account per exchange.** If you want sub-accounts (e.g. one Bybit account per strategy family),
  extend `ExchangeRegistry` to key on `(exchange, account_alias)`.
- **No order types beyond market.** SL/TP would need TradingView to send a second alert and a way to bind
  them to the entry order ID.
- **No FX/spot.** Bybit adapter hardcodes `category=linear`. Extend if you need inverse perps or spot.
