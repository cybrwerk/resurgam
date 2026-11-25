from datetime import UTC, datetime, timedelta

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


def _create_pbs_instance(client, headers) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
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


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _create_customer_scoped_viewer(db_session, test_settings, customer_id) -> dict:
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email=f"viewer-{customer_id}@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()

    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def _create_group(db_session, datastore_id, backup_id="100", **overrides):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(datastore_id=datastore_id, backup_type="vm", backup_id=backup_id, **overrides)
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _add_measurement(db_session, datastore_id, used_bytes, total_bytes=1000):
    from resurgam_api.models.usage import UsageMeasurement

    measurement = UsageMeasurement(
        datastore_id=datastore_id,
        used_bytes=used_bytes,
        available_bytes=total_bytes - used_bytes,
        total_bytes=total_bytes,
    )
    db_session.add(measurement)
    db_session.flush()
    db_session.commit()
    return measurement


def test_alarms_endpoint_empty_when_nothing_wrong(client, platform_admin_headers):
    response = client.get("/api/v1/alarms", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_protection_critical_alarm_for_never_backed_up_group(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id)},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)
    policy = client.post(
        "/api/v1/protection-policies",
        json={
            "customer_id": str(customer_id),
            "name": "Standard",
            "expected_interval_hours": 24,
            "max_time_since_last_backup_hours": 48,
            "min_snapshot_count": 1,
        },
        headers=platform_admin_headers,
    ).json()
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-policy",
        json={"protection_policy_id": policy["id"]},
        headers=platform_admin_headers,
    )

    response = client.get("/api/v1/alarms", headers=platform_admin_headers)
    assert response.status_code == 200
    alarms = response.json()
    assert len(alarms) == 1
    assert alarms[0]["type"] == "protection_critical"
    assert alarms[0]["severity"] == "critical"
    assert alarms[0]["entity_id"] == str(group_id)
    assert alarms[0]["customer_id"] == str(customer_id)
    assert alarms[0]["customer_name"] == "Acme"
    assert alarms[0]["customer_match_code"] == "ACM"


def test_no_alarm_for_superseded_group(client, platform_admin_headers, db_session, monkeypatch):
    """A reused VMID's closed-out prior generation (BackupGroup.superseded_at
    set) keeps whatever protection_policy_id it had before it closed --
    it must never raise a permanent, never-resolvable overdue-backup alarm.
    """
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id)},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id, superseded_at=datetime.now(UTC))
    policy = client.post(
        "/api/v1/protection-policies",
        json={
            "customer_id": str(customer_id),
            "name": "Standard",
            "expected_interval_hours": 24,
            "max_time_since_last_backup_hours": 48,
            "min_snapshot_count": 1,
        },
        headers=platform_admin_headers,
    ).json()
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-policy",
        json={"protection_policy_id": policy["id"]},
        headers=platform_admin_headers,
    )

    response = client.get("/api/v1/alarms", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_no_alarm_for_healthy_group(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id)},
        headers=platform_admin_headers,
    )
    now = datetime.now(UTC)
    group_id = _create_group(db_session, datastore_id, last_backup_at=now - timedelta(hours=1), last_verify_status="ok")
    policy = client.post(
        "/api/v1/protection-policies",
        json={
            "customer_id": str(customer_id),
            "name": "Standard",
            "expected_interval_hours": 24,
            "max_time_since_last_backup_hours": 48,
            "min_snapshot_count": 0,
        },
        headers=platform_admin_headers,
    ).json()
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-policy",
        json={"protection_policy_id": policy["id"]},
        headers=platform_admin_headers,
    )

    response = client.get("/api/v1/alarms", headers=platform_admin_headers)
    assert response.json() == []


def test_no_alarm_for_group_without_policy(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    _create_group(db_session, datastore_id)

    response = client.get("/api/v1/alarms", headers=platform_admin_headers)
    assert response.json() == []


def test_datastore_usage_alarms_at_thresholds(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    warning_store = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="warn-store")
    critical_store = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="crit-store")
    for store_id in (warning_store, critical_store):
        client.post(
            f"/api/v1/datastores/{store_id}/assign-customer",
            json={"customer_id": str(customer_id)},
            headers=platform_admin_headers,
        )
    _add_measurement(db_session, warning_store, used_bytes=850, total_bytes=1000)  # 85%
    _add_measurement(db_session, critical_store, used_bytes=970, total_bytes=1000)  # 97%

    response = client.get("/api/v1/alarms", headers=platform_admin_headers)
    alarms = {a["entity_id"]: a for a in response.json()}
    assert alarms[warning_store]["type"] == "datastore_usage_warning"
    assert alarms[critical_store]["type"] == "datastore_usage_critical"


