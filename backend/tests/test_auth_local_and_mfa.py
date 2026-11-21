"""Local-user login, password change, TOTP, and recovery-code MFA. Real
end-to-end: real bcrypt password hashing, real pyotp TOTP generation
against the same secret the server enrolled, real Fernet-encrypted secret
storage round-trip. WebAuthn is covered separately (test_auth_webauthn.py)
with the underlying `webauthn` library's own crypto verification mocked
at the boundary — that library's cryptographic correctness isn't
RESURGAM's to re-test; what matters here is RESURGAM's own wiring around
it (ceremony tokens, storage, error paths).
"""

import pyotp


def _create_local_user(client, headers, email="alice@example.com", password="correct horse battery") -> str:
    response = client.post(
        "/api/v1/users",
        json={"email": email, "display_name": "Alice", "password": password},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_local_login_without_mfa_issues_session(client, platform_admin_headers):
    _create_local_user(client, platform_admin_headers)

    response = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is False
    assert body["access_token"] is not None

    whoami = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert whoami.status_code == 200
    assert whoami.json()["email"] == "alice@example.com"


def test_local_login_wrong_password_rejected(client, platform_admin_headers):
    _create_local_user(client, platform_admin_headers)
    response = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "wrong password"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_local_login_unknown_email_same_error_as_wrong_password(client):
    response = client.post("/api/v1/auth/local-login", json={"email": "nobody@example.com", "password": "whatever"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_local_login_disabled_user_rejected(client, platform_admin_headers):
    user_id = _create_local_user(client, platform_admin_headers)
    client.post(f"/api/v1/users/{user_id}/disable", headers=platform_admin_headers)

    response = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    assert response.status_code == 401


def _local_headers(client, platform_admin_headers, email="alice@example.com", password="correct horse battery"):
    _create_local_user(client, platform_admin_headers, email=email, password=password)
    login = client.post("/api/v1/auth/local-login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_change_password_round_trip(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)

    change_response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "correct horse battery", "new_password": "new correct horse"},
        headers=headers,
    )
    assert change_response.status_code == 204

    old_login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "new correct horse"}
    )
    assert new_login.status_code == 200


def test_change_password_wrong_current_password_rejected(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)
    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "not the password", "new_password": "new correct horse"},
        headers=headers,
    )
    assert response.status_code == 401


def test_change_password_rejected_for_breakglass_account(client, platform_admin_headers):
    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "test-password", "new_password": "whatever"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "no_local_password"


