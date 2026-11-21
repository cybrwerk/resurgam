"""Automatic datastore discovery (`collector.sync.discover_datastores`) and
stale sync-job cleanup -- PBS HTTP is always mocked at the connectivity
function level, never called for real."""

from resurgam_api.collector import sync as collector_sync_module
from resurgam_api.collector.sync import discover_datastores, sync_instance_sync_jobs, sync_pbs_instance
from resurgam_api.connectivity.models import (
    AclListResult,
    DatastoreListResult,
    DatastoreUsageListResult,
    RemoteDatastore,
    RemoteSyncJob,
    SnapshotListResult,
    SyncJobListResult,
    TaskListResult,
)
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.sync import SyncJob
from resurgam_api.models.tenancy import Customer
from tests.test_collector_sync import _FakeSettings, _make_datastore, _make_pbs_instance


def _mock_listing(monkeypatch, *datastores: RemoteDatastore) -> list[int]:
    calls: list[int] = []

    def fake(*a, **kw):
        calls.append(1)
        return DatastoreListResult(reachable=True, datastores=list(datastores))

    monkeypatch.setattr(collector_sync_module, "list_datastores", fake)
    return calls


def _mock_rest_of_sync_unreachable(monkeypatch) -> list[str]:
    snapshot_calls: list[str] = []
    monkeypatch.setattr(collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=False))
    monkeypatch.setattr(collector_sync_module, "list_sync_jobs", lambda *a, **kw: SyncJobListResult(reachable=False))
    monkeypatch.setattr(collector_sync_module, "list_acl_entries", lambda *a, **kw: AclListResult(reachable=False))
    monkeypatch.setattr(
        collector_sync_module, "list_datastore_usage", lambda *a, **kw: DatastoreUsageListResult(reachable=False)
    )

    def fake_snapshots(host, port, trust, token, datastore, timeout=5.0):
        snapshot_calls.append(datastore)
        return SnapshotListResult(reachable=True, snapshots=[])

    monkeypatch.setattr(collector_sync_module, "list_snapshots", fake_snapshots)
    return snapshot_calls


