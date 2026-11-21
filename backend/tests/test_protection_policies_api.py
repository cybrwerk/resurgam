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


def _policy_payload(customer_id, **overrides):
    payload = {
        "customer_id": str(customer_id),
        "name": "Standard",
        "expected_interval_hours": 24,
        "max_time_since_last_backup_hours": 48,
        "min_snapshot_count": 1,
    }
    payload.update(overrides)
    return payload


def test_create_and_get_protection_policy(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)

    create_response = client.post(
        "/api/v1/protection-policies", json=_policy_payload(customer_id), headers=platform_admin_headers
    )
    assert create_response.status_code == 200
    body = create_response.json()
    assert body["customer_id"] == str(customer_id)
    assert body["name"] == "Standard"
    assert body["sync_dr_required"] is False

    get_response = client.get(f"/api/v1/protection-policies/{body['id']}", headers=platform_admin_headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == body["id"]


def test_create_rejects_max_time_below_interval(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.post(
        "/api/v1/protection-policies",
        json=_policy_payload(customer_id, expected_interval_hours=48, max_time_since_last_backup_hours=24),
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_create_rejects_duplicate_name_for_customer(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    first = client.post(
        "/api/v1/protection-policies", json=_policy_payload(customer_id), headers=platform_admin_headers
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/protection-policies", json=_policy_payload(customer_id), headers=platform_admin_headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "protection_policy_name_taken"


def test_create_rejects_unknown_customer(client, platform_admin_headers):
    response = client.post(
        "/api/v1/protection-policies",
        json=_policy_payload("00000000-0000-0000-0000-000000000000"),
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "customer_not_found"


def test_create_requires_platform_admin(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)
    response = client.post("/api/v1/protection-policies", json=_policy_payload(customer_id), headers=viewer_headers)
    assert response.status_code == 403


def test_list_respects_customer_scope(client, platform_admin_headers, db_session, test_settings):
    mine = _create_customer(db_session, "Mine", "mine")
    other = _create_customer(db_session, "Other", "other")
    client.post("/api/v1/protection-policies", json=_policy_payload(mine), headers=platform_admin_headers)
    client.post("/api/v1/protection-policies", json=_policy_payload(other), headers=platform_admin_headers)

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, mine)
    response = client.get("/api/v1/protection-policies", headers=viewer_headers)
    assert response.status_code == 200
    policies = response.json()
    assert len(policies) == 1
    assert policies[0]["customer_id"] == str(mine)


def test_get_out_of_scope_policy_is_404(client, platform_admin_headers, db_session, test_settings):
    mine = _create_customer(db_session, "Mine", "mine")
    other = _create_customer(db_session, "Other", "other")
    other_policy = client.post(
        "/api/v1/protection-policies", json=_policy_payload(other), headers=platform_admin_headers
    ).json()

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, mine)
    response = client.get(f"/api/v1/protection-policies/{other_policy['id']}", headers=viewer_headers)
    assert response.status_code == 404


def test_delete_policy_unassigns_it_from_backup_groups(client, platform_admin_headers, db_session, monkeypatch):
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
        "/api/v1/protection-policies", json=_policy_payload(customer_id), headers=platform_admin_headers
    ).json()
    assign_response = client.post(
        f"/api/v1/backup-groups/{group_id}/assign-policy",
        json={"protection_policy_id": policy["id"]},
        headers=platform_admin_headers,
    )
    assert assign_response.status_code == 200
    assert assign_response.json()["protection_policy_id"] == policy["id"]
    assert assign_response.json()["protection_status"] in {"ok", "warning", "critical"}

    delete_response = client.delete(f"/api/v1/protection-policies/{policy['id']}", headers=platform_admin_headers)
    assert delete_response.status_code == 204

    groups_response = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=platform_admin_headers)
    group = next(g for g in groups_response.json() if g["id"] == str(group_id))
    assert group["protection_policy_id"] is None
    assert group["protection_status"] == "unknown"


def test_assign_policy_rejects_customer_mismatch(client, platform_admin_headers, db_session, monkeypatch):
    owning_customer = _create_customer(db_session, "Owner", "owner")
    other_customer = _create_customer(db_session, "Other", "other")
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(owning_customer)},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id)

    other_policy = client.post(
        "/api/v1/protection-policies", json=_policy_payload(other_customer), headers=platform_admin_headers
    ).json()

    response = client.post(
        f"/api/v1/backup-groups/{group_id}/assign-policy",
        json={"protection_policy_id": other_policy["id"]},
        headers=platform_admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "protection_policy_customer_mismatch"


def test_unassign_policy(client, platform_admin_headers, db_session, monkeypatch):
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
        "/api/v1/protection-policies", json=_policy_payload(customer_id), headers=platform_admin_headers
    ).json()
    client.post(
        f"/api/v1/backup-groups/{group_id}/assign-policy",
        json={"protection_policy_id": policy["id"]},
        headers=platform_admin_headers,
    )

    response = client.post(f"/api/v1/backup-groups/{group_id}/unassign-policy", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["protection_policy_id"] is None
    assert response.json()["protection_status"] == "unknown"
