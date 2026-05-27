from __future__ import annotations
import logging
import httpx
from .config import get_settings

log = logging.getLogger(__name__)


class TelegramNotifier:
    """Fire-and-forget Telegram bot client. Failures are logged, not raised —
    the trading path must not be blocked by a flaky notification channel."""

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
                     side: str, qty_usd: float, attempts: int, error: str) -> None:
        self.send(
            "🚨 *Order FAILED* (will retry)\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• Side: {side}  Qty: ${qty_usd:.2f}\n"
            f"• Attempts: {attempts}\n"
            f"• Error: `{error[:300]}`",
            urgent=True,
        )

    def order_dead(self, strategy_id: str, exchange: str, symbol: str,
                   side: str, qty_usd: float, attempts: int, error: str) -> None:
        self.send(
            "💀 *Order DEAD-LETTERED — manual intervention required*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• Side: {side}  Qty: ${qty_usd:.2f}\n"
            f"• Attempts: {attempts}\n"
            f"• Last error: `{error[:300]}`",
            urgent=True,
        )

    def order_succeeded(self, strategy_id: str, exchange: str, symbol: str,
                        side: str, qty_usd: float, price: float) -> None:
        self.send(
            "✅ *Order filled*\n"
            f"• Strategy: `{strategy_id}`\n"
            f"• Exchange: *{exchange}* / {symbol}\n"
            f"• {side.upper()} ${qty_usd:.2f} @ {price}",
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
            f"• `{strategy_id}` is not in `strategies.yaml` — alert was logged but skipped.",
            urgent=True,
        )

    def disabled_strategy(self, strategy_id: str) -> None:
        self.send(
            "ℹ️ *Disabled strategy fired*\n"
            f"• `{strategy_id}` is configured but `enabled: false` — alert logged, skipped.",
        )


_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
