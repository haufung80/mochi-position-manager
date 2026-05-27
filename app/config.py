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

    retry_max_attempts: int = 4
    retry_base_delay_sec: int = 2
    retry_max_delay_sec: int = 60

    strategies_file: str = "strategies.yaml"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
