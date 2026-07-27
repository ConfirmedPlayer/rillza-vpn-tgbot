import pytest

from app.core.settings import Settings

BASE_ENV = {
    'bot_token': 'test-bot-token',
    'database_url': 'postgresql+asyncpg://user:pass@localhost:5432/rillza',
    'panel_base_url': 'https://panel.example.com',
    'panel_api_key': 'ck_test',
}


@pytest.fixture
def make_settings():
    """Build Settings from explicit values, ignoring any real .env."""

    def _make(**overrides: object) -> Settings:
        return Settings(_env_file=None, **{**BASE_ENV, **overrides})

    return _make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()
