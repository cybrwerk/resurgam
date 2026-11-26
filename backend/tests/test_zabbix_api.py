import uuid
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
    assert 0 <= entry["age_seconds"] < 60
    # One hour of history: no forecast yet, sent as -1 so a trigger can resolve.
    assert entry["growth_bytes_per_day"] is None
    assert entry["days_until_full"] == -1.0


def test_datastore_usage_includes_the_capacity_forecast(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    now = datetime.now(UTC)
    for day in range(10):
        _add_measurement(db_session, datastore_id, now - timedelta(days=9 - day, minutes=5), used_bytes=400 + 10 * day)

    entry = client.get("/api/v1/zabbix/datastores/usage", headers=platform_admin_headers).json()["datastores"][0]
    assert entry["growth_bytes_per_day"] == 10
    assert entry["days_until_full"] == 51.0  # 510 bytes left at 10 per day


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
        "/api/v1/zabbix/setup",
        "/api/v1/zabbix/template",
        "/api/v1/zabbix/ca-certificate",
    ]:
        response = client.get(path, headers=headers)
        assert response.status_code == 403, path


def _template(text: str) -> dict:
    import yaml

    return yaml.safe_load(text)["zabbix_export"]["templates"][0]


def test_template_download_presets_the_appliance_address(client, platform_admin_headers, test_settings):
    test_settings.frontend_origin = "https://resurgam.example.internal/"
    response = client.get("/api/v1/zabbix/template", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert "zabbix_template_resurgam.yaml" in response.headers["content-disposition"]
    template = _template(response.text)
    macros = {m["macro"]: m for m in template["macros"]}
    assert macros["{$RESURGAM.URL}"]["value"] == "https://resurgam.example.internal"
    assert macros["{$RESURGAM.TOKEN}"]["type"] == "SECRET_TEXT"
    assert "value" not in macros["{$RESURGAM.TOKEN}"]


def test_template_reads_only_fields_the_api_provides():
    """Every value the template's JSONPath steps read and every LLD macro it
    uses must exist in the API -- otherwise Zabbix silently gets nothing."""
    import re

    from resurgam_api import monitoring
    from resurgam_api.schemas import zabbix as schemas

    template = _template(monitoring.template_yaml())
    assert template["name"] == monitoring.TEMPLATE_NAME
    assert template["vendor"]["version"] == monitoring.TEMPLATE_REVISION

    fields = {
        "datastores": set(schemas.ZabbixDatastoreUsageValue.model_fields),
        "jobs": set(schemas.ZabbixJobStatusValue.model_fields),
    }
    lld_macros = {
        "resurgam.datastores.discovery": {f.alias for f in schemas.ZabbixDatastoreDiscoveryEntry.model_fields.values()},
        "resurgam.jobs.discovery": {f.alias for f in schemas.ZabbixJobDiscoveryEntry.model_fields.values()},
    }
    checked = 0
    for rule in template["discovery_rules"]:
        rule_text = str(rule)
        used = set(re.findall(r"\{#[A-Z_]+\}", rule_text))
        assert used <= lld_macros[rule["key"]], (rule["key"], used - lld_macros[rule["key"]])
        for prototype in rule["item_prototypes"]:
            path = prototype["preprocessing"][0]["parameters"][0]
            match = re.match(r"\$\.(\w+)\[\?\(.*\)\]\.(\w+)\.first\(\)$", path)
            assert match, path
            collection, field = match.groups()
            assert field in fields[collection], (collection, field)
            for key in re.findall(r"@\.(\w+)", path):
                assert key in fields[collection], (collection, key)
            checked += 1
    assert checked == 9

    # Dependent items on the 3-2-1 master item read top-level fields.
    gap_fields = set(schemas.ZabbixBackupGapsOut.model_fields)
    dependent = [i for i in template["items"] if i.get("master_item", {}).get("key") == "resurgam.backup_gaps"]
    assert len(dependent) == 4
    for item in dependent:
        path = item["preprocessing"][0]["parameters"][0]
        assert re.match(r"\$\.(\w+)$", path), path
        assert path[2:] in gap_fields, path


def test_setup_summarizes_what_zabbix_will_discover(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    test_settings.frontend_origin = "https://resurgam.example.internal"
    test_settings.appliance_tls_ca_file = "/nonexistent/root.crt"
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="store2")
    _add_measurement(db_session, datastore_id, datetime.now(UTC), used_bytes=10)
    now = datetime.now(UTC)
    _add_task(db_session, instance_id, "backup", "ok", now, upid_suffix="1")
    _add_task(db_session, instance_id, "unknown", "ok", now, upid_suffix="2")

    body = client.get("/api/v1/zabbix/setup", headers=platform_admin_headers).json()
    assert body["appliance_url"] == "https://resurgam.example.internal"
    assert body["template_name"] == "RESURGAM by HTTP"
    assert (body["datastores"], body["datastores_with_usage"], body["job_types"]) == (2, 1, 1)
    assert body["ca_certificate_available"] is False


def test_ca_certificate_download(client, platform_admin_headers, test_settings, tmp_path):
    test_settings.appliance_tls_ca_file = str(tmp_path / "missing.crt")
    missing = client.get("/api/v1/zabbix/ca-certificate", headers=platform_admin_headers)
    assert (missing.status_code, missing.json()["code"]) == (404, "ca_certificate_unavailable")

    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    (tmp_path / "root.crt").write_text(pem)
    test_settings.appliance_tls_ca_file = str(tmp_path / "root.crt")
    response = client.get("/api/v1/zabbix/ca-certificate", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.text == pem


def test_backup_gaps_counts(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    response = client.get("/api/v1/zabbix/backup-gaps", headers=platform_admin_headers)
    assert response.json() == {
        "unprotected_guests": 0,
        "offsite_gaps": 0,
        "offsite_critical": 0,
        "offsite_max_lag_hours": -1,
        "excepted": 0,
    }

    group = BackupGroup(datastore_id=uuid.UUID(datastore_id), backup_type="vm", backup_id="100")
    db_session.add(group)
    db_session.flush()
    db_session.add(BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime.now(UTC)))
    db_session.commit()
    # No sync job covers it: listed, but no gap while no policy requires Sync/DR.
    assert client.get("/api/v1/zabbix/backup-gaps", headers=platform_admin_headers).json()["offsite_gaps"] == 0

    from resurgam_api.models.inventory import ProtectionPolicy
    from resurgam_api.models.tenancy import Customer

    customer = Customer(name="Acme", slug="acme", debitor_number="d-acme", match_code="ACM")
    db_session.add(customer)
    db_session.flush()
    policy = ProtectionPolicy(
        customer_id=customer.id,
        name="DR",
        expected_interval_hours=24,
        max_time_since_last_backup_hours=48,
        sync_dr_required=True,
    )
    db_session.add(policy)
    db_session.flush()
    group.protection_policy_id = policy.id
    db_session.commit()
    body = client.get("/api/v1/zabbix/backup-gaps", headers=platform_admin_headers).json()
    assert body["offsite_gaps"] == 1
    assert body["offsite_critical"] == 0