def test_discovery_imports_new_datastores_with_manual_import_fields(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    _mock_listing(
        monkeypatch,
        RemoteDatastore(name="store-a", path="/mnt/a", comment="primary"),
        RemoteDatastore(name="store-b"),
    )

    result = discover_datastores(db_session, _FakeSettings(), instance)

    assert result.reachable is True
    assert sorted(result.created) == ["store-a", "store-b"]
    assert result.updated == result.removed == result.restored == []
    store_a = db_session.query(Datastore).filter_by(pbs_instance_id=instance.id, name="store-a").one()
    assert (store_a.path, store_a.comment, store_a.removed_from_pbs_at) == ("/mnt/a", "primary", None)
    # Starts unassigned -- customer assignment stays a human decision.
    assert db_session.query(DatastoreCustomerAssignment).filter_by(datastore_id=store_a.id).count() == 0
    audit = db_session.query(AuditEvent).filter_by(action="datastore_discovered", target_id=str(store_a.id)).one()
    assert audit.actor_user_id is None
    assert audit.actor_label == "Collector (automatic)"


def test_discovery_is_idempotent(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    _mock_listing(monkeypatch, RemoteDatastore(name="store-a", path="/mnt/a"))

    discover_datastores(db_session, _FakeSettings(), instance)
    second = discover_datastores(db_session, _FakeSettings(), instance)

    assert second.created == second.updated == second.removed == second.restored == []
    assert db_session.query(Datastore).filter_by(pbs_instance_id=instance.id).count() == 1


def test_discovery_refreshes_path_and_comment(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance, name="store-a")
    datastore.path, datastore.comment = "/old", "old comment"
    _mock_listing(monkeypatch, RemoteDatastore(name="store-a", path="/new", comment=None))

    result = discover_datastores(db_session, _FakeSettings(), instance)

    assert result.updated == ["store-a"]
    assert (datastore.path, datastore.comment) == ("/new", None)


def test_discovery_marks_vanished_datastore_without_deleting_it(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    gone = _make_datastore(db_session, instance, name="gone")
    customer = Customer(name="Acme Discovery", slug="acme-discovery", debitor_number="D-DISC-1", match_code="ADZ")
    db_session.add(customer)
    db_session.flush()
    db_session.add(DatastoreCustomerAssignment(datastore_id=gone.id, customer_id=customer.id))
    db_session.flush()
    _mock_listing(monkeypatch, RemoteDatastore(name="other"))

    result = discover_datastores(db_session, _FakeSettings(), instance)

    assert result.removed == ["gone"]
    db_session.refresh(gone)
    assert gone.removed_from_pbs_at is not None
    assert db_session.query(DatastoreCustomerAssignment).filter_by(datastore_id=gone.id).count() == 1
    assert db_session.query(AuditEvent).filter_by(action="datastore_removed_from_pbs", target_id=str(gone.id)).count()

    # A second pass doesn't re-mark (timestamp = when it was first noticed).
    first_mark = gone.removed_from_pbs_at
    again = discover_datastores(db_session, _FakeSettings(), instance)
    assert again.removed == []
    assert gone.removed_from_pbs_at == first_mark


def test_discovery_clears_mark_when_datastore_reappears(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance, name="store-a")
    _mock_listing(monkeypatch)
    discover_datastores(db_session, _FakeSettings(), instance)
    assert datastore.removed_from_pbs_at is not None

    _mock_listing(monkeypatch, RemoteDatastore(name="store-a"))
    result = discover_datastores(db_session, _FakeSettings(), instance)

    assert result.restored == ["store-a"]
    assert result.created == []
    assert datastore.removed_from_pbs_at is None
    assert db_session.query(Datastore).filter_by(pbs_instance_id=instance.id).count() == 1


def test_discovery_unreachable_touches_nothing(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance, name="store-a")
    monkeypatch.setattr(
        collector_sync_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=False, error="connection refused"),
    )

    result = discover_datastores(db_session, _FakeSettings(), instance)

    assert result.reachable is False
    assert result.error == "connection refused"
    assert datastore.removed_from_pbs_at is None


def test_discovery_only_touches_this_instances_datastores(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    other_instance = _make_pbs_instance_with_key(db_session, "other-instance-token")
    other_store = _make_datastore(db_session, other_instance, name="store-a")
    _mock_listing(monkeypatch)

    discover_datastores(db_session, _FakeSettings(), instance)

    assert other_store.removed_from_pbs_at is None


def _make_pbs_instance_with_key(db_session, key: str):
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(reference_key=key, description="test", purpose=SecretPurpose.INFRA_CREDENTIAL)
    db_session.add(ref)
    db_session.flush()
    instance = PbsInstance(
        display_name="PBS 2",
        host="pbs2.internal.example",
        port=8007,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
    )
    db_session.add(instance)
    db_session.flush()
    return instance


def test_sync_pbs_instance_discovers_then_syncs_only_present_datastores(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    _make_datastore(db_session, instance, name="gone")
    _mock_listing(monkeypatch, RemoteDatastore(name="fresh", path="/mnt/fresh"))
    snapshot_calls = _mock_rest_of_sync_unreachable(monkeypatch)

    result = sync_pbs_instance(db_session, _FakeSettings(), instance)

    assert result.discovery is not None
    assert result.discovery.created == ["fresh"]
    assert result.discovery.removed == ["gone"]
    # The newly discovered datastore is inventoried in the same pass; the
    # removed one is skipped (PBS would only answer "not found").
    assert snapshot_calls == ["fresh"]
    assert [o.datastore_name for o in result.datastores] == ["fresh"]


def test_sync_pbs_instance_continues_when_discovery_raises(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    _make_datastore(db_session, instance, name="store-a")

    def boom(*a, **kw):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(collector_sync_module, "list_datastores", boom)
    snapshot_calls = _mock_rest_of_sync_unreachable(monkeypatch)

    result = sync_pbs_instance(db_session, _FakeSettings(), instance)

    assert result.discovery.reachable is False
    assert "discovery exploded" in result.discovery.error
    assert snapshot_calls == ["store-a"]


def test_sync_jobs_removed_on_pbs_are_deleted(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    jobs = [
        RemoteSyncJob(job_id="keep", remote_name="dr", remote_store="main", local_store="s1"),
        RemoteSyncJob(job_id="drop", remote_name="dr", remote_store="main", local_store="s1"),
    ]
    monkeypatch.setattr(
        collector_sync_module, "list_sync_jobs", lambda *a, **kw: SyncJobListResult(reachable=True, sync_jobs=jobs)
    )
    sync_instance_sync_jobs(db_session, _FakeSettings(), instance)

    monkeypatch.setattr(
        collector_sync_module,
        "list_sync_jobs",
        lambda *a, **kw: SyncJobListResult(reachable=True, sync_jobs=[jobs[0]]),
    )
    result = sync_instance_sync_jobs(db_session, _FakeSettings(), instance)

    assert result.sync_jobs_removed == 1
    assert [j.job_id for j in db_session.query(SyncJob).filter_by(pbs_instance_id=instance.id).all()] == ["keep"]


def test_sync_jobs_untouched_when_pbs_unreachable(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    job = RemoteSyncJob(job_id="keep", remote_name="dr", remote_store="main", local_store="s1")
    monkeypatch.setattr(
        collector_sync_module, "list_sync_jobs", lambda *a, **kw: SyncJobListResult(reachable=True, sync_jobs=[job])
    )
    sync_instance_sync_jobs(db_session, _FakeSettings(), instance)

    monkeypatch.setattr(
        collector_sync_module, "list_sync_jobs", lambda *a, **kw: SyncJobListResult(reachable=False, error="down")
    )
    result = sync_instance_sync_jobs(db_session, _FakeSettings(), instance)

    assert result.reachable is False
    assert db_session.query(SyncJob).filter_by(pbs_instance_id=instance.id).count() == 1
