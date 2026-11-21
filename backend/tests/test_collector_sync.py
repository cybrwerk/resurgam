import uuid
from datetime import UTC, datetime

from resurgam_api.collector import sync as collector_sync_module
from resurgam_api.collector.sync import (
    sync_datastore_inventory,
    sync_instance_acl,
    sync_instance_sync_jobs,
    sync_instance_tasks,
    sync_pbs_instance,
)
from resurgam_api.connectivity.models import (
    AclListResult,
    DatastoreUsageListResult,
    GroupNotesResult,
    GuestListResult,
    RemoteAclEntry,
    RemoteDatastoreUsage,
    RemoteGuest,
    RemoteSnapshot,
    RemoteSyncJob,
    RemoteTask,
    SnapshotListResult,
    SyncJobListResult,
    TaskListResult,
)
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, PbsAclEntry, TaskRun
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster, PveClusterDatastore
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.models.sync import SyncJob
from resurgam_api.models.usage import UsageMeasurement
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

_ENCRYPTION_KEY = "PIjcEQaYJrbfZSCc5nPrYO-qID45lH3qxeKsKt_ln44="


def _make_pbs_instance(db_session) -> PbsInstance:
    ref = SecretReference(
        reference_key="test-collector-token", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    LocalEncryptedSecretsProvider(db_session, _ENCRYPTION_KEY).store("test-collector-token", "test@pbs!token:secret")

    instance = PbsInstance(
        display_name="PBS",
        host="pbs.internal.example",
        port=8007,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
    )
    db_session.add(instance)
    db_session.flush()
    return instance


def _make_datastore(db_session, instance: PbsInstance, name="store1") -> Datastore:
    datastore = Datastore(pbs_instance_id=instance.id, name=name)
    db_session.add(datastore)
    db_session.flush()
    return datastore


class _FakeSettings:
    secrets_local_encryption_key = _ENCRYPTION_KEY
    connectivity_timeout_seconds = 5.0


def test_first_sync_creates_groups_and_snapshots(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", size=1024),
        RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok", size=2048),
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )

    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.reachable is True
    assert result.groups_created == 1
    assert result.snapshots_created == 2

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.backup_type == "vm"
    assert group.backup_id == "100"
    assert group.last_verify_status == "ok"
    assert group.last_backup_at == datetime.fromtimestamp(1_700_086_400, tz=UTC)
    assert db_session.query(BackupSnapshot).filter_by(backup_group_id=group.id).count() == 2


def test_sync_stores_owner_from_latest_snapshot(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(
            backup_type="ct", backup_id="100", backup_time=1_700_000_000, verify_state="ok", owner="old@pbs"
        ),
        RemoteSnapshot(
            backup_type="ct", backup_id="100", backup_time=1_700_086_400, verify_state="ok", owner="tenant-a@pbs"
        ),
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.owner == "tenant-a@pbs"

    # A second sync where the latest snapshot's owner changed must update it.
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(
            reachable=True,
            snapshots=[
                RemoteSnapshot(
                    backup_type="ct", backup_id="100", backup_time=1_700_172_800, verify_state="ok", owner="new@pbs"
                )
            ],
        ),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    db_session.refresh(group)
    assert group.owner == "new@pbs"


def test_second_sync_with_same_data_is_idempotent(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [RemoteSnapshot(backup_type="ct", backup_id="200", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )

    first = sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    second = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert first.groups_created == 1
    assert first.snapshots_created == 1
    assert second.groups_created == 0
    assert second.groups_updated == 1
    assert second.snapshots_created == 0
    assert second.snapshots_updated == 1

    assert db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).count() == 1
    assert db_session.query(BackupSnapshot).count() == 1


def test_new_snapshot_added_to_existing_group(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    first_batch = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=first_batch),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    second_batch = first_batch + [
        RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="failed")
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=second_batch),
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.groups_created == 0
    assert result.groups_updated == 1
    assert result.snapshots_created == 1
    assert result.snapshots_updated == 1  # the original snapshot re-synced

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.last_verify_status == "failed"  # reflects the latest snapshot, not the first


def test_group_missing_from_a_sync_is_marked_removed(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snaps = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snaps)
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.removed_from_pbs_at is None

    # A later sync no longer reports this backup_id at all -- deleted on PBS.
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=[])
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.groups_removed == 1
    db_session.refresh(group)
    assert group.removed_from_pbs_at is not None
    assert group.superseded_at is None  # distinct from a rename -- no successor generation opened


def test_group_reappearing_clears_removed_flag_without_opening_a_new_generation(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snaps = [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="web-01"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snaps)
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=[])
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.removed_from_pbs_at is not None
    original_group_id = group.id

    # Same identity tuple, same guest name -- PBS is reporting it again
    # (undeleted, or a fresh backup landed at the same slot).
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snaps)
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    live_groups = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id, superseded_at=None).all()
    assert len(live_groups) == 1
    assert live_groups[0].id == original_group_id  # same row reused, not a new generation
    assert live_groups[0].removed_from_pbs_at is None


