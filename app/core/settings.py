"""Application configuration.

Settings are read once via :func:`get_settings` and passed down explicitly.
Nothing is instantiated at import time, so importing any module never
requires a populated environment (tests build ``Settings`` directly).
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / '.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    # --- Telegram -------------------------------------------------------
    bot_token: SecretStr
    # NoDecode: without it pydantic-settings JSON-decodes the raw env value
    # before validation and "1,2" blows up inside the settings source.
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Storage --------------------------------------------------------
    database_url: str
    redis_url: str = 'redis://redis:6379/0'

    # --- CELERITY panel -------------------------------------------------
    panel_base_url: str
    panel_api_key: SecretStr
    panel_group_name: str = 'Rillza'

    # --- Product --------------------------------------------------------
    trial_days: int = 3
    invoice_ttl_minutes: int = 30

    # --- Payments -------------------------------------------------------
    yoomoney_access_token: SecretStr | None = None
    yoomoney_wallet: str | None = None
    yoomoney_payment_type: Literal['AC', 'PC'] = 'AC'
    cryptobot_token: SecretStr | None = None

    # --- Happ download links (shown in the connection guide) ------------
    happ_ios_url: str = 'https://apps.apple.com/app/id6504287215'
    happ_android_url: str = (
        'https://play.google.com/store/apps/details?id=com.happproxy'
    )
    happ_site_url: str = 'https://happ.su'

    # --- Logging --------------------------------------------------------
    log_level: str = 'INFO'
    log_bot_token: SecretStr | None = None
    log_chat_id: int | str | None = None
    log_telegram_level: str = 'WARNING'

    @field_validator('admin_ids', mode='before')
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        """Accept both ``[1,2]`` and ``1,2`` from any source.

        pydantic-settings JSON-decodes complex types coming from the
        environment, but a value passed directly to ``Settings(...)``
        arrives as a raw string — so both shapes are handled here.
        """
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith('['):
            return json.loads(raw)
        return [part.strip() for part in raw.split(',') if part.strip()]

    @field_validator('panel_base_url', mode='after')
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip('/')

    @property
    def telegram_logging_enabled(self) -> bool:
        return self.log_bot_token is not None and self.log_chat_id is not None

    def subscription_url(self, token: str) -> str:
        """Public subscription link handed to the user's Happ app."""
        return f'{self.panel_base_url}/api/files/{token}'

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
