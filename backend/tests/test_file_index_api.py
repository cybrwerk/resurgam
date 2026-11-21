"""Endpoint/RBAC tests for manifest §13's File Indexing job + search API
(`api/v1/file_index.py`, ADR 0016). Mirrors `test_filerestore_api.py`'s
conventions -- job creation/polling/cancellation and search are tested
here against the real API/DB layer; the worker's own dispatch behavior is
covered separately in `test_file_index_worker.py`.
"""

import uuid
from datetime import UTC, datetime

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


def _create_pbs_instance(client, headers) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "resurgam@pbs!dev:secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _import_datastore(client, headers, instance_id, monkeypatch, name="store1") -> str:
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name=name)]),
    )
    response = client.post(f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": name}, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _create_group(db_session, datastore_id, backup_type="ct", backup_id="100", file_index_enabled=True):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(
        datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id, file_index_enabled=file_index_enabled
    )
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _add_snapshot(db_session, group_id, snapshot_time=None):
    from resurgam_api.models.inventory import BackupSnapshot

    snapshot = BackupSnapshot(
        backup_group_id=group_id, snapshot_time=snapshot_time or datetime(2023, 9, 20, 0, 0, 3, tzinfo=UTC)
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.commit()
    return snapshot.id


def _create_customer_scoped_user(db_session, test_settings, customer_id, role_key) -> dict:
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email=f"user-{role_key}-{customer_id}@example.com", display_name="Scoped User")
    db_session.add(user)
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey(role_key).value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()

    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def _enable_indexing(db_session, customer_id, **kwargs) -> None:
    from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings

    defaults = {"enabled": True, "retention_days": 30}
    defaults.update(kwargs)
    db_session.add(CustomerFileIndexSettings(customer_id=customer_id, **defaults))
    db_session.commit()


def _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch, *, enable=True, group_enabled=True):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id, backup_type="ct", file_index_enabled=group_enabled)
    snapshot_id = _add_snapshot(db_session, group_id)
    if enable:
        _enable_indexing(db_session, customer_id)
    return customer_id, group_id, snapshot_id


def _create_index_job(client, headers, group_id, snapshot_id):
    return client.post(f"/api/v1/backup-groups/{group_id}/snapshots/{snapshot_id}/index-jobs", headers=headers)


# --- create (manual) ----------------------------------------------------------