def test_removal_detection_only_touches_this_datastores_groups(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore_a = _make_datastore(db_session, instance, name="store-a")
    datastore_b = _make_datastore(db_session, instance, name="store-b")

    snaps = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snaps)
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore_a)
    sync_datastore_inventory(db_session, _FakeSettings(), datastore_b)

    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=[])
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore_a)

    assert result.groups_removed == 1
    group_a = db_session.query(BackupGroup).filter_by(datastore_id=datastore_a.id).one()
    group_b = db_session.query(BackupGroup).filter_by(datastore_id=datastore_b.id).one()
    assert group_a.removed_from_pbs_at is not None
    assert group_b.removed_from_pbs_at is None  # untouched -- store-b was never synced this pass


def test_snapshot_comment_populates_resolved_name(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="egw")
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )

    def _fail_if_called(*a, **kw):
        raise AssertionError("get_group_notes should not be called when the snapshot already has a comment")

    monkeypatch.setattr(collector_sync_module, "get_group_notes", _fail_if_called)

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.resolved_name == "egw"


def test_snapshot_comment_takes_priority_over_group_notes(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(
            backup_type="vm",
            backup_id="100",
            backup_time=1_700_000_000,
            verify_state="ok",
            comment="from-snapshot-comment",
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    monkeypatch.setattr(
        collector_sync_module,
        "get_group_notes",
        lambda *a, **kw: GroupNotesResult(reachable=True, notes="from-group-notes"),
    )

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.resolved_name == "from-snapshot-comment"


def test_renamed_workload_splits_into_a_new_generation(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    first_batch = [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="old-name"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=first_batch),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    old_group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert old_group.resolved_name == "old-name"
    old_snapshot = db_session.query(BackupSnapshot).filter_by(backup_group_id=old_group.id).one()

    second_batch = first_batch + [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok", comment="renamed"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=second_batch),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    groups = (
        db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).order_by(BackupGroup.resolved_name).all()
    )
    assert len(groups) == 2

    db_session.refresh(old_group)
    assert old_group.resolved_name == "old-name"
    assert old_group.superseded_at is not None

    new_group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id, resolved_name="renamed").one()
    assert new_group.superseded_at is None
    assert new_group.id != old_group.id

    # The original snapshot is never reclassified onto the new generation.
    db_session.refresh(old_snapshot)
    assert old_snapshot.backup_group_id == old_group.id
    new_snapshot = db_session.query(BackupSnapshot).filter_by(backup_group_id=new_group.id).one()
    assert new_snapshot.snapshot_time.timestamp() == 1_700_086_400


def test_no_split_when_new_snapshots_comment_matches_established_name(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    first_batch = [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="web-01"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=first_batch),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    second_batch = first_batch + [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok", comment="web-01"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=second_batch),
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.groups_created == 0
    assert result.groups_updated == 1
    groups = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).all()
    assert len(groups) == 1
    assert groups[0].superseded_at is None


def test_first_ever_name_assignment_is_never_a_rename(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="web-01"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.groups_created == 1
    groups = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).all()
    assert len(groups) == 1
    assert groups[0].resolved_name == "web-01"
    assert groups[0].superseded_at is None


def test_split_leaves_old_generations_own_settings_untouched(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)
    from resurgam_api.models.inventory import ProtectionPolicy
    from resurgam_api.models.tenancy import Customer

    first_batch = [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="old-name"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=first_batch),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    old_group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()

    customer = Customer(name="Acme", slug="acme", debitor_number="D-1", match_code="ACM")
    db_session.add(customer)
    db_session.flush()
    policy = ProtectionPolicy(
        customer_id=customer.id,
        name="daily",
        expected_interval_hours=24,
        max_time_since_last_backup_hours=48,
    )
    db_session.add(policy)
    db_session.flush()
    old_group.protection_policy_id = policy.id
    old_group.file_index_enabled = True
    old_group.file_index_exclude_patterns = ["^/custom-only"]
    db_session.flush()

    second_batch = first_batch + [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok", comment="renamed"
        )
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=second_batch),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    db_session.refresh(old_group)
    assert old_group.protection_policy_id == policy.id
    assert old_group.file_index_enabled is True
    assert old_group.file_index_exclude_patterns == ["^/custom-only"]

    new_group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id, resolved_name="renamed").one()
    assert new_group.protection_policy_id is None
    assert new_group.customer_id is None
    assert new_group.file_index_enabled is False
    assert new_group.file_index_exclude_patterns != ["^/custom-only"]
    assert len(new_group.file_index_exclude_patterns) > 0  # still gets the normal defaults


