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


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _mock_list_datastores(monkeypatch, module, datastores):
    monkeypatch.setattr(
        module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=datastores),
    )


def test_discover_and_import_datastore(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    remote = [RemoteDatastore(name="customer-a-store", path="/mnt/store1", comment="Customer A")]
    _mock_list_datastores(monkeypatch, pbs_instances_module, remote)

    discover_response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/available-datastores", headers=platform_admin_headers
    )
    assert discover_response.status_code == 200
    assert discover_response.json() == [{"name": "customer-a-store", "path": "/mnt/store1", "comment": "Customer A"}]

    import_response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores",
        json={"name": "customer-a-store"},
        headers=platform_admin_headers,
    )
    assert import_response.status_code == 200
    body = import_response.json()
    assert body["name"] == "customer-a-store"
    assert body["path"] == "/mnt/store1"
    assert body["customer_id"] is None

    # now discovered again, the imported one should be excluded
    discover_again = client.get(
        f"/api/v1/pbs-instances/{instance_id}/available-datastores", headers=platform_admin_headers
    )
    assert discover_again.json() == []


def test_import_duplicate_name_rejected(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    _mock_list_datastores(monkeypatch, pbs_instances_module, [RemoteDatastore(name="store1")])

    first = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": "store1"}, headers=platform_admin_headers
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": "store1"}, headers=platform_admin_headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "datastore_already_imported"


def test_import_unknown_name_rejected(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    _mock_list_datastores(monkeypatch, pbs_instances_module, [RemoteDatastore(name="store1")])

    response = client.post(
        f"/api/v1/pbs-instances/{instance_id}/datastores",
        json={"name": "does-not-exist"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "datastore_not_found_on_pbs"


def _import_one(client, headers, instance_id, monkeypatch, name="store1"):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    _mock_list_datastores(monkeypatch, pbs_instances_module, [RemoteDatastore(name=name)])
    response = client.post(f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": name}, headers=headers)
    return response.json()["id"]


def test_assign_reassign_and_unassign_customer(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)
    customer_a = _create_customer(db_session, "Acme", "acme")
    customer_b = _create_customer(db_session, "Globex", "globex")

    assign_response = client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_a)},
        headers=platform_admin_headers,
    )
    assert assign_response.status_code == 200
    assert assign_response.json()["customer_id"] == str(customer_a)
    assert assign_response.json()["role"] == "primary"  # default when not specified

    reassign_response = client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_b)},
        headers=platform_admin_headers,
    )
    assert reassign_response.status_code == 200
    assert reassign_response.json()["customer_id"] == str(customer_b)

    unassign_response = client.post(
        f"/api/v1/datastores/{datastore_id}/unassign-customer", headers=platform_admin_headers
    )
    assert unassign_response.status_code == 200
    assert unassign_response.json()["customer_id"] is None


def test_assign_customer_with_offsite_role(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)
    customer_id = _create_customer(db_session)

    response = client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "offsite"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["role"] == "offsite"


def test_assign_to_unknown_customer_rejected(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)

    response = client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": "00000000-0000-0000-0000-000000000000"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "customer_not_found"


def test_delete_datastore(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)

    delete_response = client.delete(f"/api/v1/datastores/{datastore_id}", headers=platform_admin_headers)
    assert delete_response.status_code == 204

    get_response = client.get(f"/api/v1/datastores/{datastore_id}", headers=platform_admin_headers)
    assert get_response.status_code == 404


def _create_customer_scoped_viewer(db_session, test_settings, customer_id) -> dict:
    """Create a genuinely separate, non-breakglass user with a customer-
    scoped viewer assignment, and mint a session token directly.

    Deliberately does not go through the break-glass login endpoint: that
    endpoint always resolves to the *same* fixed break-glass identity
    (get_or_create_breakglass_user keys off a static configured username),
    so reusing it here would just add a second role assignment onto the
    same user already granted global platform_admin by the
    `platform_admin_headers` fixture in the same test — silently defeating
    the scope-filtering assertion this test exists to make.
    """
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


