"""FSM storage: abandoned dialogs must not accumulate forever."""

from datetime import timedelta

from app.core.settings import Settings
from app.main import build_storage
from tests.conftest import BASE_ENV


def _settings() -> Settings:
    return Settings(_env_file=None, **BASE_ENV)  # type: ignore[arg-type]


def test_fsm_keys_expire() -> None:
    """Without a TTL every abandoned dialog leaves a Redis key behind.

    Nothing deletes them: a user who taps «Поддержка» and never writes,
    an admin who opens the price prompt and walks away — each leaves a
    key that outlives the reason it existed. The volume is small and the
    growth is permanent, which is the combination that goes unnoticed
    until a restart is the only thing that reclaims the memory.
    """
    storage = build_storage(_settings())

    assert isinstance(storage.state_ttl, timedelta)
    assert storage.state_ttl > timedelta(0)
    assert storage.data_ttl == storage.state_ttl
