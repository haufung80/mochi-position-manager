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
                        side: str, quantity: float, price: float) -> None:
        self.send(
            "✅ *Order filled*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()}: {_fmt_qty(quantity, symbol, price)} @ ${price:,.2f}",
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


_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