def test_multiple_renames_within_one_sync_batch(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="name-a"
        ),
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok", comment="name-b"
        ),
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_172_800, verify_state="ok", comment="name-c"
        ),
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.groups_created == 3
    groups = (
        db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).order_by(BackupGroup.resolved_name).all()
    )
    assert [g.resolved_name for g in groups] == ["name-a", "name-b", "name-c"]
    assert [g.superseded_at is None for g in groups] == [False, False, True]


def test_resync_after_split_does_not_duplicate_or_reparent_snapshots(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", comment="old-name"
        ),
        RemoteSnapshot(
            backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok", comment="renamed"
        ),
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    old_group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id, resolved_name="old-name").one()
    new_group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id, resolved_name="renamed").one()

    # Resync with the exact same (unchanged) snapshot list.
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.groups_created == 0
    assert db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).count() == 2
    assert db_session.query(BackupSnapshot).filter_by(backup_group_id=old_group.id).count() == 1
    assert db_session.query(BackupSnapshot).filter_by(backup_group_id=new_group.id).count() == 1


def test_partial_unique_index_allows_multiple_superseded_rows_same_identity(db_session):
    from datetime import datetime as dt

    from sqlalchemy.exc import IntegrityError

    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    g1 = BackupGroup(
        datastore_id=datastore.id,
        namespace="",
        backup_type="vm",
        backup_id="999",
        resolved_name="old1",
        superseded_at=dt(2026, 1, 1, tzinfo=UTC),
    )
    g2 = BackupGroup(
        datastore_id=datastore.id,
        namespace="",
        backup_type="vm",
        backup_id="999",
        resolved_name="old2",
        superseded_at=dt(2026, 2, 1, tzinfo=UTC),
    )
    g3 = BackupGroup(
        datastore_id=datastore.id, namespace="", backup_type="vm", backup_id="999", resolved_name="current"
    )
    db_session.add_all([g1, g2, g3])
    db_session.flush()

    g4 = BackupGroup(datastore_id=datastore.id, namespace="", backup_type="vm", backup_id="999", resolved_name="dup")
    db_session.add(g4)
    try:
        db_session.flush()
        assert False, "expected IntegrityError for a second concurrent current row"
    except IntegrityError:
        db_session.rollback()


