"""Telegram notifier — fire-and-forget bot client.

Failures are logged, not raised — the trading path must not be blocked by a
flaky notification channel.

All formatting helpers take `quantity` in BASE-ASSET units (matching how the
rest of the app handles it). If a price is available, the Telegram message
also shows the approximate USD notional for convenience.
"""
from __future__ import annotations
import logging
import httpx
from .config import get_settings

log = logging.getLogger(__name__)


def _base_asset(symbol: str) -> str:
    """Strip the quote suffix to display 'BTC' instead of 'BTCUSDT' in
    notifications. Works for our two supported venues."""
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


def _fmt_qty(quantity: float, symbol: str, price: float = 0.0) -> str:
    """Format quantity + optional ~USD for human-readable messages."""
    base = _base_asset(symbol)
    if price > 0:
        return f"{quantity:g} {base} (~${quantity * price:,.2f})"
    return f"{quantity:g} {base}"


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        s = get_settings()
        self._token = token or s.telegram_bot_token
        self._chat_id = chat_id or s.telegram_chat_id

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str, *, urgent: bool = False) -> None:
        if not self.enabled:
            log.info("Telegram disabled, would have sent: %s", text)
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_notification": not urgent,
        }
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(url, json=payload)
            if resp.status_code != 200:
                log.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.warning("Telegram send exception: %s", e)

    # ---- formatted helpers ----

    def limit_order_stale(self, strategy_id: str, exchange: str, symbol: str, side: str,
                          limit_price: float, qty_target: float, qty_filled: float,
                          age_hours: float) -> None:
        """A resting limit ENTRY has been unfilled past the staleness threshold. Notify
        ONLY — the order is never auto-cancelled (D2). Re-pinged every N hours by the poller."""
        fill_note = (f"{qty_filled:g}/{qty_target:g} filled" if qty_filled else "unfilled")
        self.send(
            "⏳ *Limit entry still resting*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()} limit @ {limit_price:g} ({fill_note})\n"
            f"• Resting ~{age_hours:.0f}h, no close yet — NOT auto-cancelled.",
            urgent=False,
        )

    def order_failed(self, strategy_id: str, exchange: str, symbol: str,
                     side: str, quantity: float, attempts: int, error: str) -> None:
        self.send(
            "🚨 *Order FAILED* (will retry)\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()}: {_fmt_qty(quantity, symbol)}\n"
            f"• Attempts: {attempts}\n"
            f"• Error: `{error[:300]}`",
            urgent=True,
        )

    def order_dead(self, strategy_id: str, exchange: str, symbol: str,
                   side: str, quantity: float, attempts: int, error: str) -> None:
        self.send(
            "💀 *Order DEAD-LETTERED — manual intervention required*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()}: {_fmt_qty(quantity, symbol)}\n"
            f"• Attempts: {attempts}\n"
            f"• Last error: `{error[:300]}`",
            urgent=True,
        )

    def order_succeeded(self, strategy_id: str, exchange: str, symbol: str,
                        side: str, quantity: float, price: float, *,
                        realized: float = 0.0) -> None:
        # When the fill CLOSED/reduced a position it books a realized gain/loss; show it
        # (🟢/🔴). An open/increase books 0 -> no line.
        realized_line = ""
        if realized:
            realized_line = f"\n• Realized: {'🟢' if realized >= 0 else '🔴'} ${realized:,.2f}"
        self.send(
            "✅ *Order filled*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()}: {_fmt_qty(quantity, symbol, price)} @ ${price:,.2f}"
            f"{realized_line}",
        )

    def duplicate_alert(self, strategy_id: str, idempotency_key: str) -> None:
        self.send(
            "🛑 *Duplicate alert ignored*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Key: `{idempotency_key}`",
        )

    def unknown_strategy(self, strategy_id: str) -> None:
        self.send(
            "⚠️ *Unknown strategy_id received*\n"
            f"• `{strategy_id}` is not in `strategies.yaml` — alert logged but skipped.",
            urgent=True,
        )

    def disabled_strategy(self, strategy_id: str) -> None:
        self.send(
            "ℹ️ *Disabled strategy fired*\n"
            f"• `{strategy_id}` has no enabled venues — alert logged, skipped.",
        )

    def paper_trade(self, strategy_id: str, exchange: str, symbol: str,
                    side: str, quantity: float) -> None:
        self.send(
            "📝 *Paper trade — no position size configured*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()}: {_fmt_qty(quantity, symbol)} (min unit)\n"
            "• Set a position size in /admin/strategies to trade full size.",
            urgent=True,
        )

    def order_rejected(self, strategy_id: str, exchange: str, symbol: str,
                       side: str, reason: str) -> None:
        self.send(
            "⛔ *Order rejected (managed)*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()} blocked: `{reason}`",
            urgent=True,
        )

    # ---- funding-arb position-level helpers ----

    def arb_opened(self, arb_id: int, asset: str, qty: float,
                   legs: str) -> None:
        """Both legs filled and the pair is delta-neutral (`neutral=true`)."""
        self.send(
            "✅ *Arb opened* (neutral)\n"
            f"• Asset: *{asset}*  ·  Arb: `#{arb_id}`\n"
            f"• Qty: {qty:g} {asset} per leg\n"
            f"• Legs: {legs}",
        )

    def arb_error(self, arb_id: int, asset: str, reason: str,
                  skew: float | None = None) -> None:
        """A naked-or-skewed leg / finalize-error — leg risk, so URGENT."""
        skew_line = (
            f"\n• Neutrality skew: {skew:g} {asset}" if skew is not None else ""
        )
        self.send(
            "🚨 *Arb NOT NEUTRAL — manual intervention required*\n"
            f"• Asset: *{asset}*  ·  Arb: `#{arb_id}`\n"
            f"• Reason: `{reason[:300]}`"
            f"{skew_line}",
            urgent=True,
        )

    def arb_closed(self, arb_id: int, asset: str, *,
                   funding: float | None = None, commission: float | None = None,
                   realized: float | None = None, net: float | None = None) -> None:
        """The pair finished closing. When the P&L is available, lists the full
        breakdown — funding, fees, realized directional — and the net (the SAME net the
        /funding-arb dashboard shows: funding − fees + realized); otherwise a bare close
        notice."""
        breakdown = ""
        if net is not None:
            breakdown = (
                f"\n• Funding: ${funding:,.2f}"
                f"\n• Fees: −${commission:,.2f}"
                f"\n• Realized (directional): ${realized:,.2f}"
                f"\n• *Net* (funding − fees + realized): ${net:,.2f}"
            )
        self.send(
            "🅾️ *Arb closed*\n"
            f"• Asset: *{asset}*  ·  Arb: `#{arb_id}`"
            f"{breakdown}",
        )


_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
