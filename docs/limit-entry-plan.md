# Limit-entry + cancel-on-close — design plan

**Status:** DRAFT for review (no code yet). **SDK feasibility CONFIRMED** — pybit 5.8.0 and
hyperliquid-python-sdk 0.23.0 both fully support GTC resting limits, cancel, single-order status,
and client-order-ids (§10).

## 1. Goal

Let a strategy's **entry (open)** be a **resting LIMIT order at the alert price** instead of a
market order. If the **close/opposite signal** arrives before the limit fills, **cancel** the
resting order. Optionally, expire it after a max age.

Two wins:
1. **Execution quality** — you enter at your price (no spread/slippage, never worse than the
   signal price), and you don't *chase* a runaway entry.
2. **Fixes the missed-signal desync** (the SOL_MACD case): a dropped/late entry that never fills
   is *cancelled* on the close rather than leaving the manager flat so the close opens a wrong-way
   short. The entry and its close stay a matched pair.

> A buy-limit at the alert price when the market is **below** it is immediately marketable → it
> fills at once at the better price. It only *rests* when price has run **away** (above the buy
> price). So "wait, maybe cancel" engages exactly when you'd otherwise be chasing.

## 2. Decisions to confirm (defaults = my recommendation)

| # | Decision | Default |
|---|---|---|
| D1 | Partial fill, then the close fires | **Close the filled part** (market), cancel the remainder |
| D2 | Expiry | **No max-age (DECIDED).** The entry rests until the close signal cancels it — cancel-on-close is the *only* auto-cancel (a strategy always pairs entry→exit, so the close is the natural trigger; a timer risks cancelling a still-valid resting order). Safety net: a **notify-only staleness alert** — Telegram ping **every 24 h** while an entry rests unfilled (recurring nag, never cancels), guarding the case where the close webhook is *also* dropped. See §7/§13. |
| D3 | Close side | **Market** (fill certainty on the exit; only the *entry* is a limit) |
| D4 | Scope of `entry: limit` | **Managed OPENs first** (the SOL case). `sar` opens later if wanted. CLOSE is always market. |
| D5 | Marketable limit (fills immediately) | Treat as a normal fill (price-protected entry) — **intended**, not a special case |

## 3. The invariant this breaks (and why it's the whole cost)

Today (per `CLAUDE.md` + `app/executor.py`): **"market orders only"** and **fills are
synchronous** — `execute_order` places a market order, gets the fill back in the same call, and
updates the ledger right there. A resting limit order breaks both:

- **Resting orders** must be *polled* for fills and *cancelled* on demand → a new background worker.
- **Async + partial fills** → the ledger update no longer happens at placement; it happens later,
  possibly in pieces.

Everything below is the ripple from that. The good news (§9): the ledger *math* already supports
partial fills, so we reuse it rather than rewrite it.

## 4. Order model changes (`app/models.py` + additive migration)

Add columns to `Order` (all nullable / defaulted so existing rows + the market path are byte-identical):

| Column | Type | Meaning |
|---|---|---|
| `order_type` | str, default `"market"` | `"market"` \| `"limit"` |
| `limit_price` | float, null | the resting price (= alert `signal_price` for entries) |
| `qty_base_filled` | float, default `0.0` | cumulative filled base (drives partial-fill deltas) |
| `client_order_id` | str, null | deterministic id we supply (crash-safe cancel/query — see §8) |
| `last_stale_alert_at` | datetime, null | timestamp of the last staleness ping, so it re-pings every 24 h (not every poll) while resting — D2 |

New `Order.status` values: **`working`** (placed + resting, 0–partial fill) and **`cancelled`**.
Existing: `pending`, `success`, `retrying`, `dead`, `rejected` — unchanged.

