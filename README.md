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
│   1. Validate secret + schema       │
│   2. Insert Alert (UNIQUE idemp_key)│──► duplicate? return 200 {duplicate}
│   3. Look up route in strategies.yaml│──► unknown/disabled? log + Telegram
│   4. Call exchange adapter (market) │
│   5. On success: update Position    │
│   6. On failure: mark retrying      │
└────────────┬────────────────────────┘
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

**Persistence**: SQLite (`./data/middleware.db`) with three tables — `alerts`, `orders`, `positions`.
No external services required.

---

## Quick start (local dev)

```bash
cd tradingview_middleware
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
  "alert_id": "{{strategy.order.id}}-{{timenow}}",
  "bar_time": "{{timenow}}",
  "price": {{close}}
}
```

> **Important.** Use `{{strategy.order.action}}` in pinescript strategies — it auto-fills as `buy` or `sell`.
> For pure indicator alerts (not strategies), hardcode `"action": "buy"` / `"sell"` / `"close"`.

The middleware accepts these `action` values:
- `buy`, `sell` — opens a market order in that direction
- `close` — flat the position via `reduce_only` on the exchange
- `close_long`, `close_short` — direction-specific closes

---

## `strategies.yaml`

```yaml
strategies:
  MR_VOTING_BTC_6H:
    exchange: bybit         # bybit | hyperliquid
    symbol: BTCUSDT         # exchange-native: bybit→BTCUSDT, hyperliquid→BTC
    quantity_usd: 500       # $ notional per entry
    leverage: 3
    enabled: true
```

Reload without restarting:
```bash
curl -X POST http://localhost:8000/admin/reload-strategies
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
| GET  | `/alerts?limit=100` | JSON: recent alerts |
| GET  | `/orders?status=retrying` | JSON: orders, filterable |
| POST | `/admin/reload-strategies` | Re-read `strategies.yaml` without restart |

---

## Operational notes

- **Always-market-order semantics.** This middleware fires market orders only — no limit orders, no SL/TP, no
  exchange-side reconciliation. The `Position` table tracks *intent* (signal-derived net), not the actual
  exchange state. If you trade the same account manually, the two will drift.
- **Dead-lettered orders are not auto-replayed.** After `RETRY_MAX_ATTEMPTS` (default 4), the order is marked
  `dead` and Telegram-pings you. You manually decide whether to replay (DB update) or skip.
- **Idempotency.** Set `alert_id` to something unique per *intended fire* — `{{strategy.order.id}}-{{timenow}}`
  is what's recommended. If you omit it, the middleware falls back to a hash of
  `(strategy_id, action, bar_time)`.
- **Dry-run mode.** Set `DRY_RUN=true` to log every order without touching exchanges. Position ledger still
  updates so you can verify routing end-to-end.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -v
```

Covers: dedup logic, YAML routing, full webhook → executor → position flow (with mocked exchange),
duplicate handling, unknown/disabled strategies, failure → retry transition, close-action ledger zeroing.

---

## Project layout

```
tradingview_middleware/
├── app/
│   ├── main.py              FastAPI app + lifespan (boots retry worker)
│   ├── config.py            pydantic-settings (env-driven)
│   ├── db.py                SQLAlchemy session + init
│   ├── models.py            Alert / Order / Position tables
│   ├── schemas.py           TradingView payload + OrderResult
│   ├── routing.py           YAML → StrategyRoute lookup
│   ├── dedup.py             idempotency_key
│   ├── executor.py          Alert + Route → exchange call → DB update
│   ├── retry_worker.py      Async background poller for retrying orders
│   ├── notifier.py          Telegram client
│   ├── exchanges/
│   │   ├── base.py          Exchange protocol
│   │   ├── bybit.py         pybit V5 adapter
│   │   ├── hyperliquid.py   hyperliquid-python-sdk adapter
│   │   └── registry.py      Lazy singleton per exchange
│   ├── routes/
│   │   ├── webhook.py       POST /webhook/tradingview
│   │   └── dashboard.py     GET / + JSON endpoints
│   └── templates/dashboard.html
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
