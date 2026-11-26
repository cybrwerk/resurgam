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


def test_usage_endpoint_returns_latest_and_history(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)

    now = datetime.now(UTC)
    _add_measurement(db_session, datastore_id, now - timedelta(hours=25), used_bytes=400)
    _add_measurement(db_session, datastore_id, now, used_bytes=500)

    response = client.get(f"/api/v1/datastores/{datastore_id}/usage", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["latest"]["used_bytes"] == 500
    assert body["latest"]["usage_percent"] == 50.0
    assert len(body["history"]) == 2
    assert body["daily_change_bytes"] == 100
    assert body["monthly_change_bytes"] is None


def test_usage_endpoint_empty_history(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)

    response = client.get(f"/api/v1/datastores/{datastore_id}/usage", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["latest"] is None
    assert body["history"] == []
    assert body["daily_change_bytes"] is None


def test_usage_endpoint_respects_customer_scope(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    own_datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="mine")
    other_datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="other")

    customer_mine = _create_customer(db_session, "Mine Inc", "mine")
    client.post(
        f"/api/v1/datastores/{own_datastore_id}/assign-customer",
        json={"customer_id": str(customer_mine)},
        headers=platform_admin_headers,
    )
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_mine)

    own_response = client.get(f"/api/v1/datastores/{own_datastore_id}/usage", headers=viewer_headers)
    assert own_response.status_code == 200

    other_response = client.get(f"/api/v1/datastores/{other_datastore_id}/usage", headers=viewer_headers)
    assert other_response.status_code == 404  # not 403 — out of scope, must not confirm existence


def test_usage_endpoint_latest_is_newest_even_with_long_history(
    client, platform_admin_headers, db_session, monkeypatch
):
    # 600 rows (more than the 500-row history cap): `latest` must be the newest one, and
    # the 30-day baseline must still be found although it is outside the returned history.
    from resurgam_api.models.usage import UsageMeasurement

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    now = datetime.now(UTC)
    db_session.add(
        UsageMeasurement(
            datastore_id=datastore_id,
            captured_at=now - timedelta(days=31),
            used_bytes=100,
            available_bytes=900,
            total_bytes=1000,
        )
    )
    for i in range(600):
        db_session.add(
            UsageMeasurement(
                datastore_id=datastore_id,
                captured_at=now - timedelta(minutes=15 * (600 - i)),
                used_bytes=200 + i,
                available_bytes=800 - i,
                total_bytes=1000,
            )
        )
    db_session.commit()

    body = client.get(f"/api/v1/datastores/{datastore_id}/usage", headers=platform_admin_headers).json()
    assert body["latest"]["used_bytes"] == 799
    assert len(body["history"]) == 500
    assert body["history"][-1]["used_bytes"] == 799
    assert body["monthly_change_bytes"] == 699


def test_usage_endpoint_forecast(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    # Two syncs per day at 02:00 and 14:00 UTC on the last 10 full days, +10 bytes per day;
    # the daily peak counts. Fixed times of day, not offsets from now: an offset like
    # "now - 14 h" lands on the previous UTC day before 14:00 and shifts the daily peaks.
    yesterday = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    for day in range(10):
        midnight = yesterday - timedelta(days=9 - day)
        _add_measurement(db_session, datastore_id, midnight + timedelta(hours=2), used_bytes=400 + 10 * day - 1)
        _add_measurement(db_session, datastore_id, midnight + timedelta(hours=14), used_bytes=400 + 10 * day)

    body = client.get(f"/api/v1/datastores/{datastore_id}/usage", headers=platform_admin_headers).json()
    forecast = body["forecast"]
    assert forecast["status"] == "growing"
    assert forecast["growth_bytes_per_day"] == 10
    # latest: used 490 of 1000 -> 510 bytes available at 10 per day.
    assert forecast["days_until_full"] == 51.0


def test_usage_endpoint_forecast_without_history(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    body = client.get(f"/api/v1/datastores/{datastore_id}/usage", headers=platform_admin_headers).json()
    assert body["forecast"] == {
        "status": "insufficient_history",
        "basis_days": 0,
        "growth_bytes_per_day": None,
        "days_until_full": None,
        "full_at": None,
    }
