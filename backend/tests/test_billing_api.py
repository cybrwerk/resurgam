from datetime import UTC

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


def _create_group(db_session, datastore_id, backup_id="100", backup_type="vm", **overrides):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id, **overrides)
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _add_snapshot(db_session, group_id, size_bytes, snapshot_time=None):
    from datetime import datetime

    from resurgam_api.models.inventory import BackupSnapshot

    snapshot = BackupSnapshot(
        backup_group_id=group_id,
        snapshot_time=snapshot_time or datetime.now(UTC),
        size_bytes=size_bytes,
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.commit()
    return snapshot.id


def _add_measurement(db_session, datastore_id, used_bytes, logical_bytes, total_bytes=10_000_000_000):
    from resurgam_api.models.usage import UsageMeasurement

    measurement = UsageMeasurement(
        datastore_id=datastore_id,
        used_bytes=used_bytes,
        available_bytes=total_bytes - used_bytes,
        total_bytes=total_bytes,
        logical_bytes=logical_bytes,
    )
    db_session.add(measurement)
    db_session.flush()
    db_session.commit()
    return measurement


def _plan_payload(**overrides):
    payload = {
        "customer_id": None,
        "name": "Standard",
        "price_per_vm_month": "10.00",
        "price_per_gb_month": "0.0500",
        "currency": "CHF",
    }
    payload.update(overrides)
    return payload


def test_no_data_yields_zero_billing_and_no_plan(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)

    response = client.get(f"/api/v1/billing/customers/{customer_id}", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["vm_count"] == 0
    assert body["estimated_physical_bytes"] == 0
    assert body["physical_bytes_estimate_complete"] is True
    assert body["pricing_plan_id"] is None
    assert body["pricing_plan_source"] is None
    assert body["estimated_monthly_cost"] is None


def test_primary_datastore_counts_vm_and_bytes_with_global_plan(
    client, platform_admin_headers, db_session, monkeypatch
):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)
    _add_snapshot(db_session, group_id, size_bytes=100_000_000_000)  # 100 GB logical
    # Datastore-wide: 100GB logical dedups down to 40GB physical (0.4 ratio)
    _add_measurement(db_session, datastore_id, used_bytes=40_000_000_000, logical_bytes=100_000_000_000)
    client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers)

    response = client.get(f"/api/v1/billing/customers/{customer_id}", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["vm_count"] == 1
    assert body["estimated_physical_bytes"] == 40_000_000_000
    assert body["physical_bytes_estimate_complete"] is True
    assert body["pricing_plan_source"] == "global"
    # 1 VM * 10.00 + 40GB * 0.05/GB = 10.00 + 2.00 = 12.00
    assert body["estimated_monthly_cost"] == "12.00"


def test_superseded_generation_not_double_billed(client, platform_admin_headers, db_session, monkeypatch):
    """A reused VMID's closed-out prior generation (BackupGroup.superseded_at
    set, see collector.sync's split-on-rename behavior) must not mint a
    second billable unit on top of the live one.
    """
    from datetime import datetime

    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    _create_group(db_session, datastore_id, backup_id="100", resolved_name="old-guest", superseded_at=datetime.now(UTC))
    _create_group(db_session, datastore_id, backup_id="100", resolved_name="new-guest")
    client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers)

    response = client.get(f"/api/v1/billing/customers/{customer_id}", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["vm_count"] == 1


def test_vm_ct_host_priced_separately(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    _create_group(db_session, datastore_id, backup_id="100")
    _create_group(db_session, datastore_id, backup_id="101", backup_type="ct")
    _create_group(db_session, datastore_id, backup_id="102", backup_type="ct")
    _create_group(db_session, datastore_id, backup_id="103", backup_type="host")
    client.post(
        "/api/v1/pricing-plans",
        json=_plan_payload(price_per_vm_month="10.00", price_per_ct_month="4.00", price_per_host_month="2.50"),
        headers=platform_admin_headers,
    )

    response = client.get(f"/api/v1/billing/customers/{customer_id}", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["vm_count"] == 1
    assert body["ct_count"] == 2
    assert body["host_count"] == 1
    # 1*10.00 + 2*4.00 + 1*2.50 + 0 GB = 20.50
    assert body["estimated_monthly_cost"] == "20.50"


def test_customer_plan_overrides_global_plan(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    _create_group(db_session, datastore_id)
    client.post("/api/v1/pricing-plans", json=_plan_payload(), headers=platform_admin_headers)
    client.post(
        "/api/v1/pricing-plans",
        json=_plan_payload(customer_id=str(customer_id), name="Enterprise", price_per_vm_month="8.00"),
        headers=platform_admin_headers,
    )

    response = client.get(f"/api/v1/billing/customers/{customer_id}", headers=platform_admin_headers)
    body = response.json()
    assert body["pricing_plan_source"] == "customer"
    assert body["price_per_vm_month"] == "8.00"
    assert body["estimated_monthly_cost"] == "8.00"


def test_offsite_datastore_excluded_from_vm_count_but_included_in_bytes(
    client, platform_admin_headers, db_session, monkeypatch
):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "offsite"},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)
    _add_snapshot(db_session, group_id, size_bytes=100_000_000_000)
    _add_measurement(db_session, datastore_id, used_bytes=50_000_000_000, logical_bytes=100_000_000_000)

    response = client.get(f"/api/v1/billing/customers/{customer_id}", headers=platform_admin_headers)
    body = response.json()
    assert body["vm_count"] == 0
    assert body["estimated_physical_bytes"] == 50_000_000_000


def test_group_override_wins_over_datastore_customer(client, platform_admin_headers, db_session, monkeypatch):
    datastore_owner = _create_customer(db_session, "DatastoreOwner", "owner")
    override_customer = _create_customer(db_session, "Override", "override")
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(datastore_owner), "role": "primary"},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-customer",
        json={"customer_id": str(override_customer)},
        headers=platform_admin_headers,
    )

    owner_response = client.get(f"/api/v1/billing/customers/{datastore_owner}", headers=platform_admin_headers)
    override_response = client.get(f"/api/v1/billing/customers/{override_customer}", headers=platform_admin_headers)
    assert owner_response.json()["vm_count"] == 0
    assert override_response.json()["vm_count"] == 1