def test_platform_admin_sees_all_datastores(client, platform_admin_headers, db_session, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    _import_one(client, platform_admin_headers, instance_id, monkeypatch, name="store-a")

    response = client.get("/api/v1/datastores", headers=platform_admin_headers)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_customer_scoped_viewer_sees_only_own_customer_datastore(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    own_datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch, name="store-mine")
    other_datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch, name="store-other")

    customer_mine = _create_customer(db_session, "Mine Inc", "mine")
    customer_other = _create_customer(db_session, "Other Inc", "other")

    client.post(
        f"/api/v1/datastores/{own_datastore_id}/assign-customer",
        json={"customer_id": str(customer_mine)},
        headers=platform_admin_headers,
    )
    client.post(
        f"/api/v1/datastores/{other_datastore_id}/assign-customer",
        json={"customer_id": str(customer_other)},
        headers=platform_admin_headers,
    )

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_mine)

    list_response = client.get("/api/v1/datastores", headers=viewer_headers)
    assert list_response.status_code == 200
    ids = {d["id"] for d in list_response.json()}
    assert ids == {own_datastore_id}

    own_get = client.get(f"/api/v1/datastores/{own_datastore_id}", headers=viewer_headers)
    assert own_get.status_code == 200

    other_get = client.get(f"/api/v1/datastores/{other_datastore_id}", headers=viewer_headers)
    assert other_get.status_code == 404  # not 403 — must not confirm existence


def test_unassigned_datastore_invisible_to_non_admin(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)
    customer_mine = _create_customer(db_session, "Mine Inc", "mine")

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_mine)

    list_response = client.get("/api/v1/datastores", headers=viewer_headers)
    assert list_response.json() == []

    get_response = client.get(f"/api/v1/datastores/{datastore_id}", headers=viewer_headers)
    assert get_response.status_code == 404


# --- PBS encryption key (docs/adr/0022) -----------------------------------------


def test_set_encryption_key_marks_configured_but_never_returns_content(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)

    get_before = client.get(f"/api/v1/datastores/{datastore_id}", headers=platform_admin_headers)
    assert get_before.json()["encryption_key_configured"] is False

    set_response = client.post(
        f"/api/v1/datastores/{datastore_id}/encryption-key",
        json={"keyfile_content": '{"kdf":"scrypt","data":"fake-key-material"}'},
        headers=platform_admin_headers,
    )
    assert set_response.status_code == 200
    body = set_response.json()
    assert body["encryption_key_configured"] is True
    assert "keyfile_content" not in body
    assert "encryption_key_secret_ref_id" not in body


def test_set_encryption_key_on_second_call_rotates_in_place(client, platform_admin_headers, monkeypatch, db_session):
    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.secrets import SecretReference

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)

    client.post(
        f"/api/v1/datastores/{datastore_id}/encryption-key",
        json={"keyfile_content": "first-key"},
        headers=platform_admin_headers,
    )
    first_ref_id = db_session.get(Datastore, datastore_id).encryption_key_secret_ref_id

    client.post(
        f"/api/v1/datastores/{datastore_id}/encryption-key",
        json={"keyfile_content": "second-key"},
        headers=platform_admin_headers,
    )
    db_session.expire_all()
    second_ref_id = db_session.get(Datastore, datastore_id).encryption_key_secret_ref_id

    # Rotated in place, not a second orphaned SecretReference row.
    assert first_ref_id == second_ref_id
    assert db_session.query(SecretReference).filter_by(purpose="pbs_encryption_key").count() == 1


def test_delete_encryption_key_clears_reference_and_removes_secret(
    client, platform_admin_headers, monkeypatch, db_session
):
    from resurgam_api.models.secrets import SecretReference

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/encryption-key",
        json={"keyfile_content": "some-key"},
        headers=platform_admin_headers,
    )

    delete_response = client.delete(f"/api/v1/datastores/{datastore_id}/encryption-key", headers=platform_admin_headers)
    assert delete_response.status_code == 200
    assert delete_response.json()["encryption_key_configured"] is False
    assert db_session.query(SecretReference).filter_by(purpose="pbs_encryption_key").count() == 0


def test_delete_encryption_key_when_none_configured_rejected(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)

    response = client.delete(f"/api/v1/datastores/{datastore_id}/encryption-key", headers=platform_admin_headers)
    assert response.status_code == 400
    assert response.json()["code"] == "no_encryption_key"


def test_set_encryption_key_requires_platform_admin(
    client, monkeypatch, db_session, test_settings, platform_admin_headers
):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_one(client, platform_admin_headers, instance_id, monkeypatch)
    customer = _create_customer(db_session, "Some Co", "someco")
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer)

    response = client.post(
        f"/api/v1/datastores/{datastore_id}/encryption-key",
        json={"keyfile_content": "some-key"},
        headers=viewer_headers,
    )
    assert response.status_code == 403


def test_set_encryption_key_nonexistent_datastore_404(client, platform_admin_headers):
    response = client.post(
        "/api/v1/datastores/00000000-0000-0000-0000-000000000000/encryption-key",
        json={"keyfile_content": "some-key"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404