def test_group_notes_populate_resolved_name(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    monkeypatch.setattr(
        collector_sync_module,
        "get_group_notes",
        lambda *a, **kw: GroupNotesResult(reachable=True, notes="web-server-01"),
    )

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.resolved_name == "web-server-01"


def test_empty_group_notes_do_not_overwrite_resolved_name(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    monkeypatch.setattr(
        collector_sync_module, "get_group_notes", lambda *a, **kw: GroupNotesResult(reachable=True, notes="")
    )

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.resolved_name is None


def _make_pve_cluster(db_session, *, disabled=False) -> PveCluster:
    ref = SecretReference(
        reference_key=f"test-pve-token-{uuid.uuid4()}", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    LocalEncryptedSecretsProvider(db_session, _ENCRYPTION_KEY).store(ref.reference_key, "pve-token-secret")

    cluster = PveCluster(
        display_name="Sample PVE",
        host="pve.internal.example",
        port=8006,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
        disabled=disabled,
    )
    db_session.add(cluster)
    db_session.flush()
    return cluster


def test_empty_group_notes_fall_back_to_linked_pve_cluster_guest_name(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)
    cluster = _make_pve_cluster(db_session)
    db_session.add(PveClusterDatastore(pve_cluster_id=cluster.id, datastore_id=datastore.id))
    db_session.flush()

    snapshots = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    monkeypatch.setattr(
        collector_sync_module, "get_group_notes", lambda *a, **kw: GroupNotesResult(reachable=True, notes="")
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_guests",
        lambda *a, **kw: GuestListResult(
            reachable=True,
            guests=[RemoteGuest(vmid=100, node="pve1", guest_type="qemu", name="web-server-01")],
        ),
    )

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.resolved_name == "web-server-01"


def test_pbs_notes_take_priority_over_pve_fallback(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)
    cluster = _make_pve_cluster(db_session)
    db_session.add(PveClusterDatastore(pve_cluster_id=cluster.id, datastore_id=datastore.id))
    db_session.flush()

    snapshots = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    monkeypatch.setattr(
        collector_sync_module,
        "get_group_notes",
        lambda *a, **kw: GroupNotesResult(reachable=True, notes="from-pbs-notes"),
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_guests",
        lambda *a, **kw: GuestListResult(
            reachable=True,
            guests=[RemoteGuest(vmid=100, node="pve1", guest_type="qemu", name="from-pve-fallback")],
        ),
    )

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.resolved_name == "from-pbs-notes"


def test_no_pve_cluster_link_means_no_fallback_even_when_notes_empty(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)
    # A PVE cluster exists but is not linked to this datastore.
    _make_pve_cluster(db_session)

    snapshots = [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    monkeypatch.setattr(
        collector_sync_module, "get_group_notes", lambda *a, **kw: GroupNotesResult(reachable=True, notes="")
    )

    def _fail_if_called(*a, **kw):
        raise AssertionError("list_guests should not be called when no cluster is linked to this datastore")

    monkeypatch.setattr(collector_sync_module, "list_guests", _fail_if_called)

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    assert group.resolved_name is None


def test_usage_measurement_captured_when_remote_usage_given(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    snapshots = [
        RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok", size=1024),
        RemoteSnapshot(backup_type="ct", backup_id="200", backup_time=1_700_086_400, verify_state="ok", size=2048),
    ]
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    monkeypatch.setattr(
        collector_sync_module, "get_group_notes", lambda *a, **kw: GroupNotesResult(reachable=True, notes="")
    )
    gc_task = TaskRun(
        pbs_instance_id=instance.id,
        upid="UPID:pbs:0001:garbage_collection:store1:root@pam:",
        task_type="garbage_collection",
        worker_id="store1",
        status="ok",
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        ended_at=datetime(2026, 9, 1, 1, tzinfo=UTC),
    )
    db_session.add(gc_task)
    db_session.flush()

    remote_usage = RemoteDatastoreUsage(name="store1", used_bytes=500, available_bytes=1500, total_bytes=2000)
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore, remote_usage)

    assert result.usage_captured is True
    measurement = db_session.query(UsageMeasurement).filter_by(datastore_id=datastore.id).one()
    assert measurement.used_bytes == 500
    assert measurement.available_bytes == 1500
    assert measurement.total_bytes == 2000
    assert measurement.logical_bytes == 3072  # 1024 + 2048, the two snapshot sizes
    assert measurement.group_count_vm == 1
    assert measurement.group_count_ct == 1
    assert measurement.group_count_host == 0
    assert measurement.snapshot_count == 2
    assert measurement.last_successful_gc_at == datetime(2026, 9, 1, 1, tzinfo=UTC)


def test_no_usage_measurement_when_remote_usage_is_none(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=[])
    )

    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.usage_captured is False
    assert db_session.query(UsageMeasurement).count() == 0


def test_sync_pbs_instance_matches_usage_by_datastore_name(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    store_a = _make_datastore(db_session, instance, name="store-a")
    store_b = _make_datastore(db_session, instance, name="store-b")

    monkeypatch.setattr(collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=True, tasks=[]))
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=[])
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_datastore_usage",
        lambda *a, **kw: DatastoreUsageListResult(
            reachable=True,
            usage=[RemoteDatastoreUsage(name="store-a", used_bytes=111, available_bytes=1, total_bytes=112)],
        ),
    )

    result = sync_pbs_instance(db_session, _FakeSettings(), instance)

    outcomes = {o.datastore_name: o for o in result.datastores}
    assert outcomes["store-a"].result.usage_captured is True
    assert outcomes["store-b"].result.usage_captured is False  # not present in the usage listing

    measurement = db_session.query(UsageMeasurement).filter_by(datastore_id=store_a.id).one()
    assert measurement.used_bytes == 111
    assert db_session.query(UsageMeasurement).filter_by(datastore_id=store_b.id).count() == 0


def test_sync_pbs_instance_sets_last_collector_sync_at(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    assert instance.last_collector_sync_at is None

    monkeypatch.setattr(
        collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=False, error="down")
    )
    monkeypatch.setattr(
        collector_sync_module, "list_sync_jobs", lambda *a, **kw: SyncJobListResult(reachable=False, error="down")
    )
    monkeypatch.setattr(
        collector_sync_module, "list_acl_entries", lambda *a, **kw: AclListResult(reachable=False, error="down")
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_datastore_usage",
        lambda *a, **kw: DatastoreUsageListResult(reachable=False, error="down"),
    )

    before = datetime.now(UTC)
    sync_pbs_instance(db_session, _FakeSettings(), instance)
    after = datetime.now(UTC)

    # Set unconditionally -- marks that a sync was *attempted*, even
    # though every sub-sync above reported unreachable.
    assert instance.last_collector_sync_at is not None
    assert before <= instance.last_collector_sync_at <= after


def test_unreachable_datastore_reports_error(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=False, error="timeout")
    )

    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.reachable is False
    assert result.error == "timeout"
    assert db_session.query(BackupGroup).count() == 0


