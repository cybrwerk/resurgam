from datetime import UTC, datetime, timedelta

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


def _create_pbs_instance(client, headers, display_name="PBS") -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": display_name,
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "test@pbs!token:secretvalue",
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


def _add_measurement(db_session, datastore_id, captured_at, used_bytes, total_bytes=1000):
    from resurgam_api.models.usage import UsageMeasurement

    measurement = UsageMeasurement(
        datastore_id=datastore_id,
        captured_at=captured_at,
        used_bytes=used_bytes,
        available_bytes=total_bytes - used_bytes,
        total_bytes=total_bytes,
    )
    db_session.add(measurement)
    db_session.flush()
    db_session.commit()
    return measurement


def _add_task(db_session, instance_id, task_type, status, started_at, upid_suffix="1"):
    from resurgam_api.models.inventory import TaskRun

    task = TaskRun(
        pbs_instance_id=instance_id,
        upid=f"UPID:pbs:{upid_suffix}:{task_type}:root@pam:",
        task_type=task_type,
        status=status,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=5) if status != "running" else None,
    )
    db_session.add(task)
    db_session.flush()
    db_session.commit()
    return task


def test_discover_datastores_returns_zabbix_lld_shaped_macros(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="store1")

    response = client.get("/api/v1/zabbix/discovery/datastores", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "data": [
            {
                "{#DATASTORE_ID}": datastore_id,
                "{#DATASTORE_NAME}": "store1",
                "{#PBS_INSTANCE_ID}": instance_id,
                "{#PBS_INSTANCE_NAME}": "PBS",
            }
        ]
    }


def test_discover_datastores_empty_when_none_imported(client, platform_admin_headers):
    response = client.get("/api/v1/zabbix/discovery/datastores", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == {"data": []}


def test_datastore_usage_returns_latest_measurement_only(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    now = datetime.now(UTC)
    _add_measurement(db_session, datastore_id, now - timedelta(hours=1), used_bytes=200)
    _add_measurement(db_session, datastore_id, now, used_bytes=500)

    response = client.get("/api/v1/zabbix/datastores/usage", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["datastores"]) == 1
    entry = body["datastores"][0]
    assert entry["datastore_id"] == datastore_id
    assert entry["used_bytes"] == 500
    assert entry["usage_percent"] == 50.0


def test_datastore_usage_omits_datastores_with_no_measurement(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)

    response = client.get("/api/v1/zabbix/datastores/usage", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == {"datastores": []}


def test_discover_jobs_returns_one_entry_per_instance_and_task_type(client, platform_admin_headers, db_session):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    now = datetime.now(UTC)
    _add_task(db_session, instance_id, "backup", "ok", now, upid_suffix="1")
    _add_task(db_session, instance_id, "backup", "ok", now - timedelta(days=1), upid_suffix="2")  # same pair, dedup
    _add_task(db_session, instance_id, "verify", "error", now, upid_suffix="3")

    response = client.get("/api/v1/zabbix/discovery/jobs", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "data": [
            {"{#PBS_INSTANCE_ID}": instance_id, "{#PBS_INSTANCE_NAME}": "PBS", "{#TASK_TYPE}": "backup"},
            {"{#PBS_INSTANCE_ID}": instance_id, "{#PBS_INSTANCE_NAME}": "PBS", "{#TASK_TYPE}": "verify"},
        ]
    }


def test_job_status_reports_the_most_recent_task_per_pair(client, platform_admin_headers, db_session):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    now = datetime.now(UTC)
    _add_task(db_session, instance_id, "backup", "error", now - timedelta(hours=2), upid_suffix="1")
    _add_task(db_session, instance_id, "backup", "ok", now, upid_suffix="2")  # more recent -- this one should win

    response = client.get("/api/v1/zabbix/jobs/status", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 1
    job = body["jobs"][0]
    assert job["pbs_instance_id"] == instance_id
    assert job["task_type"] == "backup"
    assert job["status"] == "ok"
    assert job["age_seconds"] < 5


def test_job_status_reports_null_ended_at_for_a_running_task(client, platform_admin_headers, db_session):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    _add_task(db_session, instance_id, "backup", "running", datetime.now(UTC))

    response = client.get("/api/v1/zabbix/jobs/status", headers=platform_admin_headers)
    assert response.status_code == 200
    job = response.json()["jobs"][0]
    assert job["status"] == "running"
    assert job["last_ended_at"] is None


def test_zabbix_endpoints_require_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer-zabbix@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    for path in [
        "/api/v1/zabbix/discovery/datastores",
        "/api/v1/zabbix/datastores/usage",
        "/api/v1/zabbix/discovery/jobs",
        "/api/v1/zabbix/jobs/status",
    ]:
        response = client.get(path, headers=headers)
        assert response.status_code == 403, path
