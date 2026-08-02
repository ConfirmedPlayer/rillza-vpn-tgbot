import pytest
from pydantic import ValidationError

from tests.conftest import BASE_ENV


def test_required_fields_are_enforced(monkeypatch) -> None:
    """CI exports a full config, so the environment must be cleared first —
    otherwise this passed locally and failed on every clean CI run."""
    from app.core.settings import Settings

    for key in BASE_ENV:
        monkeypatch.delenv(key.upper(), raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_secrets_are_not_leaked_in_repr(settings) -> None:
    assert 'test-bot-token' not in repr(settings)
    assert settings.bot_token.get_secret_value() == 'test-bot-token'


@pytest.mark.parametrize(
    'raw, expected',
    [('1,2,3', [1, 2, 3]), (' 1 , 2 ', [1, 2]), ('[4,5]', [4, 5]), ('7', [7])],
)
def test_admin_ids_accept_csv_and_json(make_settings, raw, expected) -> None:
    assert make_settings(admin_ids=raw).admin_ids == expected


@pytest.mark.parametrize(
    'raw, expected',
    [('1,2,3', [1, 2, 3]), (' 1 , 2 ', [1, 2]), ('[4,5]', [4, 5]), ('', [])],
)
def test_admin_ids_parsed_from_environment(monkeypatch, raw, expected) -> None:
    """Regression: env values are the path that actually broke.

    pydantic-settings JSON-decodes complex types inside the settings
    source, before validators run, so ``ADMIN_IDS=1,2`` used to raise a
    SettingsError at startup while direct kwargs worked fine.
    """
    from app.core.settings import Settings

    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv('ADMIN_IDS', raw)

    assert Settings(_env_file=None).admin_ids == expected  # type: ignore[call-arg]


def test_admin_ids_default_to_empty(settings) -> None:
    assert settings.admin_ids == []
    assert settings.is_admin(1) is False


def test_is_admin(make_settings) -> None:
    settings = make_settings(admin_ids='42,43')
    assert settings.is_admin(42) is True
    assert settings.is_admin(44) is False


def test_panel_base_url_trailing_slash_is_stripped(make_settings) -> None:
    settings = make_settings(panel_base_url='https://panel.example.com/')
    assert settings.panel_base_url == 'https://panel.example.com'


def test_subscription_url_is_built_from_panel_base(make_settings) -> None:
    settings = make_settings(panel_base_url='https://panel.example.com/')
    assert settings.subscription_url('abc123') == (
        'https://panel.example.com/api/files/abc123'
    )


def test_telegram_logging_disabled_without_credentials(settings) -> None:
    assert settings.telegram_logging_enabled is False


def test_telegram_logging_enabled_with_credentials(make_settings) -> None:
    settings = make_settings(log_bot_token='token', log_chat_id=-100123)
    assert settings.telegram_logging_enabled is True


def test_product_defaults(settings) -> None:
    assert settings.trial_days == 3
    assert settings.invoice_ttl_minutes == 30
    assert settings.panel_group_name == 'Celerity Primary Access'
    assert settings.yoomoney_payment_type == 'AC'


def test_a_password_with_a_percent_survives_alembic_config() -> None:
    """alembic's Config is a ConfigParser, which interpolates '%'.

    docs/SETUP.md tells the operator to generate the Postgres password
    themselves, so one will contain '%' sooner or later. Unescaped, it
    makes `alembic upgrade head` raise inside docker-entrypoint.sh and
    the container crash-loops before the bot ever polls.
    """
    from alembic.config import Config

    url = 'postgresql+asyncpg://rillza:pa%ss@postgres:5432/rillza'
    config = Config()

    config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))

    assert config.get_main_option('sqlalchemy.url') == url


def test_the_percent_really_does_break_it_unescaped() -> None:
    """Otherwise the test above would prove nothing."""
    from alembic.config import Config

    config = Config()
    with pytest.raises(ValueError, match='interpolation'):
        config.set_main_option(
            'sqlalchemy.url', 'postgresql+asyncpg://u:pa%ss@db:5432/x'
        )
