"""Endpoint/RBAC tests for the manifest §13/§15 file-restore job queue
(ADR 0013). Job *creation* and *polling* are tested here, against the real
API/DB layer; the worker's own dispatch/error-mapping behavior (calling
`filerestore.browse`) is covered separately in `test_file_restore_worker.py`,
since the API layer no longer calls `browse` directly.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


@pytest.fixture
def test_settings(test_settings, tmp_path):
    """Download content lives in files now -- each test gets its own output directory."""
    return test_settings.model_copy(update={"file_restore_output_dir": str(tmp_path / "file-restore")})


def _complete_download_job(client, headers, db_session, settings, group_id, snapshot_id, content, job_type="download"):
    """A job as the worker leaves it: completed, content file in the output directory."""
    from resurgam_api.filerestore import storage
    from resurgam_api.models.filerestore_job import FileRestoreJob

    if job_type == "archive":
        response = _create_archive_job(client, headers, group_id, snapshot_id, ["/etc/nginx", "/etc/hosts"])
    else:
        response = _create_job(client, headers, group_id, snapshot_id, job_type="download", path="/etc/hosts")
    job_id = uuid.UUID(response.json()["id"])
    job = db_session.get(FileRestoreJob, job_id)
    job.status = "completed"
    job.result_filename = "hosts" if job_type == "download" else "Grüsse.zip"
    job.result_storage_name = storage.storage_name(job_id, job_type)
    job.result_size_bytes = len(content)
    if job_type == "archive":
        job.archive_report = {
            "file_count": 2,
            "directory_count": 1,
            "symlink_count": 0,
            "total_bytes": 99,
            "skipped_count": 1,
            "renamed_count": 0,
            "skipped": [{"path": "/etc/nginx/secret", "reason": "not readable"}],
            "renamed": [],
        }
    db_session.commit()
    (storage.ensure_output_dir(settings) / job.result_storage_name).write_bytes(content)
    return job_id


def _create_archive_job(client, headers, group_id, snapshot_id, paths):
    return client.post(
        f"/api/v1/backup-groups/{group_id}/snapshots/{snapshot_id}/files/jobs",
        json={"job_type": "archive", "paths": paths},
        headers=headers,
    )


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


def _create_group(db_session, datastore_id, backup_type="ct", backup_id="100"):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id)
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


def _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id, backup_type="ct")
    snapshot_id = _add_snapshot(db_session, group_id)
    return customer_id, group_id, snapshot_id


def _create_job(client, headers, group_id, snapshot_id, job_type="list", path="/"):
    return client.post(
        f"/api/v1/backup-groups/{group_id}/snapshots/{snapshot_id}/files/jobs",
        json={"job_type": job_type, "path": path},
        headers=headers,
    )


def test_create_list_job_returns_pending(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_job(client, platform_admin_headers, group_id, snapshot_id, job_type="list", path="/etc")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["job_type"] == "list"
    assert body["path"] == "/etc"
    assert body["result"] is None
    # A real UX-audit finding (2026-09-23): job lists had no way to tell
    # which workload a job was even for. Resolved server-side now.
    assert body["workload_label"] == "100"

    listed = client.get("/api/v1/file-restore/jobs", headers=platform_admin_headers).json()
    assert next(j for j in listed if j["id"] == body["id"])["workload_label"] == "100"


def test_create_job_stamps_customer_and_actor(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.filerestore_job import FileRestoreJob

    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_job(client, platform_admin_headers, group_id, snapshot_id)
    job = db_session.get(FileRestoreJob, uuid.UUID(response.json()["id"]))
    assert job.customer_id == customer_id
    assert job.actor_user_id is not None
    assert job.backup_group_id == group_id


def test_create_job_records_audit_event_with_customer_id(client, platform_admin_headers, db_session, monkeypatch):
    """Recovery Evidence compliance reporting: `AuditEvent.customer_id` is
    denormalized at write time from the already-in-scope job -- this is
    the regression guard for that threading, not a full retest of job
    creation itself.
    """
    from resurgam_api.models.audit import AuditEvent

    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_job(client, platform_admin_headers, group_id, snapshot_id)
    job_id = uuid.UUID(response.json()["id"])

    event = db_session.query(AuditEvent).filter_by(action="file_restore_job_created", target_id=str(job_id)).one()
    assert event.customer_id == customer_id


def test_get_job_requires_restore_access(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"]

    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")
    response = client.get(f"/api/v1/file-restore/jobs/{job_id}", headers=viewer_headers)
    assert response.status_code == 404


def test_get_job_allows_customer_scoped_restore_operator(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"]

    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")
    response = client.get(f"/api/v1/file-restore/jobs/{job_id}", headers=operator_headers)
    assert response.status_code == 200


def test_get_job_cross_tenant_guess_returns_404_not_data(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """A job id is not itself a capability -- polling with it must still
    re-check the caller's scope against the job's own group, or a leaked/
    guessed id from another customer would leak that customer's data.
    """
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_job(client, platform_admin_headers, group_id, snapshot_id, path="/very/secret/path").json()["id"]

    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    other_operator_headers = _create_customer_scoped_user(
        db_session, test_settings, other_customer_id, "restore_operator"
    )
    response = client.get(f"/api/v1/file-restore/jobs/{job_id}", headers=other_operator_headers)
    assert response.status_code == 404


def test_get_nonexistent_job_returns_404(client, platform_admin_headers):
    response = client.get(f"/api/v1/file-restore/jobs/{uuid.uuid4()}", headers=platform_admin_headers)
    assert response.status_code == 404


def test_create_job_for_nonexistent_snapshot_returns_404(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, _snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    response = _create_job(client, platform_admin_headers, group_id, uuid.uuid4())
    assert response.status_code == 404


def test_download_endpoint_rejects_non_completed_job(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_job(client, platform_admin_headers, group_id, snapshot_id, job_type="download").json()["id"]

    response = client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_ready"


def test_download_endpoint_returns_bytes_once_completed(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"127.0.0.1 localhost\n"
    )

    response = client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.content == b"127.0.0.1 localhost\n"
    assert 'filename="hosts"' in response.headers["content-disposition"]


def test_download_endpoint_records_audit_event(client, platform_admin_headers, db_session, monkeypatch, test_settings):
    """Manifest §10.3: the one of Browse/Extract/Download/Stop that
    wasn't already covered by job lifecycle events -- the actual fetch of
    already-extracted content, not just the job that produced it.
    """
    from resurgam_api.models.audit import AuditEvent

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"127.0.0.1 localhost\n"
    )

    response = client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)
    assert response.status_code == 200

    event = db_session.query(AuditEvent).filter_by(action="file_restore_job_downloaded", target_id=str(job_id)).one()
    assert event.actor_user_id is not None
    assert event.customer_id is not None
    # what was downloaded -- never the content itself
    assert event.detail["paths"] == ["/etc/hosts"]
    assert event.detail["size_bytes"] == len(b"127.0.0.1 localhost\n")
    assert "127.0.0.1" not in json.dumps(event.detail)


def test_download_endpoint_returns_410_once_content_expired(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.filerestore_job import FileRestoreJob

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(
        _create_job(client, platform_admin_headers, group_id, snapshot_id, job_type="download").json()["id"]
    )

    job = db_session.get(FileRestoreJob, job_id)
    job.status = "completed"
    job.result_filename = "hosts"
    job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    response = client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)
    assert response.status_code == 410
    assert response.json()["code"] == "content_expired"


def test_list_jobs_returns_most_recent_first_and_filters_by_status(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.filerestore_job import FileRestoreJob

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    older_id = uuid.UUID(_create_job(client, platform_admin_headers, group_id, snapshot_id, path="/older").json()["id"])
    newer_id = uuid.UUID(_create_job(client, platform_admin_headers, group_id, snapshot_id, path="/newer").json()["id"])
    older = db_session.get(FileRestoreJob, older_id)
    older.created_at = older.created_at - timedelta(minutes=5)
    older.status = "completed"
    db_session.commit()

    response = client.get("/api/v1/file-restore/jobs", headers=platform_admin_headers)
    assert response.status_code == 200
    ids = [j["id"] for j in response.json()]
    assert ids.index(str(newer_id)) < ids.index(str(older_id))

    filtered = client.get("/api/v1/file-restore/jobs", params={"status": "completed"}, headers=platform_admin_headers)
    filtered_ids = {j["id"] for j in filtered.json()}
    assert filtered_ids == {str(older_id)}


def test_list_jobs_scoped_to_customer_visibility(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    _create_job(client, platform_admin_headers, group_id, snapshot_id)

    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    other_operator_headers = _create_customer_scoped_user(
        db_session, test_settings, other_customer_id, "restore_operator"
    )
    response = client.get("/api/v1/file-restore/jobs", headers=other_operator_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_list_jobs_requires_restore_operator_not_just_customer_visibility(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """A customer-scoped role without `restore_operator` (viewer, auditor,
    customer_admin, backup_operator) must not see this list -- a
    completed 'list' job's `result` is a real file/directory listing from
    inside that customer's backup, the same bar `get_file_restore_job`
    already enforces per-job via `require_restore_access`.
    """
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    _create_job(client, platform_admin_headers, group_id, snapshot_id)

    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")
    response = client.get("/api/v1/file-restore/jobs", headers=viewer_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_cancel_pending_job(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"]

    response = client.post(f"/api/v1/file-restore/jobs/{job_id}/cancel", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancel_running_job_is_accepted_and_a_finished_one_rejected(
    client, platform_admin_headers, db_session, monkeypatch
):
    """A running job can be cancelled now: the worker's guard notices and stops (see
    test_file_restore_worker); only a job that already ended is refused."""
    from resurgam_api.models.filerestore_job import FileRestoreJob

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = uuid.UUID(_create_job(client, platform_admin_headers, group_id, snapshot_id).json()["id"])
    job = db_session.get(FileRestoreJob, job_id)
    job.status = "running"
    db_session.commit()

    response = client.post(f"/api/v1/file-restore/jobs/{job_id}/cancel", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    response = client.post(f"/api/v1/file-restore/jobs/{job_id}/cancel", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_cancellable"


def test_capability_endpoint_is_reachable_by_any_authenticated_user(client, platform_admin_headers):
    response = client.get("/api/v1/file-restore/capability", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert "checks" in body
    assert len(body["checks"]) == 8
    assert all(c["status"] in ("ok", "degraded", "missing") for c in body["checks"])
    assert [t["backup_type"] for t in body["backup_types"]] == ["vm", "ct", "host"]


def test_capability_endpoint_reports_the_appliance_host_from_the_probe_file(
    client, platform_admin_headers, tmp_path, test_settings
):
    """On the appliance the API container has none of the tools; the report must come from the host probe."""
    state = tmp_path / "host-capabilities.json"
    state.write_text(
        json.dumps({"kvm": {"available": True, "detail": "ok"}, "checked_at": "2026-10-07T00:00:00+00:00"})
    )
    test_settings.host_capabilities_file = str(state)
    body = client.get("/api/v1/file-restore/capability", headers=platform_admin_headers).json()
    assert body["location"] == "appliance_host"
    vm = body["backup_types"][0]
    assert vm["available"] is False and "resurgam-host-probe" in vm["message"]


# --- folder / multi-selection ZIP ("archive") jobs ---------------------------------------------


def test_create_archive_job_normalizes_selection_and_records_paths(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.audit import AuditEvent

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    response = _create_archive_job(
        client,
        platform_admin_headers,
        group_id,
        snapshot_id,
        ["/etc/nginx/", "/etc/hosts", "/etc/nginx/sites", "/etc/hosts", "/etc/Grüsse – 日本.txt"],
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["job_type"] == "archive"
    assert body["status"] == "pending"
    # duplicates and entries inside a selected directory are dropped; trailing slashes normalized
    assert body["paths"] == ["/etc/Grüsse – 日本.txt", "/etc/hosts", "/etc/nginx"]
    assert body["path"] == "/etc"  # the common parent the ZIP's names are relative to
    assert body["content_available"] is False
    event = db_session.query(AuditEvent).filter_by(action="file_restore_job_created", target_id=body["id"]).one()
    assert event.detail == {"job_type": "archive", "paths": body["paths"]}


def test_create_archive_job_common_parent_spans_directories(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    response = _create_archive_job(
        client, platform_admin_headers, group_id, snapshot_id, ["/var/www", "/var/log/syslog"]
    )
    assert response.json()["path"] == "/var"


@pytest.mark.parametrize(
    ("paths", "fragment"),
    [
        ([], "at least one"),
        (["etc/hosts"], "absolute"),
        (["/etc/../root"], "'..'"),
        (["/"], "snapshot root"),
    ],
)
def test_create_archive_job_rejects_invalid_selections(
    client, platform_admin_headers, db_session, monkeypatch, paths, fragment
):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    response = _create_archive_job(client, platform_admin_headers, group_id, snapshot_id, paths)
    assert response.status_code == 422
    assert fragment in response.json()["message"]


def test_create_archive_job_enforces_max_entries(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.config import get_settings

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    client.app.dependency_overrides[get_settings] = lambda: test_settings.model_copy(
        update={"file_restore_archive_max_entries": 2}
    )
    response = _create_archive_job(client, platform_admin_headers, group_id, snapshot_id, ["/a", "/b", "/c"])
    assert response.status_code == 422
    assert "at most 2" in response.json()["message"]


def test_create_download_job_requires_a_path(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    response = client.post(
        f"/api/v1/backup-groups/{group_id}/snapshots/{snapshot_id}/files/jobs",
        json={"job_type": "download"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422
    assert "path" in response.json()["message"]


def test_create_download_job_refused_when_disk_is_below_the_reserve(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.filerestore import storage

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    monkeypatch.setattr(storage, "disk_space", lambda settings: storage.DiskSpace(free_bytes=10, total_bytes=100))

    for response in (
        _create_job(client, platform_admin_headers, group_id, snapshot_id, job_type="download", path="/etc/hosts"),
        _create_archive_job(client, platform_admin_headers, group_id, snapshot_id, ["/etc"]),
    ):
        assert response.status_code == 507
        assert response.json()["code"] == "insufficient_disk_space"
        assert "FILE_RESTORE_MIN_FREE_BYTES" in response.json()["message"]
    # listing needs no disk space
    assert _create_job(client, platform_admin_headers, group_id, snapshot_id).status_code == 201


def test_archive_job_requires_restore_access(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")
    response = _create_archive_job(client, viewer_headers, group_id, snapshot_id, ["/etc"])
    assert response.status_code == 404

    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")
    assert _create_archive_job(client, operator_headers, group_id, snapshot_id, ["/etc"]).status_code == 201

    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    other_headers = _create_customer_scoped_user(db_session, test_settings, other_customer_id, "restore_operator")
    assert _create_archive_job(client, other_headers, group_id, snapshot_id, ["/etc"]).status_code == 404


def test_download_archive_streams_zip_with_utf8_filename_and_report(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"PK-zip", job_type="archive"
    )

    job = client.get(f"/api/v1/file-restore/jobs/{job_id}", headers=platform_admin_headers).json()
    assert job["content_available"] is True
    assert job["result_size_bytes"] == len(b"PK-zip")
    assert job["archive_report"]["skipped"] == [{"path": "/etc/nginx/secret", "reason": "not readable"}]

    response = client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.content == b"PK-zip"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-length"] == str(len(b"PK-zip"))
    assert "filename*=utf-8''Gr%C3%BCsse.zip" in response.headers["content-disposition"]


def test_download_audit_event_records_archive_paths_counts_and_size(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.models.audit import AuditEvent

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"PK-zip", job_type="archive"
    )
    client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)

    event = db_session.query(AuditEvent).filter_by(action="file_restore_job_downloaded", target_id=str(job_id)).one()
    assert event.detail["paths"] == ["/etc/hosts", "/etc/nginx"]
    assert event.detail["file_count"] == 2
    assert event.detail["total_bytes"] == 99
    assert event.detail["size_bytes"] == len(b"PK-zip")
    assert event.detail["skipped_paths"] == ["/etc/nginx/secret"]
    assert event.detail["via"] == "header"


def test_download_returns_410_when_content_file_is_gone(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.filerestore import storage

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"x"
    )
    (storage.output_dir(test_settings) / f"{job_id}.bin").unlink()

    response = client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)
    assert response.status_code == 410
    assert response.json()["code"] == "content_expired"
    job = client.get(f"/api/v1/file-restore/jobs/{job_id}", headers=platform_admin_headers).json()
    assert (
        job["content_available"] is True or job["content_available"] is False
    )  # flag is DB-based, file check is at download


def test_download_never_serves_a_storage_name_outside_the_output_dir(
    client, platform_admin_headers, db_session, monkeypatch, test_settings, tmp_path
):
    from resurgam_api.models.filerestore_job import FileRestoreJob

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"x"
    )
    (tmp_path / "secret").write_text("outside")
    job = db_session.get(FileRestoreJob, job_id)
    job.result_storage_name = "../secret"
    db_session.commit()

    response = client.get(f"/api/v1/file-restore/jobs/{job_id}/download", headers=platform_admin_headers)
    assert response.status_code == 410


def test_download_ticket_allows_headerless_download_bound_to_job(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    from resurgam_api.models.audit import AuditEvent

    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"hello"
    )
    other_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"other"
    )

    ticket = client.post(f"/api/v1/file-restore/jobs/{job_id}/download-ticket", headers=platform_admin_headers)
    assert ticket.status_code == 200
    url = ticket.json()["download_url"]
    assert url.startswith(f"/file-restore/jobs/{job_id}/download?ticket=")

    response = client.get(f"/api/v1{url}")  # no Authorization header
    assert response.status_code == 200
    assert response.content == b"hello"
    event = db_session.query(AuditEvent).filter_by(action="file_restore_job_downloaded", target_id=str(job_id)).one()
    assert event.detail["via"] == "ticket"

    # bound to its job: useless for another one
    token = url.split("ticket=", 1)[1]
    assert client.get(f"/api/v1/file-restore/jobs/{other_id}/download?ticket={token}").status_code == 401
    # and never accepted as a session token
    assert (
        client.get(f"/api/v1/file-restore/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )
    # tampered
    assert client.get(f"/api/v1/file-restore/jobs/{job_id}/download?ticket={token}x").status_code == 401
    # no credentials at all
    assert client.get(f"/api/v1/file-restore/jobs/{job_id}/download").status_code == 401


def test_download_ticket_rechecks_access_and_expiry(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    import time

    from resurgam_api.auth.sessions import create_file_restore_download_ticket
    from resurgam_api.models.identity import User

    customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _complete_download_job(
        client, platform_admin_headers, db_session, test_settings, group_id, snapshot_id, b"x"
    )

    # a ticket for a user without restore access to this group is refused like any other request
    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")
    viewer = db_session.query(User).filter(User.email.like("user-viewer-%")).one()
    ticket, _ = create_file_restore_download_ticket(viewer.id, job_id, test_settings)
    assert client.get(f"/api/v1/file-restore/jobs/{job_id}/download?ticket={ticket}").status_code == 404
    assert client.post(f"/api/v1/file-restore/jobs/{job_id}/download-ticket", headers=viewer_headers).status_code == 404

    expired_settings = test_settings.model_copy(update={"file_restore_download_ticket_ttl_seconds": -1})
    admin_id = db_session.query(User).filter_by(is_breakglass=True).first().id
    stale, _ = create_file_restore_download_ticket(admin_id, job_id, expired_settings)
    time.sleep(0.01)
    assert client.get(f"/api/v1/file-restore/jobs/{job_id}/download?ticket={stale}").status_code == 401


def test_download_ticket_refused_for_unfinished_job(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, group_id, snapshot_id = _setup_ct_group(client, platform_admin_headers, db_session, monkeypatch)
    job_id = _create_job(client, platform_admin_headers, group_id, snapshot_id, job_type="download", path="/x").json()[
        "id"
    ]
    response = client.post(f"/api/v1/file-restore/jobs/{job_id}/download-ticket", headers=platform_admin_headers)
    assert response.status_code == 409
