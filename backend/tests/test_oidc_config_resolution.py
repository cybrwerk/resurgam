"""Tests for `resolve_effective_oidc_config` (auth/oidc.py) -- the
precedence rule underpinning Administration -> Integrations (manifest
§6.3): a DB-backed `OidcSettings` row, when `enabled=True`, is
authoritative over `Settings.oidc_*` (env/`.env`); otherwise env is used
exactly as before this feature existed. This is the one piece of the
live login path with real, deliberate test coverage -- `get_oauth_client`
itself and the actual HTTP login/callback flow have no automated coverage
in this codebase (confirmed before building this), so precedence
correctness here is the real safety net.
"""

from resurgam_api.auth.oidc import resolve_effective_oidc_config


def test_falls_back_to_environment_when_no_row_exists(db_session, test_settings):
    config = resolve_effective_oidc_config(db_session, test_settings)
    assert config.source == "environment"
    assert config.issuer == test_settings.oidc_issuer
    assert config.client_id == test_settings.oidc_client_id
    assert config.client_secret == test_settings.oidc_client_secret
    assert config.redirect_uri == test_settings.oidc_redirect_uri


def test_falls_back_to_environment_when_row_exists_but_disabled(db_session, test_settings):
    from resurgam_api.models.oidc_settings import OidcSettings

    db_session.add(
        OidcSettings(
            issuer="https://db.example.com", client_id="db-client", redirect_uri="https://db/cb", enabled=False
        )
    )
    db_session.commit()

    config = resolve_effective_oidc_config(db_session, test_settings)
    assert config.source == "environment"
    assert config.issuer == test_settings.oidc_issuer


def test_uses_database_row_when_enabled(db_session, test_settings):
    from resurgam_api.models.oidc_settings import OidcSettings
    from resurgam_api.models.secrets import SecretPurpose, SecretReference
    from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

    secret_ref = SecretReference(
        reference_key="oidc-client-secret",
        description="OIDC client secret",
        purpose=SecretPurpose.OIDC_CLIENT_SECRET,
    )
    db_session.add(secret_ref)
    db_session.flush()
    provider = LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key)
    provider.store("oidc-client-secret", "db-stored-secret-value")

    db_session.add(
        OidcSettings(
            issuer="https://db.example.com",
            client_id="db-client",
            client_secret_ref_id=secret_ref.id,
            redirect_uri="https://db.example.com/callback",
            enabled=True,
        )
    )
    db_session.commit()

    config = resolve_effective_oidc_config(db_session, test_settings)
    assert config.source == "database"
    assert config.issuer == "https://db.example.com"
    assert config.client_id == "db-client"
    assert config.client_secret == "db-stored-secret-value"
    assert config.redirect_uri == "https://db.example.com/callback"


def test_database_row_with_no_secret_yet_resolves_to_empty_secret(db_session, test_settings):
    """An edge case that shouldn't normally occur through the API (which
    requires a secret on first save) but must resolve safely rather than
    crash if it ever does -- e.g. a row created directly, bypassing the
    API's own validation.
    """
    from resurgam_api.models.oidc_settings import OidcSettings

    db_session.add(
        OidcSettings(issuer="https://db.example.com", client_id="db-client", redirect_uri="https://db/cb", enabled=True)
    )
    db_session.commit()

    config = resolve_effective_oidc_config(db_session, test_settings)
    assert config.source == "database"
    assert config.client_secret == ""