Register the new columns in `_SQLITE_ADDITIVE_COLUMNS` (`app/db.py`) so live deploys get them
(the project's hand-rolled additive-migration rule).

## 5. Config (`strategies.yaml` → `StrategyRoute`)

Per-strategy, optional:

```yaml
SOL_MACD_REV_LONG_15m:
  base_asset: SOL
  position_size: 142
  entry: limit            # market (default) | limit  — limit rests at the alert price
  venues: { bybit: true, hyperliquid: true }
```

Parsed into `StrategyRoute.entry`. `entry: limit` only changes the **OPEN** leg; CLOSE stays
market. Admin UI gets the toggle (parallels the existing `sar`/`position_size` fields). Default
`market` → every existing strategy behaves exactly as today. (No max-age field — D2.) The
staleness-alert cadence is a single global setting `LIMIT_STALE_ALERT_HOURS` (default 24 → re-ping
every 24 h while resting; 0 = off), in `app/config.py` `Settings`.

## 6. Placement path

`portfolio.decide` already returns the OPEN intent (side, qty). New branch in the executor:

- **Market open** (today): unchanged.
- **Limit open** (`entry == limit`): call the new `limit_order(symbol, side, qty, price=limit,
  client_order_id, tif="GTC")`. The Order row is created with `order_type="limit"`,
  `limit_price`, `expires_at` (if max-age set), `status="working"` (or `success` if the response
  shows it filled immediately — marketable case). **No ledger update at placement** — the position
  only moves when a fill is observed (§7). The `limit_price` must be snapped to the venue tick size.

Cap + kill-switch apply at placement exactly as for market opens (notional = `qty * limit_price`).

## 7. Fill poller (`app/limit_worker.py`, modeled on `retry_worker.py`)

A new background coroutine in the lifespan (alongside retry + funding workers), same shape as
`retry_loop`: every ~3–5 s, `asyncio.to_thread(_poll_working_orders)`:

```
for order in Order.status == "working":           # own session_scope, .limit(N)
    st = adapter.order_status(order.symbol, order.exchange_order_id or order.client_order_id)
    newly_filled = st.filled_qty - order.qty_base_filled
    if newly_filled > eps:
        order.realized_pnl += _apply_fill_to_position(   # reuse! partial-capable, takes _LEDGER_LOCK
            db, strategy_id, exchange, symbol, side, newly_filled, st.avg_price)
        order.qty_base_filled = st.filled_qty
        order.fill_price = st.avg_price; order.commission += st.commission_delta
    if st.state == "filled":      order.status = "success"
    elif st.state == "cancelled": order.status = "cancelled"   # (records any partial above)
    elif order.status == "working" and LIMIT_STALE_ALERT_HOURS \
         and now - (order.last_stale_alert_at or order.created_at) > timedelta(hours=LIMIT_STALE_ALERT_HOURS):
        notifier.limit_order_stale(order)            # NOTIFY ONLY — never cancels; re-pings every 24h (D2)
        order.last_stale_alert_at = now
```

Reuses `_apply_fill_to_position` (which takes `_LEDGER_LOCK` internally), so concurrent fills stay
correct. Because it scans **persisted** `working` rows, it **survives restarts for free** (the
order row is in the DB) — boot-recovery is mostly "the first poll after boot reconciles" (§8).

## 8. Cancel-on-close (the safety) + crash safety

**Cancel-on-close** lives in the webhook fan-out, *before* `decide`:

```
on a signal for (strategy, exchange, symbol):
  w = working limit ENTRY order for this (strategy, exchange, symbol)?
  if w and signal is the opposite/close side:
      cancel w on the exchange; apply any partial fill; w.status = cancelled
      if net position (from the partial) == flat:  done — NO new order (this is the fix)
      else: proceed to CLOSE the partial (market, D1)
  else:
      normal decide()/execute path
```

This is what stops "close into a short": an unfilled entry is cancelled, leaving the book flat,
and the close becomes a no-op instead of a fresh short.

**Crash safety / boot-recovery:** place every limit with a **deterministic `client_order_id`**
(e.g. `f"mochi-{order.id}"` or derived from the alert idempotency key) so we can query/cancel it
even if the app died between "exchange accepted" and "we persisted `exchange_order_id`". On
startup, a one-shot reconcile pass over `Order.status=="working"` queries each (apply fills that
happened while down, detect external cancels) before the poller's normal cadence. **Not optional**
— CI auto-deploys restart the app and could otherwise orphan a resting order. (Exact client-id
support per venue is the open SDK question being confirmed.)

## 9. Ledger reuse — already partial-capable

`_apply_fill_to_position` → `_apply_strategy_fill` → `_fill_math` already handle open-from-flat,
same-direction increase (weighted avg), partial/full close (realize on the closed portion), and
cross-zero reversal — all parameterized by a `qty`. So a limit that fills in 3 chunks = 3 calls
with the chunk deltas; the avg-entry/realized math is identical to a single fill. **No ledger math
change** — only *when* and *how often* it's called. `_LEDGER_LOCK` already serializes it across
threads, so the new poller thread is safe alongside the webhook threadpool + retry/funding workers.

## 10. Adapter additions (`app/exchanges/*`) — SDK FEASIBILITY CONFIRMED

Add to the `Exchange` Protocol (`base.py`) + both adapters:

- `limit_order(symbol, side, qty, price, *, client_order_id, tif="GTC") -> OrderResult`
  (returns `exchange_order_id` + whether it filled immediately).
- `cancel_order(symbol, order_id_or_client_id) -> bool`.
- `order_status(symbol, order_id_or_client_id) -> {filled_qty, avg_price, state, commission}`
  where `state ∈ {working, partially_filled, filled, cancelled, rejected}`.

Both SDKs support everything required (versions: pybit 5.8.0, hyperliquid-python-sdk 0.23.0):

| Capability | **Bybit** (pybit `HTTP`) | **Hyperliquid** (`exchange`/`info`) |
|---|---|---|
| GTC limit | `place_order(category, symbol, side, orderType="Limit", qty, price, timeInForce="GTC", orderLinkId)` | `exchange.order(name, is_buy, sz, limit_px, order_type={"limit":{"tif":"Gtc"}}, cloid)` |
| Cancel | `cancel_order(category, symbol, orderId` **or** `orderLinkId)` | `cancel(name, oid:int)` or `cancel_by_cloid(name, cloid)` |
| Status (one order) | `get_open_orders(...)` while live **+** `get_order_history(...)` once terminal — `orderStatus`, `cumExecQty`, `leavesQty`, `avgPrice`, `cumExecFee` | `info.query_order_by_oid(addr, oid)` / `query_order_by_cloid(addr, cloid)` → `status` open/filled/canceled; fills via existing `user_fills` |
| Client-order-id | **`orderLinkId`** — standalone handle (place/cancel/query by it; needn't persist Bybit's `orderId`) | **`Cloid`** (16-byte hex) — but cancel needs the coin `name`, query needs the account address (both on our Order row) |
| Immediate-fill ack | order row → `Filled`/`PartiallyFilled` | response `statuses[]` → `{"filled":{oid,totalSz,avgPx}}` |
| Resting ack | order row → `New` | response `statuses[]` → **`{"resting":{oid[,cloid]}}`** |

**Venue gotchas to handle in the adapters (P0):**
1. **Bybit price → tickSize rounding.** Today only *qty* is grid-snapped (`_round_qty`). A limit
   `price` off the `priceFilter.tickSize` grid is **rejected**. Add a `_round_price` mirror
   (`tickSize` is already in the cached `_instrument(symbol)`).
2. **HL price rounding is a HARD client-side raise.** `float_to_wire` throws `ValueError` if
   `limit_px` isn't expressible (≤5 sig-figs perp); reuse the `_slippage_price` rounding rule
   *before* `order(...)`. Not a soft rejection — it raises before any network call.
3. **HL resting-response branch.** The current `market_order` parser only reads `filled`/`error`
   from `statuses[]`; the new `limit_order` must also read **`resting.oid`** (and cast HL's int
   `oid` consistently). The existing market path is untouched.
4. **Bybit two-endpoint status.** `get_open_orders` only returns *live* (unfilled/partial); once
   `Filled`/`Cancelled` the order is in `get_order_history`. `order_status` tries open → falls back
   to history (so a fill that completed between polls isn't "lost").
5. **No per-order expiry primitive** on either venue (HL `schedule_cancel` is account-wide, ≤10/day)
   → the **max-age cancel is owned by our poller** (§7), not the exchange. Confirmed.
6. **Min-notional applies to limits too** (Bybit `minNotionalValue`; HL flat $10) — same
   `get_min_notional` helpers, evaluated against *our limit price*.

## 11. Reporting (`app/routes/dashboard.py`)

- `/orders` shows `working` (resting) + `cancelled` rows distinctly (status badge already
  data-driven). A working row shows `qty_base_filled / qty_base` and `limit_price`.
- `/performance` PnL is unchanged in definition: it sums **filled** quantity only. A `working`
  order with `qty_base_filled==0` contributes 0; a partial contributes its filled part (already
  applied to the ledger). No equity-curve change.
- Execution-quality slippage: a limit fill's slippage vs the signal price is ~0 by construction —
  a nice signal that the feature is doing its job.

## 12. Interactions / invariants to preserve

- **Per-order cap + kill-switch:** apply at *placement* (notional = qty·limit_price). Kill-switch
  blocks new limit opens; **cancel-on-close is NEVER blocked** (de-risk always allowed).
- **Retry ladder vs poller:** `retrying` = failed to *place* (network) → existing retry worker.
  `working` = placed, tracking fills → new poller. Disjoint scans, like the directional/arb split.
- **Idempotency:** a duplicate alert while an entry is `working` → no second order (the working
  order already is the intent; managed pyramiding-reject covers it).
- **`reconcile.py`** stays blind to working orders (intent not yet realized); it re-baselines from
  *filled* state only. No change.
- **Arb isolation:** untouched — this is the directional path only.

## 13. Edge cases

- Marketable limit → immediate fill (D5) — placement response already filled; apply + `success`.
- Partial fill then close (D1) → close the filled part, cancel remainder.
- Never fills + the close webhook is **also** dropped (or the strategy is removed mid-rest) → the
  order keeps resting (no auto-cancel, D2); the **notify-only staleness alert** pings you to handle
  it manually. This is the deliberate trade for "no max-age."
- Price gaps fully through the limit → single full fill.
- Second entry signal while working → pyramiding-reject (no double entry).
- App restart mid-rest → poller resumes from the persisted `working` row (§8).
- Exchange rejects the limit (price too far / min-notional) → `_on_failure` path (retry/dead).
- External manual cancel on the exchange → poller sees `cancelled`, records any partial, closes the row.

## 14. Testing (keep ≥75%, suite green)

Extend `tests/conftest.py` `FakeExchange` with `limit_order` / `cancel_order` / `order_status`
plus knobs to script a fill trajectory (unfilled → partial → full, or cancelled). New tests:

- placement → `working`, no ledger move; marketable → immediate `success` + ledger move.
- poller: partial fill applies the delta; second poll completes → `success`; cumulative
  `qty_base_filled` never double-counts.
- cancel-on-close: unfilled entry → cancelled, **no short opened** (the core regression test);
  partial entry → partial closed.
- max-age expiry → cancel.
- boot-recovery: a `working` row reconciles on the first poll after "restart".
- cap/kill-switch interaction at placement; cancel-on-close works with kill-switch ON.

## 15. Phased rollout (each phase shippable + tested)

- **P0** — adapters: `limit_order`/`cancel_order`/`order_status` on both venues + fakes + tests.
- **P1** — model columns + statuses + migration; limit-entry placement; the fill poller (reusing
  `_fill_math`); marketable-immediate. Managed OPEN only.
- **P2** — cancel-on-close in the fan-out + partial-then-close (D1). *(This is the desync fix.)*
- **P3** — boot-recovery reconcile + client-order-id crash safety + notify-only staleness alert (D2).
- **P4** — reporting (`/orders` working/cancelled) + admin UI toggle + docs.

Each phase is a separate green PR. P1+P2 deliver the core value; P3 hardens it for the live box.

## 16. What this does NOT do

- Does not change the close to a limit (D3 = market close).
- Does not add limit entries to `sar` strategies in P1 (D4; addable later).
- Does not unstick the *current* SOL position — that still needs the one-off re-fire or
  temporary-disable, independent of this feature.
