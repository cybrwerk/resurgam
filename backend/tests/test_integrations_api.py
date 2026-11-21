"""Tests for the Administration -> Integrations backend (manifest §6.3):
GET/PUT /integrations/oidc, POST /integrations/oidc/test.
"""


def test_get_oidc_settings_defaults_to_environment(client, platform_admin_headers, test_settings):
    response = client.get("/api/v1/integrations/oidc", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "environment"
    assert body["issuer"] == test_settings.oidc_issuer
    assert body["enabled"] is False
    assert "client_secret" not in body


def test_put_oidc_settings_requires_secret_on_first_save(client, platform_admin_headers):
    response = client.put(
        "/api/v1/integrations/oidc",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "resurgam",
            "redirect_uri": "https://app/cb",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "client_secret_required"


def test_put_then_get_oidc_settings_switches_source_to_database(client, platform_admin_headers, db_session):
    put_response = client.put(
        "/api/v1/integrations/oidc",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "resurgam",
            "client_secret": "s3cret",
            "redirect_uri": "https://app/cb",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    assert put_response.status_code == 200
    body = put_response.json()
    assert body["source"] == "database"
    assert body["issuer"] == "https://idp.example.com"
    assert body["client_secret_configured"] is True
    assert "s3cret" not in put_response.text

    get_response = client.get("/api/v1/integrations/oidc", headers=platform_admin_headers)
    assert get_response.json()["source"] == "database"


def test_put_oidc_settings_omitting_secret_keeps_the_stored_one(
    client, platform_admin_headers, db_session, test_settings
):
    from resurgam_api.auth.oidc import resolve_effective_oidc_config

    client.put(
        "/api/v1/integrations/oidc",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "resurgam",
            "client_secret": "original-secret",
            "redirect_uri": "https://app/cb",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )

    update_response = client.put(
        "/api/v1/integrations/oidc",
        json={
            "issuer": "https://idp2.example.com",
            "client_id": "resurgam",
            "redirect_uri": "https://app/cb",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    assert update_response.status_code == 200
    assert update_response.json()["issuer"] == "https://idp2.example.com"

    config = resolve_effective_oidc_config(db_session, test_settings)
    assert config.client_secret == "original-secret"  # untouched by the secret-less update


def test_put_oidc_settings_disabled_falls_back_to_environment(
    client, platform_admin_headers, db_session, test_settings
):
    from resurgam_api.auth.oidc import resolve_effective_oidc_config

    client.put(
        "/api/v1/integrations/oidc",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "resurgam",
            "client_secret": "s3cret",
            "redirect_uri": "https://app/cb",
            "enabled": False,
        },
        headers=platform_admin_headers,
    )

    config = resolve_effective_oidc_config(db_session, test_settings)
    assert config.source == "environment"


def test_put_oidc_settings_records_audit_event(client, platform_admin_headers, db_session):
    from resurgam_api.models.audit import AuditEvent

    client.put(
        "/api/v1/integrations/oidc",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "resurgam",
            "client_secret": "s3cret",
            "redirect_uri": "https://app/cb",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )

    event = db_session.query(AuditEvent).filter_by(action="oidc_settings_updated").one()
    assert event.detail["secret_rotated"] is True


def test_oidc_settings_require_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    viewer_headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/integrations/oidc", headers=viewer_headers).status_code == 403
    assert (
        client.put(
            "/api/v1/integrations/oidc",
            json={"issuer": "x", "client_id": "x", "client_secret": "x", "redirect_uri": "x", "enabled": True},
            headers=viewer_headers,
        ).status_code
        == 403
    )
    assert (
        client.post("/api/v1/integrations/oidc/test", json={"issuer": "https://x"}, headers=viewer_headers).status_code
        == 403
    )


def test_discovery_test_reports_unreachable_for_a_bad_issuer(client, platform_admin_headers):
    response = client.post(
        "/api/v1/integrations/oidc/test",
        json={"issuer": "https://this-host-does-not-exist.invalid.example"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is False
    assert body["error"] is not None