def test_totp_enroll_confirm_and_login(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)

    enroll = client.post("/api/v1/mfa/totp/enroll", headers=headers)
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]
    assert enroll.json()["otpauth_uri"].startswith("otpauth://totp/")

    status_before = client.get("/api/v1/mfa/status", headers=headers)
    assert status_before.json()["totp_enabled"] is False

    confirm = client.post("/api/v1/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=headers)
    assert confirm.status_code == 204

    status_after = client.get("/api/v1/mfa/status", headers=headers)
    assert status_after.json()["totp_enabled"] is True

    # Now local-login must hand off to an MFA challenge instead of a session.
    login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    assert login.status_code == 200
    body = login.json()
    assert body["mfa_required"] is True
    assert body["access_token"] is None
    assert "totp" in body["available_methods"]

    verify = client.post(
        "/api/v1/auth/mfa/verify-totp",
        json={"challenge_token": body["challenge_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert verify.status_code == 200
    session_token = verify.json()["access_token"]
    whoami = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {session_token}"})
    assert whoami.status_code == 200
    assert whoami.json()["email"] == "alice@example.com"


def test_totp_confirm_wrong_code_rejected(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)
    client.post("/api/v1/mfa/totp/enroll", headers=headers)
    response = client.post("/api/v1/mfa/totp/confirm", json={"code": "000000"}, headers=headers)
    assert response.status_code == 401


def test_verify_totp_wrong_code_rejected(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)
    secret = client.post("/api/v1/mfa/totp/enroll", headers=headers).json()["secret"]
    client.post("/api/v1/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=headers)

    login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    challenge_token = login.json()["challenge_token"]

    response = client.post("/api/v1/auth/mfa/verify-totp", json={"challenge_token": challenge_token, "code": "000000"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_code"


def test_totp_remove_disables_mfa_requirement(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)
    secret = client.post("/api/v1/mfa/totp/enroll", headers=headers).json()["secret"]
    client.post("/api/v1/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=headers)

    remove = client.delete("/api/v1/mfa/totp", headers=headers)
    assert remove.status_code == 204

    login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    assert login.json()["mfa_required"] is False


def test_recovery_codes_require_a_primary_method_first(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)
    response = client.post("/api/v1/mfa/recovery-codes/generate", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "no_primary_mfa_method"


def test_recovery_code_login_and_single_use(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)
    secret = client.post("/api/v1/mfa/totp/enroll", headers=headers).json()["secret"]
    client.post("/api/v1/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=headers)

    codes_response = client.post("/api/v1/mfa/recovery-codes/generate", headers=headers)
    assert codes_response.status_code == 200
    codes = codes_response.json()["codes"]
    assert len(codes) == 10

    status = client.get("/api/v1/mfa/status", headers=headers)
    assert status.json()["recovery_codes_remaining"] == 10

    login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    challenge_token = login.json()["challenge_token"]
    assert "recovery_code" in login.json()["available_methods"]

    first_use = client.post(
        "/api/v1/auth/mfa/verify-recovery-code",
        json={"challenge_token": challenge_token, "code": codes[0]},
    )
    assert first_use.status_code == 200

    # A used code must not work a second time, even against a fresh challenge.
    login2 = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    reuse = client.post(
        "/api/v1/auth/mfa/verify-recovery-code",
        json={"challenge_token": login2.json()["challenge_token"], "code": codes[0]},
    )
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "invalid_code"


def test_regenerating_recovery_codes_invalidates_old_ones(client, platform_admin_headers):
    headers = _local_headers(client, platform_admin_headers)
    secret = client.post("/api/v1/mfa/totp/enroll", headers=headers).json()["secret"]
    client.post("/api/v1/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=headers)

    first_batch = client.post("/api/v1/mfa/recovery-codes/generate", headers=headers).json()["codes"]
    second_batch = client.post("/api/v1/mfa/recovery-codes/generate", headers=headers).json()["codes"]
    assert set(first_batch) != set(second_batch)

    login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    response = client.post(
        "/api/v1/auth/mfa/verify-recovery-code",
        json={"challenge_token": login.json()["challenge_token"], "code": first_batch[0]},
    )
    assert response.status_code == 401


def test_breakglass_login_always_holds_platform_admin(client, db_session):
    """Emergency access must be able to fix a lockout (e.g. Reset MFA), so the
    break-glass account gets global platform_admin at every login (ADR 0028)."""
    from resurgam_api.models.identity import Role, RoleKey, UserRoleAssignment
    from tests.conftest import TEST_BREAKGLASS_PASSWORD, TEST_BREAKGLASS_USERNAME

    def login():
        response = client.post(
            "/api/v1/auth/breakglass-login",
            json={"username": TEST_BREAKGLASS_USERNAME, "password": TEST_BREAKGLASS_PASSWORD},
        )
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    headers = login()
    assert client.get("/api/v1/mfa-policy", headers=headers).status_code == 200

    # Someone removed it: the next break-glass login grants it again.
    role = db_session.query(Role).filter_by(key=RoleKey.PLATFORM_ADMIN.value).one()
    db_session.query(UserRoleAssignment).filter_by(role_id=role.id).delete()
    db_session.commit()
    headers = login()
    assert client.get("/api/v1/mfa-policy", headers=headers).status_code == 200
