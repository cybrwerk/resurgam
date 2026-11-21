"""Unit tests for pure helpers in `filerestore/browse.py`."""

from datetime import UTC, datetime, timedelta, timezone

from resurgam_api.filerestore.browse import repository_token_and_keyfile, snapshot_identifier
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot


def _group(**kwargs) -> BackupGroup:
    defaults = {"datastore_id": "ds-1", "backup_type": "ct", "backup_id": "100"}
    defaults.update(kwargs)
    return BackupGroup(**defaults)


def test_snapshot_identifier_normalizes_non_utc_offset_to_utc():
    """Found live (ADR 0016): a freshly `apt`-installed PostgreSQL inherits
    the host's local timezone (unlike this project's docker-compose
    Postgres image, which defaults to UTC) -- reading a `timestamptz`
    column back then returns a non-UTC-offset datetime whose wall-clock
    numbers are already shifted. Naively `strftime`-ing that with a
    literal "Z" suffix silently mislabels a local time as UTC, producing
    a snapshot identifier PBS reports as nonexistent -- confirmed live
    against a real CT snapshot.
    """
    group = _group()
    # The real UTC instant is 2023-09-22T00:00:04Z; a Postgres session in
    # Europe/Zurich (CEST, UTC+2) would hand this back as 02:00:04+02:00.
    snapshot = BackupSnapshot(
        backup_group_id=group.id,
        snapshot_time=datetime(2023, 9, 22, 2, 0, 4, tzinfo=timezone(timedelta(hours=2))),
    )
    assert snapshot_identifier(group, snapshot) == "ct/100/2023-09-22T00:00:04Z"


def test_snapshot_identifier_already_utc():
    group = _group(backup_id="116", backup_type="vm")
    snapshot = BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime(2023, 9, 20, 0, 0, 3, tzinfo=UTC))
    assert snapshot_identifier(group, snapshot) == "vm/116/2023-09-20T00:00:03Z"


def _pbs_instance_and_datastore(db_session, test_settings, *, with_encryption_key: bool):
    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import SecretPurpose, SecretReference
    from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

    token_ref = SecretReference(
        reference_key="test-browse-token", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(token_ref)
    db_session.flush()
    LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key).store(
        token_ref.reference_key, "tokenid:tokensecret"
    )
    instance = PbsInstance(
        display_name="PBS", host="pbs.example", port=8007, tls_trust_mode="ca", api_token_secret_ref_id=token_ref.id
    )
    db_session.add(instance)
    db_session.flush()

    datastore = Datastore(pbs_instance_id=instance.id, name="store1")
    if with_encryption_key:
        key_ref = SecretReference(
            reference_key="test-browse-keyfile", description="test", purpose=SecretPurpose.PBS_ENCRYPTION_KEY
        )
        db_session.add(key_ref)
        db_session.flush()
        LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key).store(
            key_ref.reference_key, "keyfile-plaintext"
        )
        datastore.encryption_key_secret_ref_id = key_ref.id
    db_session.add(datastore)
    db_session.flush()
    return datastore


def test_repository_token_and_keyfile_returns_none_when_unconfigured(db_session, test_settings):
    datastore = _pbs_instance_and_datastore(db_session, test_settings, with_encryption_key=False)

    repository, token, keyfile = repository_token_and_keyfile(db_session, test_settings, datastore)

    assert repository == "tokenid@pbs.example:8007:store1"
    assert token == "tokensecret"
    assert keyfile is None


def test_repository_token_and_keyfile_resolves_configured_key(db_session, test_settings):
    datastore = _pbs_instance_and_datastore(db_session, test_settings, with_encryption_key=True)

    _repository, _token, keyfile = repository_token_and_keyfile(db_session, test_settings, datastore)

    assert keyfile == "keyfile-plaintext"


def test_pbs_fingerprint_only_for_pinned_instances(db_session, test_settings):
    from resurgam_api.filerestore.browse import pbs_fingerprint
    from resurgam_api.models.pbs import PbsInstance

    datastore = _pbs_instance_and_datastore(db_session, test_settings, with_encryption_key=False)
    assert pbs_fingerprint(db_session, datastore) is None  # `ca` trust mode
    instance = db_session.get(PbsInstance, datastore.pbs_instance_id)
    instance.tls_trust_mode = "fingerprint"
    instance.tls_fingerprint_sha256 = "f3a3560dfa8b48e7d570ce3c0510b4f2c2a5b91ac8030da02615a07c0452416b"
    db_session.flush()
    assert pbs_fingerprint(db_session, datastore) == instance.tls_fingerprint_sha256


def test_list_directory_passes_the_pinned_fingerprint_to_the_cli(db_session, test_settings, monkeypatch):
    from resurgam_api.filerestore import browse
    from resurgam_api.models.pbs import PbsInstance

    datastore = _pbs_instance_and_datastore(db_session, test_settings, with_encryption_key=False)
    instance = db_session.get(PbsInstance, datastore.pbs_instance_id)
    instance.tls_trust_mode = "fingerprint"
    instance.tls_fingerprint_sha256 = "ab" * 32
    db_session.flush()
    seen = {}
    monkeypatch.setattr(browse.catalog, "dump_catalog", lambda *a, **kw: seen.update(kw) or [])
    group = _group(backup_type="ct")
    snapshot = BackupSnapshot(snapshot_time=datetime(2026, 1, 1, tzinfo=UTC))
    browse.list_directory(db_session, test_settings, group, snapshot, datastore, "/")
    assert seen["fingerprint"] == "ab" * 32
