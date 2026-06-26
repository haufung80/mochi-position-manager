# Idle-Cash Management — requirements brief (funding-arb)

> **Status:** requirements / what-to-build. **Not** an implementation plan — the position-manager
> session should read this and produce its own plan (file layout, exchange endpoints, schemas, worker
> wiring, tests). Authored from the `mochi-carry-backtester` research below.

## Why

In single-venue cash-and-carry, capital is **un-deployed a large fraction of the time**:
- **Fully idle** whenever the carry signal is flat (funding quiet) — and funding is highly correlated
  across coins, so the *whole* book tends to go flat together.
- **Partially idle** even while in a position: at 1× we deploy `position_notional`, so any capital
  beyond that (and the margin buffer) sits in stablecoins doing nothing.

The backtester now models the uplift from earning a baseline on that idle cash (`cash_yield_apr`, and
optionally a historical T-bill/SOFR path). The result is striking — on **HL BTC over ~3 years** with
the strategy in-market only **29%** of the time:

| idle yield | net profit | funding harvested | idle yield earned |
|---|---|---|---|
| **0%/yr** | $19.49 (3.90%) | $21.85 | $0 |
| **4%/yr** | $77.19 (**15.44%**) | $21.85 | **$57.70** |

i.e. **idle yield can exceed the carry itself** and roughly **4×** the net return. So earning baseline
yield on un-deployed stablecoins in the live arb account is high-value — *provided it never
compromises the delta-neutral hedge*.

## Goal

In the **dedicated funding-arb account only**, automatically earn a stablecoin baseline yield on
un-deployed capital, and unwind it in time to fund every carry entry — with hedge safety first.

## Venue mechanics (researched June 2026 — verify against current docs when planning)

**Hyperliquid — nearly free, the easy case.** Under **portfolio margin**, the account *"pays interest
on borrowed assets and earns interest on idle assets at the same rate"* — stablecoin ≈
`0.05 + 4.75·max(0, util−0.8)` APY, compounding continuously. USDC is now the **Aligned Quote Asset**
(native margin/spot/perp collateral), and idle collateral is **instantly usable as margin (no
redeem step)**. There is also an explicit "supply USDC to earn" on the Earn page. → For HL this is
largely *ensure portfolio-margin mode is enabled + track accrued interest*, not an orchestration.

**Bybit — explicit product, needs orchestration.** **Earn Flexible Savings** now has an **OpenAPI**
(subscribe / redeem, query positions + estimated APR), on Unified Trading and Funding accounts,
supporting **USDT/USDC**, **no lock-up** (redeem anytime). → Subscribe idle balance, **redeem before
firing a carry order**. Mind redemption latency / partial fills.

Sources: Bybit Earn OpenAPI (prnewswire 302385895) · Bybit Easy Earn · Hyperliquid Docs → Portfolio
margin · Talos "State of the Network" (HL USDC yield).

## What to build (for the PM session to plan)

1. **Idle-cash manager**, scoped to the funding-arb account (reuse the existing arb-account guard;
   **never** touch the directional account).
2. **Idle sizing:** `idle = stable_balance − (margin reserved for open arb legs + safety buffer)`.
   Keep a buffer for taker fees, slippage, and variation margin — never stake what the hedge needs.
3. **Hyperliquid path:** detect / ensure portfolio-margin is on; (optionally) route surplus to Earn
   supply; read and record accrued interest. No redeem step needed for margin reuse.
4. **Bybit path:** subscribe idle USDT/USDC to Flexible Savings via the Earn API; **redeem-then-trade**
   ordering around every entry; handle redemption latency, partial redemption, and failures.
5. **Hedge safety (hard invariant):** a carry entry must never be delayed/unhedged by staking.
   Free the needed margin *before* placing the first leg; if redemption can't confirm in time, skip the
   stake rather than risk a missed/half hedge.
6. **Crash-safe + idempotent:** persist staked state (an `Arb*`-style table), reconcile staked vs free
   on startup, isolate per-venue failures (one bad call must not wedge the worker).
7. **Reporting:** record idle yield earned as its own realized-P&L component so arb P&L mirrors the
   backtester's `cash_yield` line (don't bury it inside funding).
8. **Alerts:** stake / redeem / failure via the existing arb Telegram notifier.
9. **Config:** enable/disable, buffer %, min-stake threshold, per-venue product IDs/params, max
   counterparty exposure.
10. **Risk controls:** counterparty + USDC-depeg exposure cap, a kill-switch, and a documented
    redemption-under-stress plan (Earn can gate redemptions when utilization spikes).

## Constraints / non-goals

- **Don't break existing arb execution**; **add test cases** for the new paths; **remove tech debt**
  (the standing rules for this repo).
- Idle yield **re-introduces** counterparty / peg / liquidity risk that delta-neutral was avoiding —
  treat it as a deliberate, capped, toggleable exposure, not free money.
- Rates float and aren't guaranteed; don't assume a fixed APR.

## Open questions for the planning session

- Where in the worker loop does stake/redeem run, and how does it coordinate with the
  approve-to-fire / `/open` flow so redemption always precedes a fill?
- How to detect HL portfolio-margin state and read accrued idle interest via API?
- Does the **OpenAPI funding-arb contract** need a "capital available / staked" field so the signal app
  knows deployable capital, or is this purely internal to the executor?
- Reconciliation model for staked balances across restarts (extend the existing `Arb*` tables?).

## Reference

- Backtester model: `mochi-carry-backtester` → `config.py` (`cash_yield_apr`), `engine.py` (idle-cash
  accrual on the un-deployed balance), surfaced in the CLI/dashboard as a separate P&L component.