def test_create_index_job_returns_pending(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["trigger"] == "manual"
    assert body["entries_indexed"] == 0
    # A real UX-audit finding (2026-09-23): job lists had no way to tell
    # which workload a job was even for, short of resolving
    # backup_group_id by hand. Resolved server-side now.
    assert body["workload_label"] == "100"

    listed = client.get("/api/v1/file-index/jobs", headers=platform_admin_headers).json()
    assert next(j for j in listed if j["id"] == body["id"])["workload_label"] == "100"


def test_create_index_job_stamps_customer_and_actor(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.file_index_job import FileIndexJob

    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    job = db_session.get(FileIndexJob, uuid.UUID(response.json()["id"]))
    assert job.customer_id == customer_id
    assert job.actor_user_id is not None


def test_create_index_job_records_audit_event_with_customer_id(client, platform_admin_headers, db_session, monkeypatch):
    """Recovery Evidence compliance reporting: `AuditEvent.customer_id` is
    denormalized at write time from the already-in-scope job.
    """
    from resurgam_api.models.audit import AuditEvent

    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    job_id = uuid.UUID(response.json()["id"])

    event = db_session.query(AuditEvent).filter_by(action="file_index_job_created", target_id=str(job_id)).one()
    assert event.customer_id == customer_id


def test_create_index_job_rejects_when_indexing_not_enabled(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(
        client, platform_admin_headers, db_session, monkeypatch, enable=False
    )

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert response.status_code == 409
    assert response.json()["code"] == "index_not_enabled"


def test_create_index_job_rejects_when_group_not_opted_in(client, platform_admin_headers, db_session, monkeypatch):
    """Per-VM opt-in (ADR 0016): the customer-level switch alone isn't
    enough -- each backup group must be individually enabled too.
    """
    _customer_id, group_id, snapshot_id = _setup_ct_group(
        client, platform_admin_headers, db_session, monkeypatch, enable=True, group_enabled=False
    )

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert response.status_code == 409
    assert response.json()["code"] == "index_not_enabled_for_group"


def test_enable_and_disable_file_index_for_group(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(
        client, platform_admin_headers, db_session, monkeypatch, enable=True, group_enabled=False
    )

    enable_response = client.post(f"/api/v1/backup-groups/{group_id}/enable-file-index", headers=platform_admin_headers)
    assert enable_response.status_code == 200
    assert enable_response.json()["file_index_enabled"] is True

    create_response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert create_response.status_code == 201

    disable_response = client.post(
        f"/api/v1/backup-groups/{group_id}/disable-file-index", headers=platform_admin_headers
    )
    assert disable_response.status_code == 200
    assert disable_response.json()["file_index_enabled"] is False


def test_set_file_index_exclude_patterns(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-exclude-patterns",
        headers=platform_admin_headers,
        json={"patterns": [r"^/usr/src", r"(?i)/windows($|/)"]},
    )
    assert response.status_code == 200
    assert response.json()["file_index_exclude_patterns"] == [r"^/usr/src", r"(?i)/windows($|/)"]


# --- guest-exec File Indexing (ADR 0021) ----------------------------------------


def test_backup_group_defaults_to_external_mount(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.inventory import BackupGroup

    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    group = db_session.get(BackupGroup, group_id)
    assert group.file_index_method == "external_mount"


def _create_production_cluster(client, headers) -> str:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Prod PVE",
            "host": "pve-prod.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_set_file_index_method(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-method",
        headers=platform_admin_headers,
        json={"method": "guest_exec_ephemeral"},
    )
    assert response.status_code == 200
    assert response.json()["file_index_method"] == "guest_exec_ephemeral"


def test_set_file_index_method_guest_exec_live_requires_cluster(
    client, platform_admin_headers, db_session, monkeypatch
):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-method",
        headers=platform_admin_headers,
        json={"method": "guest_exec_live"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "guest_exec_live_cluster_required"

    cluster_id = _create_production_cluster(client, platform_admin_headers)
    response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-method",
        headers=platform_admin_headers,
        json={"method": "guest_exec_live", "guest_exec_live_pve_cluster_id": cluster_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["file_index_method"] == "guest_exec_live"
    assert body["guest_exec_live_pve_cluster_id"] == cluster_id


def test_create_index_job_rejects_manual_trigger_for_guest_exec_live(
    client, platform_admin_headers, db_session, monkeypatch
):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    cluster_id = _create_production_cluster(client, platform_admin_headers)
    client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-method",
        headers=platform_admin_headers,
        json={"method": "guest_exec_live", "guest_exec_live_pve_cluster_id": cluster_id},
    )

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert response.status_code == 409
    assert response.json()["code"] == "guest_exec_live_not_manually_triggerable"


def test_create_index_job_rejects_guest_exec_ephemeral_without_cluster_config(
    client, platform_admin_headers, db_session, monkeypatch
):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-method",
        headers=platform_admin_headers,
        json={"method": "guest_exec_ephemeral"},
    )

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert response.status_code == 400
    assert response.json()["code"] == "guest_exec_ephemeral_not_configured"


def test_create_index_job_guest_exec_ephemeral_allocates_a_real_vmid(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.api.v1 import file_index as file_index_module
    from resurgam_api.connectivity.models import VmidAvailabilityResult
    from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
    from resurgam_api.models.file_index_job import FileIndexJob

    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-method",
        headers=platform_admin_headers,
        json={"method": "guest_exec_ephemeral"},
    )

    cluster_response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Lab PVE",
            "host": "pve1.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "is_lab_compute": True,
        },
        headers=platform_admin_headers,
    )
    cluster_id = cluster_response.json()["id"]

    settings = db_session.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one()
    settings.guest_exec_pve_cluster_id = uuid.UUID(cluster_id)
    settings.guest_exec_target_node = "pve1"
    settings.guest_exec_target_storage = "local-lvm"
    db_session.commit()

    monkeypatch.setattr(
        file_index_module,
        "next_free_vmid",
        lambda *a, **kw: VmidAvailabilityResult(reachable=True, vmid=500, free=True),
    )

    response = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert response.status_code == 201
    body = response.json()
    assert body["file_index_method"] == "guest_exec_ephemeral"
    assert body["guest_exec_target_vmid"] == 500
    assert body["guest_exec_target_node"] == "pve1"

    job = db_session.get(FileIndexJob, uuid.UUID(body["id"]))
    assert job.guest_exec_target_vmid == 500


def test_set_file_index_exclude_patterns_rejects_invalid_regex(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-exclude-patterns",
        headers=platform_admin_headers,
        json={"patterns": ["["]},  # unterminated character class -- invalid regex
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_pattern"


def test_set_file_index_exclude_patterns_requires_platform_admin(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")

    response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-exclude-patterns",
        headers=operator_headers,
        json={"patterns": ["^/usr/src"]},
    )
    assert response.status_code == 403


def test_get_file_index_default_exclude_patterns(client, platform_admin_headers):
    from resurgam_api.inventory.file_index_defaults import DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS

    response = client.get("/api/v1/file-index-default-exclude-patterns", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["patterns"] == DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS


def test_get_file_index_default_exclude_patterns_open_to_any_authenticated_user(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, _group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")

    response = client.get("/api/v1/file-index-default-exclude-patterns", headers=viewer_headers)
    assert response.status_code == 200
    assert len(response.json()["patterns"]) > 0


def test_new_backup_group_starts_with_default_exclude_patterns(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.inventory.file_index_defaults import DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS
    from resurgam_api.models.inventory import BackupGroup

    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    group = db_session.get(BackupGroup, group_id)
    assert group.file_index_exclude_patterns == DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS


def test_two_new_backup_groups_get_independent_default_pattern_lists(db_session):
    """A shared mutable default across ORM instances would mean editing
    one group's patterns leaks into every other unconfigured group --
    `default_file_index_exclude_patterns` returns a fresh list per row
    specifically to avoid that.
    """
    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(
        reference_key="test-shared-default-token", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    instance = PbsInstance(
        display_name="PBS", host="pbs.example", port=8007, tls_trust_mode="ca", api_token_secret_ref_id=ref.id
    )
    db_session.add(instance)
    db_session.flush()
    datastore = Datastore(pbs_instance_id=instance.id, name="store1")
    db_session.add(datastore)
    db_session.flush()

    group_a = BackupGroup(datastore_id=datastore.id, namespace="", backup_type="ct", backup_id="100")
    group_b = BackupGroup(datastore_id=datastore.id, namespace="", backup_type="ct", backup_id="200")
    db_session.add_all([group_a, group_b])
    db_session.flush()

    group_a.file_index_exclude_patterns.append("^/only-on-a")
    assert "^/only-on-a" not in group_b.file_index_exclude_patterns


def test_enable_file_index_requires_platform_admin(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, group_id, _snapshot_id = _setup_ct_group(
        client, platform_admin_headers, db_session, monkeypatch, group_enabled=False
    )
    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")

    response = client.post(f"/api/v1/backup-groups/{group_id}/enable-file-index", headers=operator_headers)
    assert response.status_code == 403


def test_create_index_job_rejects_when_one_already_in_progress(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    first = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert first.status_code == 201

    second = _create_index_job(client, platform_admin_headers, group_id, snapshot_id)
    assert second.status_code == 409
    assert second.json()["code"] == "index_job_already_in_progress"


def test_create_index_job_requires_restore_access(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")

    response = _create_index_job(client, viewer_headers, group_id, snapshot_id)
    assert response.status_code == 404


def test_create_index_job_for_nonexistent_snapshot_returns_404(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    response = _create_index_job(client, platform_admin_headers, group_id, uuid.uuid4())
    assert response.status_code == 404


# --- bulk create ----------------------------------------------------------------


def test_bulk_create_creates_one_job_per_eligible_snapshot(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    _add_snapshot(db_session, group_id, snapshot_time=datetime(2023, 9, 21, tzinfo=UTC))

    response = client.post(f"/api/v1/backup-groups/{group_id}/index-jobs/bulk", headers=platform_admin_headers)
    assert response.status_code == 201
    body = response.json()
    assert len(body["created_job_ids"]) == 2
    assert body["skipped_already_indexed"] == 0


def test_bulk_create_skips_snapshot_with_in_flight_job(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    _create_index_job(client, platform_admin_headers, group_id, snapshot_id)

    response = client.post(f"/api/v1/backup-groups/{group_id}/index-jobs/bulk", headers=platform_admin_headers)
    assert response.status_code == 201
    body = response.json()
    assert body["created_job_ids"] == []
    assert body["skipped_already_indexed"] == 1


# --- get / list -------------------------------------------------------------------


def test_get_job_cross_tenant_guess_returns_404_not_data(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"]

    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    other_operator_headers = _create_customer_scoped_user(
        db_session, test_settings, other_customer_id, "restore_operator"
    )
    response = client.get(f"/api/v1/file-index/jobs/{job_id}", headers=other_operator_headers)
    assert response.status_code == 404


def test_get_job_allows_customer_scoped_restore_operator(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"]

    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")
    response = client.get(f"/api/v1/file-index/jobs/{job_id}", headers=operator_headers)
    assert response.status_code == 200


def test_list_jobs_filters_by_group(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    _create_index_job(client, platform_admin_headers, group_id, snapshot_id)

    response = client.get(
        "/api/v1/file-index/jobs", params={"backup_group_id": str(group_id)}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


# --- cancel -----------------------------------------------------------------------


def test_cancel_pending_job_is_immediate(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"]

    response = client.post(f"/api/v1/file-index/jobs/{job_id}/cancel", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancel_running_job_sets_cancel_requested_flag(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.file_index_job import FileIndexJob

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    job = db_session.get(FileIndexJob, job_id)
    job.status = "running"
    db_session.commit()

    response = client.post(f"/api/v1/file-index/jobs/{job_id}/cancel", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"  # not flipped synchronously
    assert body["cancel_requested"] is True


def test_cancel_terminal_job_returns_409(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.file_index_job import FileIndexJob

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    job = db_session.get(FileIndexJob, job_id)
    job.status = "completed"
    db_session.commit()

    response = client.post(f"/api/v1/file-index/jobs/{job_id}/cancel", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_cancellable"


# --- search -----------------------------------------------------------------------


def _seed_entry(
    db_session,
    *,
    job_id,
    snapshot_id,
    group_id,
    customer_id,
    path,
    filename,
    type_="file",
    size_bytes=None,
    mtime=None,
    linked_from_entry_id=None,
):
    from resurgam_api.models.file_index_entry import FileIndexEntry

    entry = FileIndexEntry(
        file_index_job_id=job_id,
        backup_snapshot_id=snapshot_id,
        backup_group_id=group_id,
        customer_id=customer_id,
        archive="root.pxar",
        path=path,
        filename=filename,
        type=type_,
        size_bytes=size_bytes,
        mtime=mtime,
        linked_from_entry_id=linked_from_entry_id,
    )
    db_session.add(entry)
    db_session.commit()
    return entry


def test_search_finds_substring_matches(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/var/log/syslog",
        filename="syslog",
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/hosts",
        filename="hosts",
    )

    response = client.get(
        "/api/v1/file-index/search",
        params={"customer_id": str(customer_id), "filename": "yslo"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["filename"] == "syslog"
    assert body["has_more"] is False


def test_search_filters_by_entry_type(client, platform_admin_headers, db_session, monkeypatch):
    # The query parameter is `type` on the wire; the handler's Python name is
    # aliased (it would otherwise shadow the builtin) -- this pins the contract.
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    common = {"job_id": job_id, "snapshot_id": snapshot_id, "group_id": group_id, "customer_id": customer_id}
    _seed_entry(db_session, **common, path="/var/log", filename="log", type_="directory")
    _seed_entry(db_session, **common, path="/var/log/syslog", filename="syslog", type_="file")

    response = client.get(
        "/api/v1/file-index/search",
        params={"customer_id": str(customer_id), "type": "directory"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert [item["filename"] for item in response.json()["items"]] == ["log"]


def test_search_escapes_sql_like_wildcards_in_filename_and_path(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Regression test: a literal `%`/`_` in a search term must be
    matched literally, not treated as a SQL LIKE wildcard (unlike
    `browse_file_index`, which already escapes via `_escape_like`).
    """
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/data/100%_report.pdf",
        filename="100%_report.pdf",
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/data/100Xreport.pdf",
        filename="100Xreport.pdf",
    )

    response = client.get(
        "/api/v1/file-index/search",
        params={"customer_id": str(customer_id), "filename": "100%_report"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["filename"] == "100%_report.pdf"


def test_search_resolves_size_and_mtime_through_a_link(client, platform_admin_headers, db_session, monkeypatch):
    """ "Reuse and link" (manifest §13, ADR 0016): a linked row's own
    `size_bytes`/`mtime` are NULL by design -- search must still return
    the real values, resolved from the row it links to.
    """
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    root = _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/hosts",
        filename="hosts",
        size_bytes=123,
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/motd",
        filename="motd",
        size_bytes=None,
        linked_from_entry_id=root.id,
    )

    response = client.get(
        "/api/v1/file-index/search",
        params={"customer_id": str(customer_id), "filename": "motd"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["size_bytes"] == 123  # resolved through the link, not the row's own NULL


def test_search_requires_customer_scope(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_id, _group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    other_operator_headers = _create_customer_scoped_user(
        db_session, test_settings, other_customer_id, "restore_operator"
    )
    response = client.get(
        "/api/v1/file-index/search", params={"customer_id": str(customer_id)}, headers=other_operator_headers
    )
    assert response.status_code == 404


def test_browse_lists_only_direct_children(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc",
        filename="etc",
        type_="directory",
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/hosts",
        filename="hosts",
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/nested/deep.conf",
        filename="deep.conf",
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/var",
        filename="var",
        type_="directory",
    )

    response = client.get(
        "/api/v1/file-index/browse",
        params={"backup_group_id": str(group_id), "backup_snapshot_id": str(snapshot_id), "path": "/"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    root_paths = {e["path"] for e in response.json()}
    assert root_paths == {"/etc", "/var"}

    response2 = client.get(
        "/api/v1/file-index/browse",
        params={"backup_group_id": str(group_id), "backup_snapshot_id": str(snapshot_id), "path": "/etc"},
        headers=platform_admin_headers,
    )
    etc_paths = {e["path"] for e in response2.json()}
    # "/etc/nested/deep.conf" is two levels deep from "/etc" -- must not appear.
    assert etc_paths == {"/etc/hosts"}


def test_browse_sorts_directories_before_files(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/aaa_file.txt",
        filename="aaa_file.txt",
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/zzz_dir",
        filename="zzz_dir",
        type_="directory",
    )

    response = client.get(
        "/api/v1/file-index/browse",
        params={"backup_group_id": str(group_id), "backup_snapshot_id": str(snapshot_id), "path": "/"},
        headers=platform_admin_headers,
    )
    names = [e["filename"] for e in response.json()]
    assert names == ["zzz_dir", "aaa_file.txt"]


def test_browse_requires_restore_access(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")

    response = client.get(
        "/api/v1/file-index/browse",
        params={"backup_group_id": str(group_id), "backup_snapshot_id": str(snapshot_id)},
        headers=viewer_headers,
    )
    assert response.status_code == 404


# --- snapshot-status ----------------------------------------------------------------


def test_snapshot_status_lists_only_snapshots_with_an_index(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    other_snapshot_id = _add_snapshot(
        db_session, group_id, snapshot_time=datetime(2024, 1, 1, tzinfo=UTC)
    )  # never indexed

    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/hosts",
        filename="hosts",
    )

    response = client.get(
        "/api/v1/file-index/snapshot-status", params={"backup_group_id": str(group_id)}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["indexed_snapshot_ids"] == [str(snapshot_id)]
    assert str(other_snapshot_id) not in response.json()["indexed_snapshot_ids"]


def test_snapshot_status_dedupes_multiple_entries_per_snapshot(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/hosts",
        filename="hosts",
    )
    _seed_entry(
        db_session,
        job_id=job_id,
        snapshot_id=snapshot_id,
        group_id=group_id,
        customer_id=customer_id,
        path="/etc/motd",
        filename="motd",
    )

    response = client.get(
        "/api/v1/file-index/snapshot-status", params={"backup_group_id": str(group_id)}, headers=platform_admin_headers
    )
    assert response.json()["indexed_snapshot_ids"] == [str(snapshot_id)]


def test_snapshot_status_empty_group_returns_empty_list(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = client.get(
        "/api/v1/file-index/snapshot-status", params={"backup_group_id": str(group_id)}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["indexed_snapshot_ids"] == []


def test_snapshot_status_requires_restore_access(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")

    response = client.get(
        "/api/v1/file-index/snapshot-status", params={"backup_group_id": str(group_id)}, headers=viewer_headers
    )
    assert response.status_code == 404


def test_snapshot_status_unknown_group_returns_404(client, platform_admin_headers):
    response = client.get(
        "/api/v1/file-index/snapshot-status",
        params={"backup_group_id": "00000000-0000-0000-0000-000000000000"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404


def test_search_has_more_pagination(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_index_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    for i in range(3):
        _seed_entry(
            db_session,
            job_id=job_id,
            snapshot_id=snapshot_id,
            group_id=group_id,
            customer_id=customer_id,
            path=f"/file{i}",
            filename=f"file{i}",
        )

    response = client.get(
        "/api/v1/file-index/search",
        params={"customer_id": str(customer_id), "limit": 2},
        headers=platform_admin_headers,
    )
    body = response.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