def test_sync_instance_tasks_creates_and_updates(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)

    running_task = RemoteTask(
        upid="UPID:pbs:0001:backup:100:root@pam:",
        task_type="backup",
        worker_id="100",
        status=None,
        start_time=1_700_000_000,
    )
    monkeypatch.setattr(
        collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=True, tasks=[running_task])
    )
    first = sync_instance_tasks(db_session, _FakeSettings(), instance)
    assert first.tasks_created == 1

    task = db_session.query(TaskRun).filter_by(pbs_instance_id=instance.id).one()
    assert task.status == "running"
    assert task.ended_at is None

    completed_task = RemoteTask(
        upid=running_task.upid,
        task_type="backup",
        worker_id="100",
        status="OK",
        start_time=1_700_000_000,
        end_time=1_700_000_300,
    )
    monkeypatch.setattr(
        collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=True, tasks=[completed_task])
    )
    second = sync_instance_tasks(db_session, _FakeSettings(), instance)
    assert second.tasks_created == 0
    assert second.tasks_updated == 1

    db_session.refresh(task)
    assert task.status == "ok"
    assert task.ended_at is not None


def test_sync_instance_tasks_maps_reader_worker_type_to_sync(db_session, monkeypatch):
    """A Sync Job's actual pull-from-remote task uses PBS's own "reader"
    worker type, not "sync"/"syncjob" -- confirmed live 2026-09-28
    against real data where every "reader" UPID was a Sync Job pull, and
    without this mapping they all fell through to UNKNOWN.
    """
    instance = _make_pbs_instance(db_session)
    reader_task = RemoteTask(
        upid="UPID:pbs:0001:reader:backup-store:root@pam:",
        task_type="reader",
        worker_id="backup-store",
        status="OK",
        start_time=1_700_000_000,
        end_time=1_700_000_300,
    )
    monkeypatch.setattr(
        collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=True, tasks=[reader_task])
    )
    sync_instance_tasks(db_session, _FakeSettings(), instance)

    task = db_session.query(TaskRun).filter_by(pbs_instance_id=instance.id).one()
    assert task.task_type == "sync"


