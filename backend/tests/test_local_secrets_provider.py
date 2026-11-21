import pytest

from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.secrets.key_derivation import derive_purpose_key
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from resurgam_api.secrets.provider import SecretNotFoundError

_TEST_KEY_A = "PIjcEQaYJrbfZSCc5nPrYO-qID45lH3qxeKsKt_ln44="
_TEST_KEY_B = "ZudiLmOpc4fGcr6Ug9DcorhUuJLrxpT_Tlmme-VKE14="


def _make_reference(db_session, key: str, purpose: str = SecretPurpose.INFRA_CREDENTIAL) -> SecretReference:
    ref = SecretReference(reference_key=key, description="test secret", purpose=purpose)
    db_session.add(ref)
    db_session.flush()
    return ref


def test_store_and_retrieve_round_trip(db_session):
    _make_reference(db_session, "test-ref-1")
    provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A)

    provider.store("test-ref-1", "super-secret-token")

    assert provider.retrieve("test-ref-1") == "super-secret-token"


def test_store_overwrites_existing_value(db_session):
    _make_reference(db_session, "test-ref-2")
    provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A)

    provider.store("test-ref-2", "first-value")
    provider.store("test-ref-2", "second-value")

    assert provider.retrieve("test-ref-2") == "second-value"


def test_retrieve_missing_reference_raises(db_session):
    provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A)

    with pytest.raises(SecretNotFoundError):
        provider.retrieve("does-not-exist")


def test_retrieve_with_wrong_key_raises(db_session):
    _make_reference(db_session, "test-ref-3")
    LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A).store("test-ref-3", "secret-value")

    wrong_key_provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_B)
    with pytest.raises(SecretNotFoundError):
        wrong_key_provider.retrieve("test-ref-3")


def test_delete_removes_value(db_session):
    _make_reference(db_session, "test-ref-4")
    provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A)
    provider.store("test-ref-4", "secret-value")

    provider.delete("test-ref-4")

    with pytest.raises(SecretNotFoundError):
        provider.retrieve("test-ref-4")


def test_missing_encryption_key_raises_at_construction(db_session):
    with pytest.raises(RuntimeError):
        LocalEncryptedSecretsProvider(db_session, "")


def test_retrieve_missing_secret_reference_raises(db_session):
    """No `SecretReference` row at all (not even without a value) --
    `retrieve` must still fail as SecretNotFoundError, not a raw
    AttributeError/`NoneType` error, when purpose lookup can't find one.
    """
    provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A)

    with pytest.raises(SecretNotFoundError):
        provider.retrieve("no-such-reference")


def test_different_purposes_encrypt_under_different_subkeys(db_session):
    """Credential-store review (2026-09-24): two references with the same
    master key but different `purpose` must produce ciphertext that
    can't cross-decrypt -- proves the purpose actually selects a
    different Fernet key, not just a label.
    """
    infra_ref = _make_reference(db_session, "infra-ref", purpose=SecretPurpose.MFA_TOTP)
    mfa_ref = _make_reference(db_session, "mfa-ref", purpose=SecretPurpose.MFA_TOTP)
    oidc_ref = _make_reference(db_session, "oidc-ref", purpose=SecretPurpose.OIDC_CLIENT_SECRET)

    provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A)
    provider.store(infra_ref.reference_key, "same-plaintext")
    provider.store(mfa_ref.reference_key, "same-plaintext")
    provider.store(oidc_ref.reference_key, "same-plaintext")

    from resurgam_api.models.secrets import LocalSecretValue

    infra_ciphertext = db_session.get(LocalSecretValue, infra_ref.reference_key).ciphertext
    mfa_ciphertext = db_session.get(LocalSecretValue, mfa_ref.reference_key).ciphertext
    oidc_ciphertext = db_session.get(LocalSecretValue, oidc_ref.reference_key).ciphertext

    # Same purpose -> same subkey -> still different ciphertext (Fernet's
    # own random IV/timestamp), but at least decryptable identically.
    assert infra_ciphertext != mfa_ciphertext
    assert provider.retrieve(infra_ref.reference_key) == provider.retrieve(mfa_ref.reference_key) == "same-plaintext"
    assert oidc_ciphertext not in (infra_ciphertext, mfa_ciphertext)


def test_retrieve_fails_if_reference_purpose_changes_after_store(db_session):
    """A confused-deputy simulation: if a `SecretReference` row's own
    `purpose` were ever changed after the value was stored under the old
    purpose's subkey, decryption must fail loudly (InvalidToken ->
    SecretNotFoundError), not silently succeed against the wrong subkey
    or silently return garbage.
    """
    ref = _make_reference(db_session, "purpose-drift", purpose=SecretPurpose.INFRA_CREDENTIAL)
    provider = LocalEncryptedSecretsProvider(db_session, _TEST_KEY_A)
    provider.store(ref.reference_key, "secret-value")

    ref.purpose = SecretPurpose.SMTP_PASSWORD
    db_session.flush()

    with pytest.raises(SecretNotFoundError):
        provider.retrieve(ref.reference_key)


def test_derive_purpose_key_is_deterministic_and_purpose_specific():
    key_a = derive_purpose_key(_TEST_KEY_A, SecretPurpose.INFRA_CREDENTIAL)
    key_a_again = derive_purpose_key(_TEST_KEY_A, SecretPurpose.INFRA_CREDENTIAL)
    key_b = derive_purpose_key(_TEST_KEY_A, SecretPurpose.MFA_TOTP)

    assert key_a == key_a_again
    assert key_a != key_b
    assert len(key_a) == 44  # urlsafe-base64 of 32 raw bytes, Fernet's own expected length