def test_datastore_usage_alarm_below_threshold_is_silent(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    _add_measurement(db_session, datastore_id, used_bytes=500, total_bytes=1000)  # 50%

    response = client.get("/api/v1/alarms", headers=platform_admin_headers)
    assert response.json() == []


def test_alarms_respect_customer_scope(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    mine = _create_customer(db_session, "Mine", "mine")
    other = _create_customer(db_session, "Other", "other")
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    my_store = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="my-store")
    other_store = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="other-store")
    client.post(
        f"/api/v1/datastores/{my_store}/assign-customer",
        json={"customer_id": str(mine)},
        headers=platform_admin_headers,
    )
    client.post(
        f"/api/v1/datastores/{other_store}/assign-customer",
        json={"customer_id": str(other)},
        headers=platform_admin_headers,
    )
    _add_measurement(db_session, my_store, used_bytes=990, total_bytes=1000)
    _add_measurement(db_session, other_store, used_bytes=990, total_bytes=1000)

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, mine)
    response = client.get("/api/v1/alarms", headers=viewer_headers)
    alarms = response.json()
    assert len(alarms) == 1
    assert alarms[0]["entity_id"] == my_store


def _add_growth_history(db_session, datastore_id, start_used, per_day, days=10, total_bytes=1000):
    from datetime import UTC, datetime, timedelta

    from resurgam_api.models.usage import UsageMeasurement

    now = datetime.now(UTC)
    for day in range(days):
        used = start_used + per_day * day
        db_session.add(
            UsageMeasurement(
                datastore_id=datastore_id,
                captured_at=now - timedelta(days=days - 1 - day, minutes=5),
                used_bytes=used,
                available_bytes=total_bytes - used,
                total_bytes=total_bytes,
            )
        )
    db_session.commit()


def _alarms_for(client, headers, datastore_id) -> dict[str, dict]:
    response = client.get("/api/v1/alarms", headers=headers)
    return {a["type"]: a for a in response.json() if a["entity_id"] == datastore_id}


def test_full_soon_alarms_from_forecast(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    soon = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="soon")
    later = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="later")
    far = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="far")
    # Latest used 590 -> 410 left. 41/day: 10 days (critical). 15/day: ~27 days (warning). 1/day: no alarm.
    _add_growth_history(db_session, soon, start_used=590 - 9 * 41, per_day=41)
    _add_growth_history(db_session, later, start_used=590 - 9 * 15, per_day=15)
    _add_growth_history(db_session, far, start_used=590 - 9, per_day=1)

    soon_alarms = _alarms_for(client, platform_admin_headers, soon)
    assert set(soon_alarms) == {"datastore_full_soon_critical"}
    assert "full in about 10 days" in soon_alarms["datastore_full_soon_critical"]["message"]
    assert set(_alarms_for(client, platform_admin_headers, later)) == {"datastore_full_soon_warning"}
    assert _alarms_for(client, platform_admin_headers, far) == {}


def test_full_soon_alarm_not_raised_next_to_an_equally_severe_usage_alarm(
    client, platform_admin_headers, db_session, monkeypatch
):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    critical = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="crit")
    warning = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="warn")
    # 97 % used and full in ~3 days: the usage alarm already says it.
    _add_growth_history(db_session, critical, start_used=970 - 9 * 10, per_day=10)
    # 85 % used (warning) but full in ~7 days (critical): both show, the forecast is more urgent.
    _add_growth_history(db_session, warning, start_used=850 - 9 * 20, per_day=20)

    assert set(_alarms_for(client, platform_admin_headers, critical)) == {"datastore_usage_critical"}
    assert set(_alarms_for(client, platform_admin_headers, warning)) == {
        "datastore_usage_warning",
        "datastore_full_soon_critical",
    }
