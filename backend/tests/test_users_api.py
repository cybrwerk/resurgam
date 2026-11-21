def _create_user_payload(**overrides):
    payload = {"email": "alice@example.com", "display_name": "Alice", "password": "correct horse battery"}
    payload.update(overrides)
    return payload


def test_create_list_get_user(client, platform_admin_headers):
    create_response = client.post("/api/v1/users", json=_create_user_payload(), headers=platform_admin_headers)
    assert create_response.status_code == 200
    body = create_response.json()
    assert body["email"] == "alice@example.com"
    assert body["has_password"] is True
    assert body["disabled"] is False
    assert "password" not in body
    assert "password_hash" not in body

    list_response = client.get("/api/v1/users", headers=platform_admin_headers)
    assert list_response.status_code == 200
    assert any(u["email"] == "alice@example.com" for u in list_response.json())

    get_response = client.get(f"/api/v1/users/{body['id']}", headers=platform_admin_headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == body["id"]


def test_create_rejects_duplicate_email(client, platform_admin_headers):
    client.post("/api/v1/users", json=_create_user_payload(), headers=platform_admin_headers)
    response = client.post("/api/v1/users", json=_create_user_payload(), headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "email_taken"


def test_create_requires_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)

    response = client.post("/api/v1/users", json=_create_user_payload(), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_disable_and_enable(client, platform_admin_headers):
    created = client.post("/api/v1/users", json=_create_user_payload(), headers=platform_admin_headers).json()

    disable_response = client.post(f"/api/v1/users/{created['id']}/disable", headers=platform_admin_headers)
    assert disable_response.status_code == 200
    assert disable_response.json()["disabled"] is True

    enable_response = client.post(f"/api/v1/users/{created['id']}/enable", headers=platform_admin_headers)
    assert enable_response.status_code == 200
    assert enable_response.json()["disabled"] is False


def test_assign_and_unassign_role(client, platform_admin_headers, db_session):
    from resurgam_api.models.tenancy import Customer

    created = client.post("/api/v1/users", json=_create_user_payload(), headers=platform_admin_headers).json()

    customer = Customer(name="Acme", slug="acme", debitor_number="D-1", match_code="ACM")
    db_session.add(customer)
    db_session.flush()
    db_session.commit()

    assign_response = client.post(
        f"/api/v1/users/{created['id']}/assign-role",
        json={"role": "customer_admin", "scope_type": "customer", "scope_id": str(customer.id)},
        headers=platform_admin_headers,
    )
    assert assign_response.status_code == 200
    assignment = assign_response.json()
    assert assignment["role"] == "customer_admin"
    assert assignment["scope_id"] == str(customer.id)

    list_response = client.get(f"/api/v1/users/{created['id']}/role-assignments", headers=platform_admin_headers)
    assert len(list_response.json()) == 1

    unassign_response = client.post(
        f"/api/v1/users/{created['id']}/unassign-role",
        json={"assignment_id": assignment["id"]},
        headers=platform_admin_headers,
    )
    assert unassign_response.status_code == 204

    list_after = client.get(f"/api/v1/users/{created['id']}/role-assignments", headers=platform_admin_headers)
    assert list_after.json() == []


def test_user_list_and_get_include_role_assignments(client, platform_admin_headers, db_session):
    """A real UX-audit finding (2026-09-23): "Benutzer & Rollen" showed
    no role at all in its own list -- answering "who has platform_admin"
    meant opening every user one by one. `UserOut.roles` closes that gap.
    """
    from resurgam_api.models.tenancy import Customer

    created = client.post("/api/v1/users", json=_create_user_payload(), headers=platform_admin_headers).json()
    assert created["roles"] == []

    customer = Customer(name="Acme", slug="acme", debitor_number="D-2", match_code="ACM")
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    client.post(
        f"/api/v1/users/{created['id']}/assign-role",
        json={"role": "customer_admin", "scope_type": "customer", "scope_id": str(customer.id)},
        headers=platform_admin_headers,
    )

    get_response = client.get(f"/api/v1/users/{created['id']}", headers=platform_admin_headers)
    assert [r["role"] for r in get_response.json()["roles"]] == ["customer_admin"]

    list_response = client.get("/api/v1/users", headers=platform_admin_headers)
    listed = next(u for u in list_response.json() if u["id"] == created["id"])
    assert [r["role"] for r in listed["roles"]] == ["customer_admin"]
    assert listed["roles"][0]["scope_id"] == str(customer.id)


def test_assign_role_rejects_duplicate(client, platform_admin_headers):
    created = client.post("/api/v1/users", json=_create_user_payload(), headers=platform_admin_headers).json()
    body = {"role": "viewer", "scope_type": "global", "scope_id": None}
    first = client.post(f"/api/v1/users/{created['id']}/assign-role", json=body, headers=platform_admin_headers)
    assert first.status_code == 200
    second = client.post(f"/api/v1/users/{created['id']}/assign-role", json=body, headers=platform_admin_headers)
    assert second.status_code == 409
    assert second.json()["code"] == "assignment_exists"


def test_get_nonexistent_user_returns_404(client, platform_admin_headers):
    response = client.get("/api/v1/users/00000000-0000-0000-0000-000000000000", headers=platform_admin_headers)
    assert response.status_code == 404


def test_admin_sets_password_for_an_imported_user(client, db_session, platform_admin_headers):
    from resurgam_api.models.identity import User

    user = User(email="imported@example.com", display_name="Imported")  # e.g. from a configuration import
    db_session.add(user)
    db_session.commit()

    response = client.post(
        f"/api/v1/users/{user.id}/set-password", json={"password": "a-new-password"}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["has_password"] is True
    login = client.post(
        "/api/v1/auth/local-login", json={"email": "imported@example.com", "password": "a-new-password"}
    )
    assert login.status_code == 200

    too_short = client.post(
        f"/api/v1/users/{user.id}/set-password", json={"password": "short"}, headers=platform_admin_headers
    )
    assert too_short.status_code == 422


def test_set_password_refuses_the_breakglass_user(client, db_session, platform_admin_headers):
    from resurgam_api.models.identity import User

    breakglass = db_session.query(User).filter_by(is_breakglass=True).first()
    response = client.post(
        f"/api/v1/users/{breakglass.id}/set-password",
        json={"password": "a-new-password"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "breakglass_password_not_settable"


def _local_admin(client, platform_admin_headers, email):
    created = client.post(
        "/api/v1/users", json=_create_user_payload(email=email, display_name=email), headers=platform_admin_headers
    ).json()
    assignment = client.post(
        f"/api/v1/users/{created['id']}/assign-role",
        json={"role": "platform_admin", "scope_type": "global"},
        headers=platform_admin_headers,
    ).json()
    login = client.post("/api/v1/auth/local-login", json={"email": email, "password": "correct horse battery"}).json()
    return created["id"], assignment["id"], {"Authorization": f"Bearer {login['access_token']}"}


def test_nobody_can_disable_their_own_account(client, platform_admin_headers):
    """Live finding 2026-11-21: a platform admin could disable their own account."""
    admin_id, _, admin_headers = _local_admin(client, platform_admin_headers, "ops@example.com")
    _local_admin(client, platform_admin_headers, "ops2@example.com")

    response = client.post(f"/api/v1/users/{admin_id}/disable", headers=admin_headers)

    assert response.status_code == 409
    assert response.json()["code"] == "cannot_disable_self"


def test_the_last_platform_admin_cannot_be_disabled(client, platform_admin_headers):
    # Break-glass (the fixture's caller) does not count: it only exists while switched on.
    admin_id, _, _ = _local_admin(client, platform_admin_headers, "ops@example.com")

    response = client.post(f"/api/v1/users/{admin_id}/disable", headers=platform_admin_headers)

    assert response.status_code == 409
    assert response.json()["code"] == "last_platform_admin"


def test_a_platform_admin_can_be_disabled_while_another_remains(client, platform_admin_headers):
    admin_id, _, _ = _local_admin(client, platform_admin_headers, "ops@example.com")
    _local_admin(client, platform_admin_headers, "ops2@example.com")

    response = client.post(f"/api/v1/users/{admin_id}/disable", headers=platform_admin_headers)

    assert response.status_code == 200


def test_the_last_global_platform_admin_role_cannot_be_removed(client, platform_admin_headers):
    admin_id, assignment_id, _ = _local_admin(client, platform_admin_headers, "ops@example.com")

    refused = client.post(
        f"/api/v1/users/{admin_id}/unassign-role", json={"assignment_id": assignment_id}, headers=platform_admin_headers
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "last_platform_admin"

    _local_admin(client, platform_admin_headers, "ops2@example.com")
    allowed = client.post(
        f"/api/v1/users/{admin_id}/unassign-role", json={"assignment_id": assignment_id}, headers=platform_admin_headers
    )
    assert allowed.status_code == 204
