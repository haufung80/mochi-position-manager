from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    webhook_secret: str = Field(..., min_length=8)
    database_url: str = "sqlite:///./data/middleware.db"
    log_level: str = "INFO"
    dry_run: bool = False

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

    hyperliquid_private_key: str = ""
    hyperliquid_account_address: str = ""
    hyperliquid_vault_address: str = ""   # if set, orders trade on behalf of this vault
    hyperliquid_testnet: bool = False

    # --- Funding-arbitrage execution (dedicated, separately-credentialed books) ---
    # Optional so directional-only deploys still boot; when unset the /funding-arb
    # router answers 503. The arb book on each venue is a DISTINCT account (own
    # keys), so arb fills never net against the directional positions.
    funding_arb_secret: str = ""   # X-Arb-Secret API-key header; "" => arb API 503s
    bybit_arb_api_key: str = ""
    bybit_arb_api_secret: str = ""
    hyperliquid_arb_private_key: str = ""
    hyperliquid_arb_account_address: str = ""

    retry_max_attempts: int = 4
    retry_base_delay_sec: int = 2
    retry_max_delay_sec: int = 60

    strategies_file: str = "strategies.yaml"

    # IANA tz name used to render timestamps on the dashboard. Storage stays UTC;
    # this only affects display. e.g. "America/Toronto", "UTC", "Asia/Singapore".
    display_timezone: str = "America/Toronto"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def account_credentials(self, name: str, account: str = "default") -> dict:
        """Resolve the credential bucket for one ``(exchange, account)`` pair.

        ``account="default"`` returns the directional creds (every existing call
        site asks for this implicitly via the one-arg ``registry.get(name)``).
        ``account="arb"`` returns the dedicated funding-arb sub-account creds, so
        arb fills never net against the directional book. The registry passes the
        returned dict straight into the adapter constructor.

        Generalizes by ``account`` label (no literal-``"arb"`` branch buried in the
        registry). Raises ``ValueError`` for an unknown exchange, an unknown
        account label, or a known account whose credentials are empty (so a
        mis-set arb deploy fails loudly at construction, not mid-order).
        """
        ex = name.lower()
        acct = (account or "default").lower()

        if ex == "bybit":
            buckets = {
                "default": {
                    "api_key": self.bybit_api_key,
                    "api_secret": self.bybit_api_secret,
                },
                "arb": {
                    "api_key": self.bybit_arb_api_key,
                    "api_secret": self.bybit_arb_api_secret,
                },
            }
            shared = {"testnet": self.bybit_testnet, "dry_run": self.dry_run}
            required = ("api_key", "api_secret")
        elif ex == "hyperliquid":
            buckets = {
                "default": {
                    "private_key": self.hyperliquid_private_key,
                    "account_address": self.hyperliquid_account_address,
                    "vault_address": self.hyperliquid_vault_address,
                },
                "arb": {
                    "private_key": self.hyperliquid_arb_private_key,
                    "account_address": self.hyperliquid_arb_account_address,
                    # The arb book is a plain separate account, never a vault.
                    "vault_address": "",
                },
            }
            shared = {"testnet": self.hyperliquid_testnet, "dry_run": self.dry_run}
            required = ("private_key",)
        else:
            raise ValueError(f"Unknown exchange: {name}")

        if acct not in buckets:
            raise ValueError(f"Unknown account '{account}' for exchange '{name}'")

        creds = buckets[acct]
        # In DRY_RUN the directional adapters intentionally boot with empty creds
        # (offline/tests). A NON-default account is opt-in, though, so an empty
        # arb bucket is always a misconfiguration — fail loudly.
        if acct != "default" and not all(creds.get(k) for k in required):
            missing = ", ".join(k for k in required if not creds.get(k))
            raise ValueError(
                f"Missing credentials for {name}:{account} (empty: {missing}). "
                f"Set the {ex.upper()}_ARB_* env vars or omit the arb account."
            )
        return {**creds, **shared}


@lru_cache
def get_settings() -> Settings:
    return Settings()