def test_sync_instance_sync_jobs_creates_and_links_local_datastore(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance, name="local-store")

    remote_job = RemoteSyncJob(
        job_id="job-1", remote_name="dr-site", remote_store="main", local_store="local-store", schedule="daily"
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_sync_jobs",
        lambda *a, **kw: SyncJobListResult(reachable=True, sync_jobs=[remote_job]),
    )

    result = sync_instance_sync_jobs(db_session, _FakeSettings(), instance)

    assert result.reachable is True
    assert result.sync_jobs_created == 1
    job = db_session.query(SyncJob).filter_by(pbs_instance_id=instance.id, job_id="job-1").one()
    assert job.remote_name == "dr-site"
    assert job.local_datastore_id == datastore.id


def test_sync_instance_sync_jobs_unmatched_local_store_leaves_datastore_id_none(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)

    remote_job = RemoteSyncJob(job_id="job-1", remote_name="dr-site", remote_store="main", local_store="not-imported")
    monkeypatch.setattr(
        collector_sync_module,
        "list_sync_jobs",
        lambda *a, **kw: SyncJobListResult(reachable=True, sync_jobs=[remote_job]),
    )

    result = sync_instance_sync_jobs(db_session, _FakeSettings(), instance)

    assert result.sync_jobs_created == 1
    job = db_session.query(SyncJob).filter_by(pbs_instance_id=instance.id, job_id="job-1").one()
    assert job.local_datastore_id is None


def test_sync_instance_sync_jobs_second_run_updates(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)

    first_job = RemoteSyncJob(
        job_id="job-1", remote_name="dr-site", remote_store="main", local_store="store1", schedule="daily"
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_sync_jobs",
        lambda *a, **kw: SyncJobListResult(reachable=True, sync_jobs=[first_job]),
    )
    first = sync_instance_sync_jobs(db_session, _FakeSettings(), instance)
    assert first.sync_jobs_created == 1

    updated_job = RemoteSyncJob(
        job_id="job-1", remote_name="dr-site", remote_store="main", local_store="store1", schedule="hourly"
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_sync_jobs",
        lambda *a, **kw: SyncJobListResult(reachable=True, sync_jobs=[updated_job]),
    )
    second = sync_instance_sync_jobs(db_session, _FakeSettings(), instance)
    assert second.sync_jobs_created == 0
    assert second.sync_jobs_updated == 1

    job = db_session.query(SyncJob).filter_by(pbs_instance_id=instance.id, job_id="job-1").one()
    assert job.schedule == "hourly"


def test_sync_instance_acl_creates_entries(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)

    entries = [
        RemoteAclEntry(path="/", role="Admin", subject="root@pam", subject_type="user", propagate=True),
        RemoteAclEntry(
            path="/datastore/backup-store", role="Admin", subject="tenant-a@pbs", subject_type="user", propagate=True
        ),
    ]
    monkeypatch.setattr(
        collector_sync_module, "list_acl_entries", lambda *a, **kw: AclListResult(reachable=True, entries=entries)
    )

    result = sync_instance_acl(db_session, _FakeSettings(), instance)

    assert result.reachable is True
    assert result.entries_synced == 2
    rows = db_session.query(PbsAclEntry).filter_by(pbs_instance_id=instance.id).all()
    assert {r.subject for r in rows} == {"root@pam", "tenant-a@pbs"}


def test_sync_instance_acl_replaces_stale_entries_on_resync(db_session, monkeypatch):
    """A revoked grant must actually disappear -- delete-then-reinsert,
    not an upsert that only ever adds/updates rows.
    """
    instance = _make_pbs_instance(db_session)

    first_entries = [
        RemoteAclEntry(path="/", role="Admin", subject="root@pam", subject_type="user", propagate=True),
        RemoteAclEntry(
            path="/datastore/backup-store", role="Admin", subject="tenant-a@pbs", subject_type="user", propagate=True
        ),
    ]
    monkeypatch.setattr(
        collector_sync_module, "list_acl_entries", lambda *a, **kw: AclListResult(reachable=True, entries=first_entries)
    )
    sync_instance_acl(db_session, _FakeSettings(), instance)

    # tenant-a@pbs's grant was revoked in PBS since the last sync.
    second_entries = [RemoteAclEntry(path="/", role="Admin", subject="root@pam", subject_type="user", propagate=True)]
    monkeypatch.setattr(
        collector_sync_module,
        "list_acl_entries",
        lambda *a, **kw: AclListResult(reachable=True, entries=second_entries),
    )
    result = sync_instance_acl(db_session, _FakeSettings(), instance)

    assert result.entries_synced == 1
    rows = db_session.query(PbsAclEntry).filter_by(pbs_instance_id=instance.id).all()
    assert {r.subject for r in rows} == {"root@pam"}


