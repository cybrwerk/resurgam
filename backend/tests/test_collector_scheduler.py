from datetime import UTC, datetime, timedelta

from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.worker.collector_scheduler import _due_instances

_ENCRYPTION_KEY = "PIjcEQaYJrbfZSCc5nPrYO-qID45lH3qxeKsKt_ln44="


def _make_pbs_instance(db_session, *, disabled: bool = False, last_collector_sync_at=None) -> PbsInstance:
    ref = SecretReference(
        reference_key=f"test-scheduler-token-{db_session.query(SecretReference).count()}",
        description="test",
        purpose=SecretPurpose.INFRA_CREDENTIAL,
    )
    db_session.add(ref)
    db_session.flush()

    instance = PbsInstance(
        display_name="PBS",
        host="pbs.internal.example",
        port=8007,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
        disabled=disabled,
        last_collector_sync_at=last_collector_sync_at,
    )
    db_session.add(instance)
    db_session.flush()
    return instance


def test_due_instances_includes_never_synced(db_session):
    instance = _make_pbs_instance(db_session, last_collector_sync_at=None)
    assert [i.id for i in _due_instances(db_session, interval_minutes=15)] == [instance.id]


def test_due_instances_excludes_recently_synced(db_session):
    _make_pbs_instance(db_session, last_collector_sync_at=datetime.now(UTC) - timedelta(minutes=1))
    assert _due_instances(db_session, interval_minutes=15) == []


def test_due_instances_includes_stale_sync(db_session):
    instance = _make_pbs_instance(db_session, last_collector_sync_at=datetime.now(UTC) - timedelta(minutes=20))
    assert [i.id for i in _due_instances(db_session, interval_minutes=15)] == [instance.id]


def test_due_instances_excludes_disabled(db_session):
    _make_pbs_instance(db_session, disabled=True, last_collector_sync_at=None)
    assert _due_instances(db_session, interval_minutes=15) == []


def test_due_instances_respects_a_shorter_configured_interval(db_session):
    instance = _make_pbs_instance(db_session, last_collector_sync_at=datetime.now(UTC) - timedelta(minutes=5))
    assert _due_instances(db_session, interval_minutes=10) == []
    assert [i.id for i in _due_instances(db_session, interval_minutes=1)] == [instance.id]
