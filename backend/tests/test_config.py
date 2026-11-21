import os

import pytest

from resurgam_api.config import Settings

_REAL_SESSION_SECRET = "a-real-session-secret-at-least-32-bytes-long"
_REAL_ENCRYPTION_KEY = "PIjcEQaYJrbfZSCc5nPrYO-qID45lH3qxeKsKt_ln44="
_REAL_DATABASE_URL = "postgresql+psycopg://resurgam:a-real-password@db:5432/resurgam"


@pytest.fixture(autouse=True)
def _no_settings_from_the_environment(monkeypatch):
    """`_env_file=None` only skips `.env`; real environment variables still
    apply. CI sets `DEV_MODE=true` for the rest of the suite, which would
    otherwise turn every "outside dev mode" case below into dev mode."""
    fields = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key.upper() in fields:
            monkeypatch.delenv(key)


def _real_settings(**overrides) -> dict:
    defaults = {
        "database_url": _REAL_DATABASE_URL,
        "session_secret": _REAL_SESSION_SECRET,
        "secrets_local_encryption_key": _REAL_ENCRYPTION_KEY,
    }
    defaults.update(overrides)
    return defaults


def test_dev_mode_allows_the_zero_config_defaults():
    settings = Settings(dev_mode=True, _env_file=None)
    assert settings.session_secret == ""
    assert settings.secrets_local_encryption_key == ""


def test_refuses_to_start_with_no_config_outside_dev_mode():
    with pytest.raises(ValueError, match="SESSION_SECRET is not set"):
        Settings(dev_mode=False, _env_file=None)


def test_refuses_empty_session_secret_outside_dev_mode():
    with pytest.raises(ValueError, match="SESSION_SECRET is not set"):
        Settings(**_real_settings(session_secret=""), _env_file=None)


def test_refuses_empty_encryption_key_outside_dev_mode():
    with pytest.raises(ValueError, match="SECRETS_LOCAL_ENCRYPTION_KEY is not set"):
        Settings(**_real_settings(secrets_local_encryption_key=""), _env_file=None)


def test_refuses_the_hardcoded_dev_database_url_outside_dev_mode():
    with pytest.raises(ValueError, match="DATABASE_URL is still the hardcoded local-dev default"):
        Settings(
            **_real_settings(database_url="postgresql+psycopg://resurgam:resurgam-dev-only@localhost:55432/resurgam"),
            _env_file=None,
        )


def test_refuses_breakglass_enabled_without_a_password_hash_outside_dev_mode():
    with pytest.raises(ValueError, match="BREAKGLASS_PASSWORD_HASH is not set"):
        Settings(
            **_real_settings(breakglass_enabled=True, breakglass_password_hash=""),
            _env_file=None,
        )


def test_accepts_real_config_outside_dev_mode():
    settings = Settings(**_real_settings(), _env_file=None)
    assert settings.dev_mode is False
    assert settings.session_secret == _REAL_SESSION_SECRET