def test_sync_instance_acl_unreachable_reports_error(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    monkeypatch.setattr(
        collector_sync_module, "list_acl_entries", lambda *a, **kw: AclListResult(reachable=False, error="timed out")
    )

    result = sync_instance_acl(db_session, _FakeSettings(), instance)

    assert result.reachable is False
    assert result.error == "timed out"


def _make_file_index_entry(
    db_session, *, group_id, snapshot_id, customer_id=None, path="/etc/hosts", linked_from_entry_id=None
):
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.file_index_job import FileIndexJob

    job = FileIndexJob(
        status="completed",
        trigger="manual",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        customer_id=customer_id,
    )
    db_session.add(job)
    db_session.flush()
    entry = FileIndexEntry(
        file_index_job_id=job.id,
        backup_snapshot_id=snapshot_id,
        backup_group_id=group_id,
        customer_id=customer_id,
        archive="root.pxar",
        path=path,
        filename=path.rsplit("/", 1)[-1],
        type="file",
        linked_from_entry_id=linked_from_entry_id,
    )
    db_session.add(entry)
    db_session.flush()
    return entry


def test_sync_purges_file_index_entries_for_snapshot_no_longer_on_pbs(db_session, monkeypatch):
    """A pruned-on-PBS snapshot's `file_index_entries` must not survive a
    sync, even though the `backup_snapshots` row itself does (no general
    prune-detection, ADR 0006) -- otherwise search would keep pointing at
    data that no longer exists.
    """
    from resurgam_api.models.file_index_entry import FileIndexEntry

    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(
            reachable=True,
            snapshots=[
                RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok"),
                RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok"),
            ],
        ),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    older_snapshot = (
        db_session.query(BackupSnapshot)
        .filter_by(backup_group_id=group.id, snapshot_time=datetime.fromtimestamp(1_700_000_000, tz=UTC))
        .one()
    )
    newer_snapshot = (
        db_session.query(BackupSnapshot)
        .filter_by(backup_group_id=group.id, snapshot_time=datetime.fromtimestamp(1_700_086_400, tz=UTC))
        .one()
    )
    _make_file_index_entry(db_session, group_id=group.id, snapshot_id=older_snapshot.id)
    newer_entry = _make_file_index_entry(db_session, group_id=group.id, snapshot_id=newer_snapshot.id)
    db_session.commit()

    # Second sync: PBS now only reports the newer snapshot -- the older
    # one was pruned.
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(
            reachable=True,
            snapshots=[
                RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok"),
            ],
        ),
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.file_index_entries_purged == 1
    remaining_ids = {e.id for e in db_session.query(FileIndexEntry).filter_by(backup_group_id=group.id).all()}
    assert remaining_ids == {newer_entry.id}
    # The `backup_snapshots` row itself is untouched -- only its index
    # entries are purged (no general prune-detection, by design).
    assert db_session.query(BackupSnapshot).filter_by(id=older_snapshot.id).count() == 1


def test_sync_keeps_orphaned_entry_still_linked_by_a_live_snapshot(db_session, monkeypatch):
    """ "Reuse and link" (manifest §13, ADR 0016): an entry whose own
    snapshot PBS pruned must still survive this purge if a *different*,
    still-live snapshot's entry links to it for its data -- deleting it
    would silently break that live snapshot's search results. Accepted
    tradeoff (confirmed with the user): the pruned snapshot's own
    browse/search can still surface this one stale row until the newer
    snapshot's own retention eventually lets it go too.
    """
    from resurgam_api.models.file_index_entry import FileIndexEntry

    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(
            reachable=True,
            snapshots=[
                RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok"),
                RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok"),
            ],
        ),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    older_snapshot = (
        db_session.query(BackupSnapshot)
        .filter_by(backup_group_id=group.id, snapshot_time=datetime.fromtimestamp(1_700_000_000, tz=UTC))
        .one()
    )
    newer_snapshot = (
        db_session.query(BackupSnapshot)
        .filter_by(backup_group_id=group.id, snapshot_time=datetime.fromtimestamp(1_700_086_400, tz=UTC))
        .one()
    )
    root_entry = _make_file_index_entry(db_session, group_id=group.id, snapshot_id=older_snapshot.id)
    linked_entry = _make_file_index_entry(
        db_session, group_id=group.id, snapshot_id=newer_snapshot.id, linked_from_entry_id=root_entry.id
    )
    db_session.commit()
    root_id = root_entry.id
    linked_id = linked_entry.id

    # Second sync: PBS now only reports the newer snapshot -- the older
    # one (root_entry's own snapshot) was pruned.
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(
            reachable=True,
            snapshots=[
                RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_086_400, verify_state="ok"),
            ],
        ),
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.file_index_entries_purged == 0  # root survives -- still referenced
    assert db_session.get(FileIndexEntry, root_id) is not None
    assert db_session.get(FileIndexEntry, linked_id) is not None


