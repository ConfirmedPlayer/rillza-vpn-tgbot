"""Regression: blank optional vars must mean "disabled", not "empty".

`.env.example` ships optional variables as bare `NAME=` lines and the
README tells the operator to fill in only four of them. Without
`env_ignore_empty` those blanks load as `''` — which is not None — so
Telegram logging looked configured and the bot crash-looped on
`Bot(token='')` before it ever polled.
"""

from pathlib import Path

import pytest

from app.core.settings import Settings
from app.main import build_log_bot
from tests.conftest import BASE_ENV

BLANK_OPTIONALS = (
    'LOG_BOT_TOKEN',
    'LOG_CHAT_ID',
    'YOOMONEY_ACCESS_TOKEN',
    'YOOMONEY_WALLET',
    'CRYPTOBOT_TOKEN',
    'ADMIN_IDS',
)


@pytest.fixture
def blank_env(monkeypatch) -> None:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key.upper(), value)
    for key in BLANK_OPTIONALS:
        monkeypatch.setenv(key, '')


def test_blank_optionals_from_environment_are_unset(blank_env) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.log_bot_token is None
    assert settings.log_chat_id is None
    assert settings.yoomoney_access_token is None
    assert settings.yoomoney_wallet is None
    assert settings.cryptobot_token is None
    assert settings.admin_ids == []


def test_telegram_logging_disabled_by_blank_credentials(blank_env) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.telegram_logging_enabled is False
    assert build_log_bot(settings) is None


def test_blank_values_do_not_override_defaults(monkeypatch) -> None:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv('REDIS_URL', '')
    monkeypatch.setenv('PANEL_GROUP_NAME', '')

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.redis_url == 'redis://redis:6379/0'
    assert settings.panel_group_name == 'Rillza'


def test_shipped_env_example_starts_the_bot(tmp_path, monkeypatch) -> None:
    """The documented quick start must produce a runnable config."""
    for key in (*BLANK_OPTIONALS, *(k.upper() for k in BASE_ENV)):
        monkeypatch.delenv(key, raising=False)

    example = Path(__file__).resolve().parents[1] / '.env.example'
    filled = (
        example.read_text(encoding='utf-8')
        .replace(
            'BOT_TOKEN=123456:replace-me', f'BOT_TOKEN={BASE_ENV["bot_token"]}'
        )
        .replace('PANEL_API_KEY=ck_replace-me', 'PANEL_API_KEY=ck_real')
    )
    env_file = tmp_path / '.env'
    env_file.write_text(filled, encoding='utf-8')

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.bot_token.get_secret_value() == BASE_ENV['bot_token']
    assert settings.telegram_logging_enabled is False
    assert build_log_bot(settings) is None


@pytest.mark.parametrize(
    'overrides',
    [
        {'log_bot_token': '', 'log_chat_id': -100123},
        {'log_bot_token': 'token', 'log_chat_id': ''},
        {'log_bot_token': 'token', 'log_chat_id': '   '},
        {'log_bot_token': '', 'log_chat_id': ''},
    ],
)
def test_half_filled_logging_config_stays_disabled(
    make_settings, overrides
) -> None:
    """Direct kwargs bypass env_ignore_empty, so the property guards too."""
    assert make_settings(**overrides).telegram_logging_enabled is False
