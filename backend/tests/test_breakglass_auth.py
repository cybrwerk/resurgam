from resurgam_api.models.audit import AuditEvent
from tests.conftest import TEST_BREAKGLASS_PASSWORD, TEST_BREAKGLASS_USERNAME


def test_login_success_issues_token_and_audit_event(client, db_session):
    response = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": TEST_BREAKGLASS_USERNAME, "password": TEST_BREAKGLASS_PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    events = db_session.query(AuditEvent).filter_by(action="breakglass_login").all()
    assert len(events) == 1
    assert events[0].detail == {"username": TEST_BREAKGLASS_USERNAME}


def test_login_wrong_password_rejected_and_audited(client, db_session):
    response = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": TEST_BREAKGLASS_USERNAME, "password": "definitely-wrong"},
    )

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "invalid_credentials"
    assert body["correlation_id"]

    events = db_session.query(AuditEvent).filter_by(action="breakglass_login_failed").all()
    assert len(events) == 1


def test_login_disabled_by_default(client, db_session, test_settings):
    test_settings.breakglass_enabled = False

    response = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": TEST_BREAKGLASS_USERNAME, "password": TEST_BREAKGLASS_PASSWORD},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "breakglass_disabled"


def test_whoami_with_valid_token_reflects_breakglass_user(client):
    login = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": TEST_BREAKGLASS_USERNAME, "password": TEST_BREAKGLASS_PASSWORD},
    )
    token = login.json()["access_token"]

    response = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["is_breakglass"] is True
    # Emergency access is always a platform admin (ADR 0028).
    assert body["assignments"] == [{"role": "platform_admin", "scope_type": "global", "scope_id": None}]


def test_whoami_without_token_rejected(client):
    response = client.get("/api/v1/auth/whoami")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_whoami_with_garbage_token_rejected(client):
    response = client.get("/api/v1/auth/whoami", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"
