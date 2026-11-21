"""API-level tests for `api/v1/pbs_jobs.py` -- PBS Prune/Verify job
administration (docs/adr/0023-backup-and-maintenance-job-administration). Mirrors
`test_pve_clusters_api.py`'s backup-job test pattern exactly: capability
gating, get/create/update/delete, write-failure surfaces the real PBS
error text, and platform_admin gating via a genuinely separate user
(not a second breakglass login -- see that file's own docstring for why).
"""


def _create_pbs_instance(client, headers, **overrides) -> str:
    payload = {
        "display_name": "Linked PBS",
        "host": "pbs1.internal.example",
        "port": 8007,
        "tls_trust_mode": "ca",
        "api_token": "test@pbs!token:secretvalue",
    }
    payload.update(overrides)
    response = client.post("/api/v1/pbs-instances", json=payload, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


# --- Prune jobs ---------------------------------------------------------------


def test_prune_job_capabilities_reports_real_grants(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PermissionsResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(
            reachable=True, privileges={"/": {"Datastore.Audit": 1, "Datastore.Modify": 1}}
        ),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/capabilities", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json() == {"can_read": True, "can_write": True, "missing_write_privileges": []}


def test_prune_job_capabilities_read_only_token(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PermissionsResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(reachable=True, privileges={"/": {"Datastore.Audit": 1}}),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/capabilities", headers=platform_admin_headers
    )
    assert response.json() == {
        "can_read": True,
        "can_write": False,
        "missing_write_privileges": ["Datastore.Modify"],
    }


def test_list_datastore_prune_jobs_filters_by_store(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PbsPruneJob, PbsPruneJobListResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "list_prune_jobs",
        lambda *a, **kw: PbsPruneJobListResult(
            reachable=True,
            jobs=[
                PbsPruneJob(id="p1", store="store1", keep_daily=7),
                PbsPruneJob(id="p2", store="other-store", keep_daily=3),
            ],
        ),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/prune-jobs", headers=platform_admin_headers
    )
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "p1"


def test_get_pbs_prune_job_not_found_returns_404(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(pbs_jobs_module, "get_prune_job", lambda *a, **kw: None)

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/prune-jobs/nope", headers=platform_admin_headers)
    assert response.status_code == 404


def test_create_prune_job_generates_id_and_audits(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PbsPruneJob
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    create_calls = []
    monkeypatch.setattr(pbs_jobs_module, "create_prune_job", lambda *a, job, **kw: create_calls.append(job))
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_prune_job",
        lambda *a, job_id, **kw: PbsPruneJob(id=job_id, store="store1", schedule="21:00", keep_daily=7),
    )

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/prune-jobs",
        json={"schedule": "21:00", "keep_daily": 7},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["keep_daily"] == 7
    assert create_calls[0].id.startswith("resurgam-")
    assert create_calls[0].store == "store1"

    event = db_session.query(AuditEvent).filter_by(action="pbs_prune_job_created").one()
    assert event.detail["store"] == "store1"


def test_create_prune_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 400: parameter verification failed")

    monkeypatch.setattr(pbs_jobs_module, "create_prune_job", _raise)

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/prune-jobs",
        json={"schedule": "21:00"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_write_failed"
    assert "parameter verification failed" in response.json()["message"]


def test_update_prune_job_sends_only_changed_fields(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PbsPruneJob
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    update_calls = []
    monkeypatch.setattr(
        pbs_jobs_module, "update_prune_job", lambda *a, job_id, changes, **kw: update_calls.append(changes)
    )
    monkeypatch.setattr(
        pbs_jobs_module, "get_prune_job", lambda *a, **kw: PbsPruneJob(id="p1", store="store1", keep_daily=14)
    )

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1", json={"keep_daily": 14}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert update_calls == [{"keep-daily": 14}]

    event = db_session.query(AuditEvent).filter_by(action="pbs_prune_job_updated").one()
    assert event.detail == {"job_id": "p1", "changed_fields": ["keep-daily"]}


def test_update_prune_job_no_fields_returns_400(client, platform_admin_headers):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.put(f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1", json={}, headers=platform_admin_headers)
    assert response.status_code == 400
    assert response.json()["code"] == "no_changes"


def test_delete_prune_job_audits_and_returns_204(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    delete_calls = []
    monkeypatch.setattr(pbs_jobs_module, "delete_prune_job", lambda *a, job_id, **kw: delete_calls.append(job_id))

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1", headers=platform_admin_headers)
    assert response.status_code == 204
    assert delete_calls == ["p1"]

    event = db_session.query(AuditEvent).filter_by(action="pbs_prune_job_deleted").one()
    assert event.detail == {"job_id": "p1"}


def test_delete_prune_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 400: no such job")

    monkeypatch.setattr(pbs_jobs_module, "delete_prune_job", _raise)

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1", headers=platform_admin_headers)
    assert response.status_code == 502


# --- Verify jobs ---------------------------------------------------------------


def test_verify_job_capabilities_reports_real_grants(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PermissionsResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(
            reachable=True, privileges={"/": {"Datastore.Audit": 1, "Datastore.Verify": 1}}
        ),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/verify-jobs/capabilities", headers=platform_admin_headers
    )
    assert response.json() == {"can_read": True, "can_write": True, "missing_write_privileges": []}


def test_list_datastore_verify_jobs_filters_by_store(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PbsVerifyJob, PbsVerifyJobListResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "list_verify_jobs",
        lambda *a, **kw: PbsVerifyJobListResult(
            reachable=True,
            jobs=[
                PbsVerifyJob(id="v1", store="store1", outdated_after=30),
                PbsVerifyJob(id="v2", store="other-store"),
            ],
        ),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/verify-jobs", headers=platform_admin_headers
    )
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "v1"


def test_get_pbs_verify_job_not_found_returns_404(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(pbs_jobs_module, "get_verify_job", lambda *a, **kw: None)

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/verify-jobs/nope", headers=platform_admin_headers)
    assert response.status_code == 404


def test_create_verify_job_generates_id_and_audits(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PbsVerifyJob
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    create_calls = []
    monkeypatch.setattr(pbs_jobs_module, "create_verify_job", lambda *a, job, **kw: create_calls.append(job))
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_verify_job",
        lambda *a, job_id, **kw: PbsVerifyJob(id=job_id, store="store1", outdated_after=30),
    )

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/verify-jobs",
        json={"outdated_after": 30},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["outdated_after"] == 30
    assert create_calls[0].id.startswith("resurgam-")
    assert create_calls[0].store == "store1"

    event = db_session.query(AuditEvent).filter_by(action="pbs_verify_job_created").one()
    assert event.detail["store"] == "store1"


def test_create_verify_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 403: Permission check failed")

    monkeypatch.setattr(pbs_jobs_module, "create_verify_job", _raise)

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/verify-jobs", json={}, headers=platform_admin_headers
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_write_failed"


def test_update_verify_job_sends_only_changed_fields(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PbsVerifyJob
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    update_calls = []
    monkeypatch.setattr(
        pbs_jobs_module, "update_verify_job", lambda *a, job_id, changes, **kw: update_calls.append(changes)
    )
    monkeypatch.setattr(
        pbs_jobs_module, "get_verify_job", lambda *a, **kw: PbsVerifyJob(id="v1", store="store1", outdated_after=45)
    )

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/verify-jobs/v1",
        json={"outdated_after": 45},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert update_calls == [{"outdated-after": 45}]

    event = db_session.query(AuditEvent).filter_by(action="pbs_verify_job_updated").one()
    assert event.detail == {"job_id": "v1", "changed_fields": ["outdated-after"]}


def test_update_verify_job_no_fields_returns_400(client, platform_admin_headers):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/verify-jobs/v1", json={}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "no_changes"


def test_delete_verify_job_audits_and_returns_204(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    delete_calls = []
    monkeypatch.setattr(pbs_jobs_module, "delete_verify_job", lambda *a, job_id, **kw: delete_calls.append(job_id))

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/verify-jobs/v1", headers=platform_admin_headers)
    assert response.status_code == 204
    assert delete_calls == ["v1"]

    event = db_session.query(AuditEvent).filter_by(action="pbs_verify_job_deleted").one()
    assert event.detail == {"job_id": "v1"}


def test_delete_verify_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 400: no such job")

    monkeypatch.setattr(pbs_jobs_module, "delete_verify_job", _raise)

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/verify-jobs/v1", headers=platform_admin_headers)
    assert response.status_code == 502


def test_pbs_job_endpoints_require_platform_admin(client, platform_admin_headers, db_session, test_settings):
    """Same discipline as `test_pve_clusters_api.py::test_backup_job_endpoints_require_platform_admin`
    -- a genuinely separate, directly-minted user, not a second breakglass
    login (breakglass always resolves to the same fixed identity that
    `platform_admin_headers` already granted global platform_admin, in
    this same test transaction).
    """
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    user = User(email="viewer-pbs-jobs@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()
    viewer_role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=viewer_role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    non_admin_headers = {"Authorization": f"Bearer {token}"}

    assert (
        client.get(
            f"/api/v1/pbs-instances/{instance_id}/prune-jobs/capabilities", headers=non_admin_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/pbs-instances/{instance_id}/datastores/store1/prune-jobs", json={}, headers=non_admin_headers
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/pbs-instances/{instance_id}/prune-jobs/x", headers=non_admin_headers).status_code == 403
    )
    assert (
        client.get(
            f"/api/v1/pbs-instances/{instance_id}/verify-jobs/capabilities", headers=non_admin_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/pbs-instances/{instance_id}/datastores/store1/verify-jobs", json={}, headers=non_admin_headers
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/pbs-instances/{instance_id}/verify-jobs/x", headers=non_admin_headers).status_code
        == 403
    )


# --- Remotes ---------------------------------------------------------------


def test_remote_capabilities_reports_real_grants(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PermissionsResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(reachable=True, privileges={"/": {"Remote.Audit": 1, "Remote.Modify": 1}}),
    )

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/remotes/capabilities", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == {"can_read": True, "can_write": True, "missing_write_privileges": []}


def test_list_pbs_remotes_omits_password(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import RemoteListResult, RemoteRemote

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "list_remotes",
        lambda *a, **kw: RemoteListResult(
            reachable=True, remotes=[RemoteRemote(name="offsite", host="pbs2.example.com", auth_id="sync@pbs!token")]
        ),
    )

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/remotes", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "name": "offsite",
            "host": "pbs2.example.com",
            "auth_id": "sync@pbs!token",
            "port": None,
            "fingerprint": None,
            "comment": None,
        }
    ]
    assert "password" not in body[0]


def test_create_pbs_remote_forwards_password_and_audits_without_it(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import RemoteListResult, RemoteRemote
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    create_kwargs = {}

    def fake_create(*a, **kw):
        create_kwargs.update(kw)

    monkeypatch.setattr(pbs_jobs_module, "create_remote", fake_create)
    monkeypatch.setattr(
        pbs_jobs_module,
        "list_remotes",
        lambda *a, **kw: RemoteListResult(
            reachable=True, remotes=[RemoteRemote(name="offsite", host="pbs2.example.com", auth_id="sync@pbs!token")]
        ),
    )

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/remotes",
        json={"name": "offsite", "host": "pbs2.example.com", "auth_id": "sync@pbs!token", "password": "s3cret"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert "password" not in response.json()
    assert create_kwargs["password"] == "s3cret"

    event = db_session.query(AuditEvent).filter_by(action="pbs_remote_created").one()
    assert event.detail == {"name": "offsite", "host": "pbs2.example.com"}
    assert "password" not in str(event.detail)


def test_create_pbs_remote_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 400: parameter verification failed")

    monkeypatch.setattr(pbs_jobs_module, "create_remote", _raise)

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/remotes",
        json={"name": "offsite", "host": "pbs2.example.com", "auth_id": "sync@pbs!token", "password": "s3cret"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_write_failed"


def test_update_pbs_remote_sends_only_changed_fields_and_audit_excludes_password(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import RemoteListResult, RemoteRemote
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    update_calls = []
    monkeypatch.setattr(
        pbs_jobs_module, "update_remote", lambda *a, name, changes, **kw: update_calls.append((name, changes))
    )
    monkeypatch.setattr(
        pbs_jobs_module,
        "list_remotes",
        lambda *a, **kw: RemoteListResult(
            reachable=True, remotes=[RemoteRemote(name="offsite", host="pbs2-new.example.com")]
        ),
    )

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/remotes/offsite",
        json={"host": "pbs2-new.example.com", "password": "newsecret"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert update_calls == [("offsite", {"host": "pbs2-new.example.com", "password": "newsecret"})]

    event = db_session.query(AuditEvent).filter_by(action="pbs_remote_updated").one()
    assert event.detail == {"name": "offsite", "changed_fields": ["host"]}


def test_delete_pbs_remote_audits_and_returns_204(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    delete_calls = []
    monkeypatch.setattr(pbs_jobs_module, "delete_remote", lambda *a, name, **kw: delete_calls.append(name))

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/remotes/offsite", headers=platform_admin_headers)
    assert response.status_code == 204
    assert delete_calls == ["offsite"]

    event = db_session.query(AuditEvent).filter_by(action="pbs_remote_deleted").one()
    assert event.detail == {"name": "offsite"}


# --- Sync jobs ---------------------------------------------------------------


def test_sync_job_capabilities_reports_real_grants(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PermissionsResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(
            reachable=True, privileges={"/": {"Datastore.Audit": 1, "Datastore.Backup": 1, "Remote.Read": 1}}
        ),
    )

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/capabilities", headers=platform_admin_headers)
    assert response.json() == {"can_read": True, "can_write": True, "missing_write_privileges": []}


def test_sync_job_write_needs_remote_read_not_just_audit(client, platform_admin_headers, monkeypatch):
    # PBS api-viewer: a pull sync job reads from the remote, which needs
    # Remote.Read -- Remote.Audit only lets you see that the remote exists.
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import PermissionsResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(
            reachable=True, privileges={"/": {"Datastore.Audit": 1, "Datastore.Backup": 1, "Remote.Audit": 1}}
        ),
    )

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/capabilities", headers=platform_admin_headers)
    assert response.json() == {"can_read": True, "can_write": False, "missing_write_privileges": ["Remote.Read"]}


def test_list_datastore_sync_jobs_filters_by_local_store(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import RemoteSyncJob, SyncJobListResult

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_jobs_module,
        "list_sync_jobs",
        lambda *a, **kw: SyncJobListResult(
            reachable=True,
            sync_jobs=[
                RemoteSyncJob(job_id="s1", local_store="store1", remote_store="remote-store1", remote_name="offsite"),
                RemoteSyncJob(job_id="s2", local_store="other-store", remote_store="remote-store2"),
            ],
        ),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/sync-jobs", headers=platform_admin_headers
    )
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "s1"


def test_get_pbs_sync_job_not_found_returns_404(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    monkeypatch.setattr(pbs_jobs_module, "get_sync_job", lambda *a, **kw: None)

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/nope", headers=platform_admin_headers)
    assert response.status_code == 404


def test_create_sync_job_generates_id_and_audits(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import RemoteSyncJob
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    create_calls = []
    monkeypatch.setattr(pbs_jobs_module, "create_sync_job", lambda *a, job, **kw: create_calls.append(job))
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_sync_job",
        lambda *a, job_id, **kw: RemoteSyncJob(
            job_id=job_id, local_store="store1", remote_store="remote-store1", remote_name="offsite"
        ),
    )

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/sync-jobs",
        json={"remote_store": "remote-store1", "remote_name": "offsite"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["remote_store"] == "remote-store1"
    assert create_calls[0].job_id.startswith("resurgam-")
    assert create_calls[0].local_store == "store1"

    event = db_session.query(AuditEvent).filter_by(action="pbs_sync_job_created").one()
    assert event.detail["store"] == "store1"


def test_create_sync_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 400: parameter verification failed")

    monkeypatch.setattr(pbs_jobs_module, "create_sync_job", _raise)

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/sync-jobs",
        json={"remote_store": "remote-store1"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_write_failed"


def test_update_sync_job_sends_only_changed_fields(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.models import RemoteSyncJob
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    update_calls = []
    monkeypatch.setattr(
        pbs_jobs_module, "update_sync_job", lambda *a, job_id, changes, **kw: update_calls.append(changes)
    )
    monkeypatch.setattr(
        pbs_jobs_module,
        "get_sync_job",
        lambda *a, **kw: RemoteSyncJob(
            job_id="s1", local_store="store1", remote_store="remote-store1", schedule="04:00"
        ),
    )

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1", json={"schedule": "04:00"}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert update_calls == [{"schedule": "04:00"}]

    event = db_session.query(AuditEvent).filter_by(action="pbs_sync_job_updated").one()
    assert event.detail == {"job_id": "s1", "changed_fields": ["schedule"]}


def test_update_sync_job_no_fields_returns_400(client, platform_admin_headers):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.put(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1", json={}, headers=platform_admin_headers)
    assert response.status_code == 400
    assert response.json()["code"] == "no_changes"


def test_delete_sync_job_audits_and_returns_204(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    delete_calls = []
    monkeypatch.setattr(pbs_jobs_module, "delete_sync_job", lambda *a, job_id, **kw: delete_calls.append(job_id))

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1", headers=platform_admin_headers)
    assert response.status_code == 204
    assert delete_calls == ["s1"]

    event = db_session.query(AuditEvent).filter_by(action="pbs_sync_job_deleted").one()
    assert event.detail == {"job_id": "s1"}


def test_delete_sync_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_jobs as pbs_jobs_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 400: no such job")

    monkeypatch.setattr(pbs_jobs_module, "delete_sync_job", _raise)

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1", headers=platform_admin_headers)
    assert response.status_code == 502


def test_remote_and_sync_job_endpoints_require_platform_admin(
    client, platform_admin_headers, db_session, test_settings
):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    instance_id = _create_pbs_instance(client, platform_admin_headers)

    user = User(email="viewer-pbs-remotes@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()
    viewer_role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=viewer_role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    non_admin_headers = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/api/v1/pbs-instances/{instance_id}/remotes", headers=non_admin_headers).status_code == 403
    assert (
        client.post(f"/api/v1/pbs-instances/{instance_id}/remotes", json={}, headers=non_admin_headers).status_code
        == 403
    )
    assert client.delete(f"/api/v1/pbs-instances/{instance_id}/remotes/x", headers=non_admin_headers).status_code == 403
    assert (
        client.get(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/capabilities", headers=non_admin_headers).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/pbs-instances/{instance_id}/datastores/store1/sync-jobs", json={}, headers=non_admin_headers
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/x", headers=non_admin_headers).status_code == 403
    )