def test_missing_usage_measurement_marks_estimate_incomplete(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)
    _add_snapshot(db_session, group_id, size_bytes=100_000_000_000)
    # No UsageMeasurement inserted at all.

    response = client.get(f"/api/v1/billing/customers/{customer_id}", headers=platform_admin_headers)
    body = response.json()
    assert body["vm_count"] == 1
    assert body["estimated_physical_bytes"] == 0
    assert body["physical_bytes_estimate_complete"] is False


def test_billing_list_respects_customer_scope(client, platform_admin_headers, db_session, test_settings):
    mine = _create_customer(db_session, "Mine", "mine")
    other = _create_customer(db_session, "Other", "other")

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, mine)
    response = client.get("/api/v1/billing/customers", headers=viewer_headers)
    assert response.status_code == 200
    ids = {row["customer_id"] for row in response.json()}
    assert ids == {str(mine)}
    assert str(other) not in ids


def test_billing_get_404_for_out_of_scope_customer(client, db_session, test_settings):
    mine = _create_customer(db_session, "Mine", "mine")
    other = _create_customer(db_session, "Other", "other")

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, mine)
    response = client.get(f"/api/v1/billing/customers/{other}", headers=viewer_headers)
    assert response.status_code == 404


def test_billing_export_returns_csv_of_every_customer(client, platform_admin_headers, db_session):
    """Raised directly by the user: an MSP operator needs a global,
    cross-tenant CSV of usage/billing data to pull into their own
    invoicing workflow, not just the per-customer JSON endpoint.
    """
    _create_customer(db_session, "Acme", "acme-csv")
    _create_customer(db_session, "Beta", "beta-csv")

    response = client.get("/api/v1/billing/customers/export", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text
    assert "customer_id,customer_name,customer_match_code" in body
    assert "Acme" in body
    assert "Beta" in body


def test_billing_export_requires_platform_admin(client, db_session, test_settings):
    mine = _create_customer(db_session, "Mine", "mine-csv")
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, mine)

    response = client.get("/api/v1/billing/customers/export", headers=viewer_headers)
    assert response.status_code == 403
