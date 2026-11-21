import httpx

from tests.proxmox_fakes import FakeProxmox, FakeResponse, all_privileges


def _create_ca_mode_payload(**overrides):
    payload = {
        "display_name": "Primary PBS",
        "location": "DC1",
        "host": "pbs1.internal.example",
        "port": 8007,
        "tls_trust_mode": "ca",
        "api_token": "test@pbs!token:secretvalue",
    }
    payload.update(overrides)
    return payload


def test_create_list_get_pbs_instance(client, platform_admin_headers):
    create_response = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    assert create_response.status_code == 200
    body = create_response.json()
    assert body["display_name"] == "Primary PBS"
    assert body["last_health_status"] == "unknown"
    assert "api_token" not in body

    instance_id = body["id"]

    list_response = client.get("/api/v1/pbs-instances", headers=platform_admin_headers)
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    get_response = client.get(f"/api/v1/pbs-instances/{instance_id}", headers=platform_admin_headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == instance_id


def test_create_requires_authentication(client):
    response = client.post("/api/v1/pbs-instances", json=_create_ca_mode_payload())
    assert response.status_code == 401


def test_non_platform_admin_role_is_forbidden(client, db_session):
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, UserRoleAssignment

    login = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": "test-admin", "password": "test-password"},
    )
    token = login.json()["access_token"]
    user_id = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}).json()["user_id"]

    # Break-glass always holds platform_admin (ADR 0028); this test needs a plain viewer.
    db_session.query(UserRoleAssignment).filter_by(user_id=user_id).delete()
    viewer_role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user_id, role_id=viewer_role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()

    response = client.post(
        "/api/v1/pbs-instances",
        json=_create_ca_mode_payload(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_fingerprint_mode_requires_fingerprint(client, platform_admin_headers):
    response = client.post(
        "/api/v1/pbs-instances",
        json=_create_ca_mode_payload(tls_trust_mode="fingerprint", tls_fingerprint_sha256=None),
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_create_with_matching_pinned_fingerprint_succeeds(client, platform_admin_headers, local_tls_server):
    host, port, fingerprint = local_tls_server

    response = client.post(
        "/api/v1/pbs-instances",
        json=_create_ca_mode_payload(
            host=host,
            port=port,
            tls_trust_mode="fingerprint",
            tls_fingerprint_sha256=fingerprint,
        ),
        headers=platform_admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["tls_fingerprint_sha256"] == fingerprint


def test_create_with_wrong_confirmed_fingerprint_is_rejected(client, platform_admin_headers, local_tls_server):
    host, port, _real_fingerprint = local_tls_server

    response = client.post(
        "/api/v1/pbs-instances",
        json=_create_ca_mode_payload(
            host=host,
            port=port,
            tls_trust_mode="fingerprint",
            tls_fingerprint_sha256="0" * 64,
        ),
        headers=platform_admin_headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "tls_fingerprint_mismatch"

    listed = client.get("/api/v1/pbs-instances", headers=platform_admin_headers)
    assert listed.json() == []  # nothing persisted on rejection


def test_probe_tls_returns_observed_fingerprint(client, platform_admin_headers, local_tls_server):
    host, port, expected_fingerprint = local_tls_server

    response = client.post(
        "/api/v1/pbs-instances/probe-tls", json={"host": host, "port": port}, headers=platform_admin_headers
    )

    assert response.status_code == 200
    assert response.json()["fingerprint_sha256"] == expected_fingerprint


def test_test_connection_updates_health_status(client, platform_admin_headers, monkeypatch):
    create_response = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    instance_id = create_response.json()["id"]

    FakeProxmox(
        routes={
            ("GET", "/api2/json/version"): FakeResponse({"data": {"version": "4.2.1"}}),
            ("GET", "/api2/json/access/permissions"): FakeResponse({"data": all_privileges("pbs")}),
        }
    ).install(monkeypatch)

    response = client.post(f"/api/v1/pbs-instances/{instance_id}/test-connection", headers=platform_admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["status"] == "ok"
    assert body["version"] == "4.2.1"
    assert body["error"] is None
    assert body["missing_privileges"] == []
    assert body["permissions_checked"] is True

    get_response = client.get(f"/api/v1/pbs-instances/{instance_id}", headers=platform_admin_headers)
    body = get_response.json()
    assert body["last_health_status"] == "ok"
    assert body["version"] == "4.2.1"
    assert body["last_health_check_at"] is not None


def test_test_connection_marks_unreachable_on_failure(client, platform_admin_headers, monkeypatch):
    create_response = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    instance_id = create_response.json()["id"]

    FakeProxmox(raise_on={("GET", "/api2/json/version"): httpx.ConnectError("[Errno 111] Connection refused")}).install(
        monkeypatch
    )

    response = client.post(f"/api/v1/pbs-instances/{instance_id}/test-connection", headers=platform_admin_headers)

    assert response.status_code == 200
    assert response.json()["reachable"] is False

    get_response = client.get(f"/api/v1/pbs-instances/{instance_id}", headers=platform_admin_headers)
    assert get_response.json()["last_health_status"] == "unreachable"


def test_disable_and_enable(client, platform_admin_headers):
    create_response = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    instance_id = create_response.json()["id"]

    disable_response = client.post(f"/api/v1/pbs-instances/{instance_id}/disable", headers=platform_admin_headers)
    assert disable_response.json()["disabled"] is True

    enable_response = client.post(f"/api/v1/pbs-instances/{instance_id}/enable", headers=platform_admin_headers)
    assert enable_response.json()["disabled"] is False


def test_delete_removes_instance_and_secret(client, platform_admin_headers, db_session):
    create_response = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    instance_id = create_response.json()["id"]

    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import LocalSecretValue, SecretReference

    instance = db_session.get(PbsInstance, instance_id)
    reference_key = db_session.get(SecretReference, instance.api_token_secret_ref_id).reference_key

    delete_response = client.delete(f"/api/v1/pbs-instances/{instance_id}", headers=platform_admin_headers)
    assert delete_response.status_code == 204

    assert db_session.get(PbsInstance, instance_id) is None
    assert db_session.get(LocalSecretValue, reference_key) is None

    get_response = client.get(f"/api/v1/pbs-instances/{instance_id}", headers=platform_admin_headers)
    assert get_response.status_code == 404


def test_get_nonexistent_instance_returns_404(client, platform_admin_headers):
    response = client.get("/api/v1/pbs-instances/00000000-0000-0000-0000-000000000000", headers=platform_admin_headers)
    assert response.status_code == 404


def test_update_changes_only_given_fields(client, platform_admin_headers):
    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    response = client.patch(
        f"/api/v1/pbs-instances/{created['id']}",
        json={"display_name": "Renamed PBS"},
        headers=platform_admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Renamed PBS"
    assert body["host"] == created["host"]
    assert body["port"] == created["port"]


def test_update_rotates_api_token_secret(client, platform_admin_headers, db_session, test_settings):
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.models.secrets import SecretReference
    from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    response = client.patch(
        f"/api/v1/pbs-instances/{created['id']}",
        json={"api_token": "new@pbs!token:newsecret"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200

    instance = db_session.get(PbsInstance, created["id"])
    secret_ref = db_session.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key)
    assert provider.retrieve(secret_ref.reference_key) == "new@pbs!token:newsecret"


def test_update_to_fingerprint_mode_reverifies(client, platform_admin_headers, local_tls_server):
    host, port, fingerprint = local_tls_server
    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    ok_response = client.patch(
        f"/api/v1/pbs-instances/{created['id']}",
        json={"host": host, "port": port, "tls_trust_mode": "fingerprint", "tls_fingerprint_sha256": fingerprint},
        headers=platform_admin_headers,
    )
    assert ok_response.status_code == 200
    assert ok_response.json()["tls_fingerprint_sha256"] == fingerprint

    bad_response = client.patch(
        f"/api/v1/pbs-instances/{created['id']}",
        json={"tls_fingerprint_sha256": "0" * 64, "host": host, "port": port},
        headers=platform_admin_headers,
    )
    assert bad_response.status_code == 409
    assert bad_response.json()["code"] == "tls_fingerprint_mismatch"


def test_update_nonexistent_instance_returns_404(client, platform_admin_headers):
    response = client.patch(
        "/api/v1/pbs-instances/00000000-0000-0000-0000-000000000000",
        json={"display_name": "x"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404


def test_update_requires_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    viewer_headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        "/api/v1/pbs-instances/00000000-0000-0000-0000-000000000000",
        json={"display_name": "x"},
        headers=viewer_headers,
    )
    assert response.status_code == 403


# --- GET /{instance_id}/tasks/{upid}/log (Jobs page log modal) -------------


def _create_instance(client, headers) -> str:
    response = client.post("/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def test_task_log_endpoint_returns_lines(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import TaskLogLine, TaskLogResult

    instance_id = _create_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_instances_module,
        "get_task_log",
        lambda *a, **kw: TaskLogResult(
            reachable=True, lines=[TaskLogLine(n=1, text="line one"), TaskLogLine(n=2, text="TASK OK")], total=2
        ),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/tasks/UPID:pbs:1:backup:root@pam:/log", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lines"] == [{"n": 1, "text": "line one"}, {"n": 2, "text": "TASK OK"}]
    assert body["truncated"] is False


def test_task_log_endpoint_returns_502_when_unreachable(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import TaskLogResult

    instance_id = _create_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_instances_module,
        "get_task_log",
        lambda *a, **kw: TaskLogResult(reachable=False, error="connection refused"),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/tasks/UPID:pbs:1:backup:root@pam:/log", headers=platform_admin_headers
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_unreachable"


def test_task_log_endpoint_reports_truncated(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import TaskLogLine, TaskLogResult

    instance_id = _create_instance(client, platform_admin_headers)
    monkeypatch.setattr(
        pbs_instances_module,
        "get_task_log",
        lambda *a, **kw: TaskLogResult(
            reachable=True, lines=[TaskLogLine(n=1, text="line")], total=9999, truncated=True
        ),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/tasks/UPID:pbs:1:backup:root@pam:/log", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["truncated"] is True


def test_task_log_endpoint_requires_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer2@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    viewer_headers = {"Authorization": f"Bearer {token}"}

    response = client.get(
        "/api/v1/pbs-instances/00000000-0000-0000-0000-000000000000/tasks/UPID:pbs:1:backup:root@pam:/log",
        headers=viewer_headers,
    )
    assert response.status_code == 403


def test_get_datastore_maintenance_mode_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import DatastoreMaintenanceResult

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    monkeypatch.setattr(
        pbs_instances_module,
        "get_datastore_maintenance_mode",
        lambda *a, **kw: DatastoreMaintenanceResult(reachable=True, mode="read-only", message="planned work"),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/maintenance-mode", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json() == {"mode": "read-only", "message": "planned work"}


def test_get_datastore_maintenance_mode_unreachable_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import DatastoreMaintenanceResult

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    monkeypatch.setattr(
        pbs_instances_module,
        "get_datastore_maintenance_mode",
        lambda *a, **kw: DatastoreMaintenanceResult(reachable=False, error="timeout"),
    )

    response = client.get(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/maintenance-mode", headers=platform_admin_headers
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_unreachable"


def test_set_datastore_maintenance_mode_calls_pbs_and_records_audit(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.models.audit import AuditEvent

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    calls = []
    monkeypatch.setattr(
        pbs_instances_module,
        "set_datastore_maintenance_mode",
        lambda *a, name, mode, message, **kw: calls.append((name, mode, message)),
    )

    response = client.put(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/maintenance-mode",
        json={"mode": "read-only", "message": "planned work"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"mode": "read-only", "message": "planned work"}
    assert calls == [("store1", "read-only", "planned work")]

    event = db_session.query(AuditEvent).filter_by(action="pbs_datastore_maintenance_mode_set").one()
    assert event.detail == {"datastore": "store1", "mode": "read-only", "message": "planned work"}


def test_clear_datastore_maintenance_mode_records_clear_audit(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.models.audit import AuditEvent

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    monkeypatch.setattr(pbs_instances_module, "set_datastore_maintenance_mode", lambda *a, **kw: None)

    response = client.put(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/maintenance-mode",
        json={"mode": None},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"mode": None, "message": None}

    event = db_session.query(AuditEvent).filter_by(action="pbs_datastore_maintenance_mode_cleared").one()
    assert event.detail == {"datastore": "store1", "mode": None, "message": None}


def test_set_datastore_maintenance_mode_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 403: Permission check failed (Datastore.Modify)")

    monkeypatch.setattr(pbs_instances_module, "set_datastore_maintenance_mode", _raise)

    response = client.put(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/maintenance-mode",
        json={"mode": "offline"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_write_failed"


def test_datastore_maintenance_mode_endpoints_require_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer3@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    viewer_headers = {"Authorization": f"Bearer {token}"}

    fake_id = "00000000-0000-0000-0000-000000000000"
    assert (
        client.get(
            f"/api/v1/pbs-instances/{fake_id}/datastores/store1/maintenance-mode", headers=viewer_headers
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/pbs-instances/{fake_id}/datastores/store1/maintenance-mode",
            json={"mode": "offline"},
            headers=viewer_headers,
        ).status_code
        == 403
    )


# --- GC schedule (docs/adr/0023-backup-and-maintenance-job-administration) -----------------------------
#
# Not a job list -- just a bare `gc-schedule` field on the datastore's own
# PBS config, same underlying config-item endpoint as maintenance-mode.


def test_get_datastore_gc_schedule_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    monkeypatch.setattr(pbs_instances_module, "get_datastore_gc_schedule", lambda *a, **kw: (True, "03:00", None))

    response = client.get(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/gc-schedule", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json() == {"gc_schedule": "03:00"}


def test_get_datastore_gc_schedule_unreachable_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    monkeypatch.setattr(pbs_instances_module, "get_datastore_gc_schedule", lambda *a, **kw: (False, None, "timeout"))

    response = client.get(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/gc-schedule", headers=platform_admin_headers
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_unreachable"


def test_set_datastore_gc_schedule_calls_pbs_and_records_audit(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.models.audit import AuditEvent

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    calls = []
    monkeypatch.setattr(
        pbs_instances_module,
        "set_datastore_gc_schedule",
        lambda *a, name, schedule, **kw: calls.append((name, schedule)),
    )

    response = client.put(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/gc-schedule",
        json={"gc_schedule": "03:00"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"gc_schedule": "03:00"}
    assert calls == [("store1", "03:00")]

    event = db_session.query(AuditEvent).filter_by(action="pbs_datastore_gc_schedule_set").one()
    assert event.detail == {"datastore": "store1", "gc_schedule": "03:00"}


def test_clear_datastore_gc_schedule_records_clear_audit(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.models.audit import AuditEvent

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    monkeypatch.setattr(pbs_instances_module, "set_datastore_gc_schedule", lambda *a, **kw: None)

    response = client.put(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/gc-schedule",
        json={"gc_schedule": None},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"gc_schedule": None}

    event = db_session.query(AuditEvent).filter_by(action="pbs_datastore_gc_schedule_cleared").one()
    assert event.detail == {"datastore": "store1", "gc_schedule": None}


def test_set_datastore_gc_schedule_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.pbs_api import PbsWriteError

    created = client.post(
        "/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    def _raise(*a, **kw):
        raise PbsWriteError("HTTP 403: Permission check failed (Datastore.Modify)")

    monkeypatch.setattr(pbs_instances_module, "set_datastore_gc_schedule", _raise)

    response = client.put(
        f"/api/v1/pbs-instances/{created['id']}/datastores/store1/gc-schedule",
        json={"gc_schedule": "03:00"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_write_failed"


def test_datastore_gc_schedule_endpoints_require_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer-gc-schedule@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    viewer_headers = {"Authorization": f"Bearer {token}"}

    fake_id = "00000000-0000-0000-0000-000000000000"
    assert (
        client.get(f"/api/v1/pbs-instances/{fake_id}/datastores/store1/gc-schedule", headers=viewer_headers).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/pbs-instances/{fake_id}/datastores/store1/gc-schedule",
            json={"gc_schedule": "03:00"},
            headers=viewer_headers,
        ).status_code
        == 403
    )


def test_create_imports_the_pbs_datastores_right_away(client, platform_admin_headers, monkeypatch):
    from resurgam_api.collector import sync as collector_sync_module
    from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore

    monkeypatch.setattr(
        collector_sync_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(
            reachable=True, datastores=[RemoteDatastore(name="store-a", path="/mnt/a"), RemoteDatastore(name="store-b")]
        ),
    )
    response = client.post("/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers)
    assert response.status_code == 200

    datastores = client.get("/api/v1/datastores", headers=platform_admin_headers).json()
    assert sorted(d["name"] for d in datastores) == ["store-a", "store-b"]


def test_create_survives_a_discovery_crash(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    def boom(*a, **kw):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(pbs_instances_module, "discover_datastores", boom)
    response = client.post("/api/v1/pbs-instances", json=_create_ca_mode_payload(), headers=platform_admin_headers)
    assert response.status_code == 200
    assert len(client.get("/api/v1/pbs-instances", headers=platform_admin_headers).json()) == 1
