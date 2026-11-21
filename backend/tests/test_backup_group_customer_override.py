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


def _create_group(db_session, datastore_id, backup_id="100"):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(datastore_id=datastore_id, backup_type="vm", backup_id=backup_id)
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def test_assign_and_unassign_group_customer_override(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    group_id = _create_group(db_session, datastore_id)
    customer_id = _create_customer(db_session)

    assign_response = client.post(
        f"/api/v1/backup-groups/{group_id}/assign-customer",
        json={"customer_id": str(customer_id)},
        headers=platform_admin_headers,
    )
    assert assign_response.status_code == 200
    assert assign_response.json()["customer_id"] == str(customer_id)
    assert assign_response.json()["effective_customer_id"] == str(customer_id)

    unassign_response = client.post(
        f"/api/v1/backup-groups/{group_id}/unassign-customer", headers=platform_admin_headers
    )
    assert unassign_response.status_code == 200
    assert unassign_response.json()["customer_id"] is None
    assert unassign_response.json()["effective_customer_id"] is None


def test_assign_group_customer_rejects_unknown_customer(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    group_id = _create_group(db_session, datastore_id)

    response = client.post(
        f"/api/v1/backup-groups/{group_id}/assign-customer",
        json={"customer_id": "00000000-0000-0000-0000-000000000000"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "customer_not_found"


def test_group_override_visible_to_its_own_customer_on_shared_datastore(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    """A shared datastore assigned to customer A can still expose one
    group to customer B via an override — the core new capability."""
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    customer_a = _create_customer(db_session, "Acme", "acme")
    customer_b = _create_customer(db_session, "Globex", "globex")

    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_a)},
        headers=platform_admin_headers,
    )
    group_for_a = _create_group(db_session, datastore_id, backup_id="100")
    group_for_b = _create_group(db_session, datastore_id, backup_id="200")
    client.post(
        f"/api/v1/backup-groups/{group_for_b}/assign-customer",
        json={"customer_id": str(customer_b)},
        headers=platform_admin_headers,
    )

    viewer_b_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_b)
    response = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=viewer_b_headers)
    assert response.status_code == 200
    groups = response.json()
    assert len(groups) == 1
    assert groups[0]["id"] == str(group_for_b)

    # customer A's viewer sees only their own group, not B's override
    viewer_a_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_a)
    response_a = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=viewer_a_headers)
    assert response_a.status_code == 200
    groups_a = response_a.json()
    assert len(groups_a) == 1
    assert groups_a[0]["id"] == str(group_for_a)


def test_datastore_with_no_assignment_but_one_group_override_is_visible(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    customer_id = _create_customer(db_session)
    group_id = _create_group(db_session, datastore_id)
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-customer",
        json={"customer_id": str(customer_id)},
        headers=platform_admin_headers,
    )

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)
    response = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=viewer_headers)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_datastore_fully_out_of_scope_is_404(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    _create_group(db_session, datastore_id)
    other_customer = _create_customer(db_session)

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, other_customer)
    response = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=viewer_headers)
    assert response.status_code == 404


def test_snapshots_endpoint_respects_group_override(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    customer_a = _create_customer(db_session, "Acme", "acme")
    customer_b = _create_customer(db_session, "Globex", "globex")
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_a)},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-customer",
        json={"customer_id": str(customer_b)},
        headers=platform_admin_headers,
    )

    viewer_a_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_a)
    response_a = client.get(f"/api/v1/backup-groups/{group_id}/snapshots", headers=viewer_a_headers)
    assert response_a.status_code == 404  # overridden away from A

    viewer_b_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_b)
    response_b = client.get(f"/api/v1/backup-groups/{group_id}/snapshots", headers=viewer_b_headers)
    assert response_b.status_code == 200


def test_assign_policy_uses_effective_customer_not_just_datastore(
    client, platform_admin_headers, db_session, monkeypatch
):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    datastore_customer = _create_customer(db_session, "Acme", "acme")
    group_customer = _create_customer(db_session, "Globex", "globex")
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(datastore_customer)},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-customer",
        json={"customer_id": str(group_customer)},
        headers=platform_admin_headers,
    )

    policy = client.post(
        "/api/v1/protection-policies",
        json={
            "customer_id": str(group_customer),
            "name": "Standard",
            "expected_interval_hours": 24,
            "max_time_since_last_backup_hours": 48,
            "min_snapshot_count": 1,
        },
        headers=platform_admin_headers,
    ).json()

    # Would have been rejected under the old datastore-only check, since
    # the policy belongs to group_customer, not the datastore's own
    # datastore_customer.
    response = client.post(
        f"/api/v1/backup-groups/{group_id}/assign-policy",
        json={"protection_policy_id": policy["id"]},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