def test_sync_purges_file_index_entries_for_group_missing_entirely(db_session, monkeypatch):
    """A backup group PBS no longer reports at all (e.g. every one of its
    snapshots pruned) must have all of its `file_index_entries` purged,
    not just entries for groups still present in this sync's response.
    """
    from resurgam_api.models.file_index_entry import FileIndexEntry

    instance = _make_pbs_instance(db_session)
    datastore = _make_datastore(db_session, instance)

    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(
            reachable=True,
            snapshots=[
                RemoteSnapshot(backup_type="ct", backup_id="200", backup_time=1_700_000_000, verify_state="ok"),
            ],
        ),
    )
    sync_datastore_inventory(db_session, _FakeSettings(), datastore)
    group = db_session.query(BackupGroup).filter_by(datastore_id=datastore.id).one()
    snapshot = db_session.query(BackupSnapshot).filter_by(backup_group_id=group.id).one()
    entry = _make_file_index_entry(db_session, group_id=group.id, snapshot_id=snapshot.id)
    db_session.commit()
    entry_id = entry.id  # captured before the delete expires this instance

    # This group no longer appears in PBS's response at all.
    monkeypatch.setattr(
        collector_sync_module, "list_snapshots", lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=[])
    )
    result = sync_datastore_inventory(db_session, _FakeSettings(), datastore)

    assert result.file_index_entries_purged == 1
    assert db_session.query(FileIndexEntry).filter_by(id=entry_id).count() == 0


def test_sync_pbs_instance_continues_after_one_datastore_fails(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    _make_datastore(db_session, instance, name="good-store")
    _make_datastore(db_session, instance, name="bad-store")

    monkeypatch.setattr(collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=True, tasks=[]))

    def fake_list_snapshots(host, port, trust, token, datastore, timeout=5.0):
        if datastore == "bad-store":
            raise RuntimeError("boom")
        return SnapshotListResult(reachable=True, snapshots=[])

    monkeypatch.setattr(collector_sync_module, "list_snapshots", fake_list_snapshots)

    result = sync_pbs_instance(db_session, _FakeSettings(), instance)

    outcomes = {o.datastore_name: o for o in result.datastores}
    assert outcomes["good-store"].result.reachable is True
    assert outcomes["bad-store"].result.reachable is False
    assert "boom" in outcomes["bad-store"].result.error


def test_sync_pbs_instance_stores_the_server_timezone_and_keeps_it_when_unreadable(db_session, monkeypatch):
    instance = _make_pbs_instance(db_session)
    monkeypatch.setattr(collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=True, tasks=[]))
    monkeypatch.setattr(
        collector_sync_module,
        "list_datastore_usage",
        lambda *a, **kw: DatastoreUsageListResult(reachable=False, error="down"),
    )

    monkeypatch.setattr(collector_sync_module, "get_server_timezone", lambda *a, **kw: "Europe/Zurich")
    sync_pbs_instance(db_session, _FakeSettings(), instance)
    assert instance.server_timezone == "Europe/Zurich"

    monkeypatch.setattr(collector_sync_module, "get_server_timezone", lambda *a, **kw: None)
    sync_pbs_instance(db_session, _FakeSettings(), instance)
    assert instance.server_timezone == "Europe/Zurich"
