def _create_customer_payload(**overrides):
    payload = {"name": "Acme Corp", "slug": "acme", "debitor_number": "10001", "match_code": "ACM"}
    payload.update(overrides)
    return payload


def _create_customer_scoped_viewer(db_session, test_settings, customer_id) -> dict:
    """See tests/test_datastores_api.py for why this doesn't reuse the
    break-glass login: it always resolves to the same fixed identity.
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


def test_create_list_get_customer(client, platform_admin_headers):
    create_response = client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)
    assert create_response.status_code == 200
    body = create_response.json()
    assert body["name"] == "Acme Corp"
    assert body["debitor_number"] == "10001"
    assert body["match_code"] == "ACM"
    assert body["disabled"] is False

    customer_id = body["id"]

    list_response = client.get("/api/v1/customers", headers=platform_admin_headers)
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    get_response = client.get(f"/api/v1/customers/{customer_id}", headers=platform_admin_headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == customer_id


def test_match_code_lowercase_is_normalized_to_uppercase(client, platform_admin_headers):
    response = client.post(
        "/api/v1/customers", json=_create_customer_payload(match_code="acm"), headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["match_code"] == "ACM"


def test_match_code_wrong_length_rejected(client, platform_admin_headers):
    response = client.post(
        "/api/v1/customers", json=_create_customer_payload(match_code="AC"), headers=platform_admin_headers
    )
    assert response.status_code == 422


def test_match_code_non_alpha_rejected(client, platform_admin_headers):
    response = client.post(
        "/api/v1/customers", json=_create_customer_payload(match_code="A1C"), headers=platform_admin_headers
    )
    assert response.status_code == 422


def test_duplicate_slug_rejected(client, platform_admin_headers):
    client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)
    response = client.post(
        "/api/v1/customers",
        json=_create_customer_payload(debitor_number="10002", match_code="ACB"),
        headers=platform_admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "customer_slug_taken"


def test_duplicate_debitor_number_rejected(client, platform_admin_headers):
    client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)
    response = client.post(
        "/api/v1/customers",
        json=_create_customer_payload(slug="acme-2", match_code="ACB"),
        headers=platform_admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "customer_debitor_number_taken"


def test_duplicate_match_code_rejected(client, platform_admin_headers):
    client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)
    response = client.post(
        "/api/v1/customers",
        json=_create_customer_payload(slug="acme-2", debitor_number="10002"),
        headers=platform_admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "customer_match_code_taken"


def test_non_platform_admin_role_is_forbidden(client, db_session):
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, UserRoleAssignment

    login = client.post("/api/v1/auth/breakglass-login", json={"username": "test-admin", "password": "test-password"})
    token = login.json()["access_token"]
    user_id = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}).json()["user_id"]

    # Break-glass always holds platform_admin (ADR 0028); this test needs a plain viewer.
    db_session.query(UserRoleAssignment).filter_by(user_id=user_id).delete()
    viewer_role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user_id, role_id=viewer_role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()

    response = client.post(
        "/api/v1/customers", json=_create_customer_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_disable_and_enable(client, platform_admin_headers):
    create_response = client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)
    customer_id = create_response.json()["id"]

    disable_response = client.post(f"/api/v1/customers/{customer_id}/disable", headers=platform_admin_headers)
    assert disable_response.json()["disabled"] is True

    enable_response = client.post(f"/api/v1/customers/{customer_id}/enable", headers=platform_admin_headers)
    assert enable_response.json()["disabled"] is False


def test_get_nonexistent_customer_returns_404(client, platform_admin_headers):
    response = client.get("/api/v1/customers/00000000-0000-0000-0000-000000000000", headers=platform_admin_headers)
    assert response.status_code == 404


def test_customer_scoped_viewer_sees_only_own_customer(client, platform_admin_headers, db_session, test_settings):
    import uuid

    mine = client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers).json()
    other = client.post(
        "/api/v1/customers",
        json=_create_customer_payload(name="Other Inc", slug="other", debitor_number="10002", match_code="OTH"),
        headers=platform_admin_headers,
    ).json()

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, uuid.UUID(mine["id"]))

    list_response = client.get("/api/v1/customers", headers=viewer_headers)
    assert list_response.status_code == 200
    ids = {c["id"] for c in list_response.json()}
    assert ids == {mine["id"]}

    own_get = client.get(f"/api/v1/customers/{mine['id']}", headers=viewer_headers)
    assert own_get.status_code == 200

    other_get = client.get(f"/api/v1/customers/{other['id']}", headers=viewer_headers)
    assert other_get.status_code == 404


def test_update_changes_only_given_fields(client, platform_admin_headers):
    created = client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers).json()

    response = client.patch(
        f"/api/v1/customers/{created['id']}", json={"name": "Renamed Corp"}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed Corp"
    assert body["debitor_number"] == created["debitor_number"]
    assert body["match_code"] == created["match_code"]
    assert body["slug"] == created["slug"]


def test_update_uppercases_match_code(client, platform_admin_headers):
    created = client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers).json()
    response = client.patch(
        f"/api/v1/customers/{created['id']}", json={"match_code": "xyz"}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["match_code"] == "XYZ"


def test_update_rejects_duplicate_debitor_number(client, platform_admin_headers):
    client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)
    other = client.post(
        "/api/v1/customers",
        json=_create_customer_payload(name="Other", slug="other", debitor_number="10002", match_code="OTH"),
        headers=platform_admin_headers,
    ).json()

    response = client.patch(
        f"/api/v1/customers/{other['id']}", json={"debitor_number": "10001"}, headers=platform_admin_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "customer_debitor_number_taken"


def test_update_nonexistent_customer_returns_404(client, platform_admin_headers):
    response = client.patch(
        "/api/v1/customers/00000000-0000-0000-0000-000000000000",
        json={"name": "x"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404


def test_update_requires_platform_admin(client, db_session, test_settings):
    import uuid

    created_id = None
    from resurgam_api.models.tenancy import Customer

    customer = Customer(name="Acme", slug="acme-2", debitor_number="20001", match_code="AC2")
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    created_id = customer.id

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, uuid.UUID(str(created_id)))
    response = client.patch(f"/api/v1/customers/{created_id}", json={"name": "x"}, headers=viewer_headers)
    assert response.status_code == 403


def _csv_upload(rows: list[str]) -> dict:
    content = "\n".join(["name,slug,debitor_number,match_code", *rows]).encode("utf-8")
    return {"file": ("customers.csv", content, "text/csv")}


def test_export_returns_csv_of_all_customers(client, platform_admin_headers):
    client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)
    client.post(
        "/api/v1/customers",
        json=_create_customer_payload(name="Other Inc", slug="other", debitor_number="10002", match_code="OTH"),
        headers=platform_admin_headers,
    )

    response = client.get("/api/v1/customers/export", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text
    assert "name,slug,debitor_number,match_code,disabled" in body
    assert "Acme Corp,acme,10001,ACM,false" in body
    assert "Other Inc,other,10002,OTH,false" in body


def test_export_requires_platform_admin(client, db_session, test_settings):
    import uuid

    from resurgam_api.models.tenancy import Customer

    customer = Customer(name="Acme", slug="acme-3", debitor_number="30001", match_code="AC3")
    db_session.add(customer)
    db_session.flush()
    db_session.commit()

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, uuid.UUID(str(customer.id)))
    response = client.get("/api/v1/customers/export", headers=viewer_headers)
    assert response.status_code == 403


def test_import_creates_new_customers(client, platform_admin_headers):
    files = _csv_upload(
        [
            "Acme Corp,acme,10001,ACM",
            "Other Inc,other,10002,OTH",
        ]
    )
    response = client.post("/api/v1/customers/import", files=files, headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert body["errors"] == []

    listed = client.get("/api/v1/customers", headers=platform_admin_headers).json()
    assert {c["slug"] for c in listed} == {"acme", "other"}


def test_reimporting_same_csv_updates_by_slug(client, platform_admin_headers):
    files = _csv_upload(["Acme Corp,acme,10001,ACM"])
    client.post("/api/v1/customers/import", files=files, headers=platform_admin_headers)

    updated_files = _csv_upload(["Acme Corporation,acme,10001,ACM"])
    response = client.post("/api/v1/customers/import", files=updated_files, headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 0
    assert body["updated"] == 1

    listed = client.get("/api/v1/customers", headers=platform_admin_headers).json()
    assert listed[0]["name"] == "Acme Corporation"


def test_import_reports_row_errors_without_aborting_other_rows(client, platform_admin_headers):
    files = _csv_upload(
        [
            "Acme Corp,acme,10001,ACM",
            "Bad Row,bad,10002,TOOLONG",  # match_code must be exactly 3 letters
            "Other Inc,other,10003,OTH",
        ]
    )
    response = client.post("/api/v1/customers/import", files=files, headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert len(body["errors"]) == 1
    assert body["errors"][0]["row"] == 3  # header is row 1, "Bad Row" is row 3

    listed = client.get("/api/v1/customers", headers=platform_admin_headers).json()
    assert {c["slug"] for c in listed} == {"acme", "other"}


def test_import_rejects_debitor_number_collision_with_different_customer(client, platform_admin_headers):
    client.post("/api/v1/customers", json=_create_customer_payload(), headers=platform_admin_headers)

    files = _csv_upload(["Someone Else,someone-else,10001,SOE"])  # 10001 already used by "acme"
    response = client.post("/api/v1/customers/import", files=files, headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 1
    assert "Debitor number" in body["errors"][0]["message"]


def test_import_rejects_csv_missing_required_columns(client, platform_admin_headers):
    content = b"name,slug\nAcme,acme"
    response = client.post(
        "/api/v1/customers/import",
        files={"file": ("customers.csv", content, "text/csv")},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "csv_missing_columns"


def test_import_requires_platform_admin(client, db_session, test_settings):
    import uuid

    from resurgam_api.models.tenancy import Customer

    customer = Customer(name="Acme", slug="acme-4", debitor_number="40001", match_code="AC4")
    db_session.add(customer)
    db_session.flush()
    db_session.commit()

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, uuid.UUID(str(customer.id)))
    files = _csv_upload(["Acme Corp,acme,10001,ACM"])
    response = client.post("/api/v1/customers/import", files=files, headers=viewer_headers)
    assert response.status_code == 403
