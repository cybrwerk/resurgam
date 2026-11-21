"""CRUD/RBAC tests for manifest §13's per-customer File Indexing config
(`CustomerFileIndexSettings`, `api/v1/file_index.py`, ADR 0016).
"""

from datetime import UTC


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


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


def _settings_url(customer_id) -> str:
    return f"/api/v1/customers/{customer_id}/file-index-settings"


# --- create -----------------------------------------------------------------------


def test_create_settings(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)

    response = client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)
    assert response.status_code == 201
    body = response.json()
    assert body["enabled"] is True
    assert body["retention_days"] == 90
    assert body["schedule_enabled"] is False


def test_create_settings_requires_platform_admin(client, platform_admin_headers, db_session, test_settings):
    customer_id = _create_customer(db_session)
    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")

    response = client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=operator_headers)
    assert response.status_code == 403


def test_create_settings_conflicts_on_double_create(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)

    response = client.post(_settings_url(customer_id), json={"retention_days": 30}, headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "file_index_settings_exists"


def test_create_settings_rejects_schedule_without_interval(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)

    response = client.post(
        _settings_url(customer_id),
        json={"retention_days": 90, "schedule_enabled": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_create_settings_sets_next_scheduled_run_when_schedule_enabled(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)

    response = client.post(
        _settings_url(customer_id),
        json={"retention_days": 90, "schedule_enabled": True, "schedule_interval_hours": 6},
        headers=platform_admin_headers,
    )
    assert response.status_code == 201
    assert response.json()["next_scheduled_run_at"] is not None


# --- get --------------------------------------------------------------------------


def test_get_settings_404_when_never_configured(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.get(_settings_url(customer_id), headers=platform_admin_headers)
    assert response.status_code == 404


def test_get_settings_returns_disabled_row_not_404(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.post(
        _settings_url(customer_id), json={"retention_days": 90, "enabled": False}, headers=platform_admin_headers
    )

    response = client.get(_settings_url(customer_id), headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["enabled"] is False


# --- patch ------------------------------------------------------------------------


def test_patch_settings_partial_update(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)

    response = client.patch(_settings_url(customer_id), json={"retention_days": 30}, headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["retention_days"] == 30
    assert body["enabled"] is True  # untouched


def test_patch_settings_sets_guest_exec_cluster_and_audits_it(client, platform_admin_headers, db_session):
    """A real bug found live (2026-09-24, first-ever call setting this
    field): `body.model_dump(exclude_unset=True)`'s default mode="python"
    left `guest_exec_pve_cluster_id` as a live `uuid.UUID` object, which
    psycopg's JSONB adapter can't serialize when writing the audit
    event's `detail` -- a 500, not a validation error. Fixed via
    `mode="json"`; this locks in both the successful PATCH and that a
    real AuditEvent row got written (not just avoided a crash).
    """
    customer_id = _create_customer(db_session)
    client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)

    from resurgam_api.models.pve import PveCluster
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(
        reference_key="test-guest-exec-cluster-token", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    cluster = PveCluster(
        display_name="Lab PVE",
        host="pve.example",
        port=8006,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
    )
    db_session.add(cluster)
    db_session.commit()

    response = client.patch(
        _settings_url(customer_id),
        json={
            "guest_exec_pve_cluster_id": str(cluster.id),
            "guest_exec_target_node": "pve1",
            "guest_exec_target_storage": "resurgam-lab-overlays",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["guest_exec_pve_cluster_id"] == str(cluster.id)
    assert body["guest_exec_target_node"] == "pve1"
    assert body["guest_exec_target_storage"] == "resurgam-lab-overlays"

    from resurgam_api.models.audit import AuditEvent

    event = (
        db_session.query(AuditEvent)
        .filter_by(action="customer_file_index_settings_updated")
        .order_by(AuditEvent.occurred_at.desc())
        .first()
    )
    assert event is not None
    assert event.detail["guest_exec_pve_cluster_id"] == str(cluster.id)


def test_patch_settings_requires_platform_admin(client, platform_admin_headers, db_session, test_settings):
    customer_id = _create_customer(db_session)
    client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)
    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")

    response = client.patch(_settings_url(customer_id), json={"retention_days": 30}, headers=operator_headers)
    assert response.status_code == 403


def test_patch_settings_rejects_unpaired_window(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)

    response = client.patch(
        _settings_url(customer_id), json={"window_start_time": "22:00:00"}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_window"


def test_patch_settings_disabling_schedule_clears_next_run(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.post(
        _settings_url(customer_id),
        json={"retention_days": 90, "schedule_enabled": True, "schedule_interval_hours": 6},
        headers=platform_admin_headers,
    )

    response = client.patch(
        _settings_url(customer_id), json={"schedule_enabled": False}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["next_scheduled_run_at"] is None


# --- delete -----------------------------------------------------------------------


def test_delete_settings_purges_entries(client, platform_admin_headers, db_session, monkeypatch):
    from datetime import datetime

    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.file_index_job import FileIndexJob
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    customer_id = _create_customer(db_session)
    client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)

    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name="store1")]),
    )
    instance_response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "resurgam@pbs!dev:secretvalue",
        },
        headers=platform_admin_headers,
    )
    instance_id = instance_response.json()["id"]
    datastore_response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": "store1"}, headers=platform_admin_headers
    )
    datastore_id = datastore_response.json()["id"]

    group = BackupGroup(datastore_id=datastore_id, backup_type="ct", backup_id="100", customer_id=customer_id)
    db_session.add(group)
    db_session.flush()
    snapshot = BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime(2023, 9, 20, tzinfo=UTC))
    db_session.add(snapshot)
    db_session.flush()
    job = FileIndexJob(
        status="completed",
        trigger="manual",
        backup_group_id=group.id,
        backup_snapshot_id=snapshot.id,
        customer_id=customer_id,
    )
    db_session.add(job)
    db_session.flush()
    db_session.add(
        FileIndexEntry(
            file_index_job_id=job.id,
            backup_snapshot_id=snapshot.id,
            backup_group_id=group.id,
            customer_id=customer_id,
            archive="root.pxar",
            path="/etc/hosts",
            filename="hosts",
            type="file",
        )
    )
    db_session.commit()

    response = client.delete(_settings_url(customer_id), headers=platform_admin_headers)
    assert response.status_code == 204

    assert db_session.query(FileIndexEntry).filter_by(customer_id=customer_id).count() == 0
    get_response = client.get(_settings_url(customer_id), headers=platform_admin_headers)
    assert get_response.status_code == 404


def test_delete_settings_requires_platform_admin(client, platform_admin_headers, db_session, test_settings):
    customer_id = _create_customer(db_session)
    client.post(_settings_url(customer_id), json={"retention_days": 90}, headers=platform_admin_headers)
    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")

    response = client.delete(_settings_url(customer_id), headers=operator_headers)
    assert response.status_code == 403


def test_delete_nonexistent_settings_returns_404(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.delete(_settings_url(customer_id), headers=platform_admin_headers)
    assert response.status_code == 404
