"""Mandatory MFA: an admin requires MFA for a local account; the next login
without a second factor gets only an enrollment token, which reaches the
enrollment endpoints and nothing else, and is swapped for a session once a
factor is proven. The last factor can't be removed while required; an admin
can reset a user's factors."""

import pyotp

PASSWORD = "correct horse battery"


def _create(client, headers, email="alice@example.com", **extra) -> str:
    response = client.post(
        "/api/v1/users", json={"email": email, "display_name": "Alice", "password": PASSWORD, **extra}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _login(client, email="alice@example.com"):
    return client.post("/api/v1/auth/local-login", json={"email": email, "password": PASSWORD})


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _enroll_totp(client, headers) -> str:
    secret = client.post("/api/v1/mfa/totp/enroll", headers=headers).json()["secret"]
    confirm = client.post("/api/v1/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=headers)
    assert confirm.status_code == 204, confirm.text
    return secret


def test_required_mfa_forces_enrollment_then_issues_session(client, platform_admin_headers):
    user_id = _create(client, platform_admin_headers)
    response = client.post(
        f"/api/v1/users/{user_id}/mfa-requirement", json={"required": True}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["mfa_required"] is True
    assert response.json()["mfa_enrolled"] is False

    body = _login(client).json()
    assert body["mfa_enrollment_required"] is True
    assert body["access_token"] is None and body["mfa_required"] is False
    enrollment = _bearer(body["enrollment_token"])

    # The enrollment token reaches no business endpoint, and can't be exchanged yet.
    assert client.get("/api/v1/auth/whoami", headers=enrollment).status_code == 401
    assert client.get("/api/v1/customers", headers=enrollment).status_code == 401
    early = client.post("/api/v1/auth/mfa/enrollment/complete", json={"enrollment_token": body["enrollment_token"]})
    assert early.status_code == 400
    assert early.json()["code"] == "mfa_not_enrolled"

    assert client.get("/api/v1/mfa/status", headers=enrollment).json()["mfa_required"] is True
    _enroll_totp(client, enrollment)
    codes = client.post("/api/v1/mfa/recovery-codes/generate", headers=enrollment)
    assert codes.status_code == 200 and len(codes.json()["codes"]) > 0

    done = client.post("/api/v1/auth/mfa/enrollment/complete", json={"enrollment_token": body["enrollment_token"]})
    assert done.status_code == 200, done.text
    whoami = client.get("/api/v1/auth/whoami", headers=_bearer(done.json()["access_token"]))
    assert whoami.json()["email"] == "alice@example.com"

    # The next login is a normal MFA challenge.
    again = _login(client).json()
    assert again["mfa_required"] is True and again["mfa_enrollment_required"] is False

    listed = client.get("/api/v1/users", headers=platform_admin_headers).json()
    alice = next(u for u in listed if u["email"] == "alice@example.com")
    assert alice["mfa_required"] is True and alice["mfa_enrolled"] is True


def test_mfa_required_at_creation(client, platform_admin_headers):
    _create(client, platform_admin_headers, mfa_required=True)
    assert _login(client).json()["mfa_enrollment_required"] is True


def test_not_required_logs_in_without_mfa(client, platform_admin_headers):
    _create(client, platform_admin_headers)
    body = _login(client).json()
    assert body["access_token"] and body["mfa_enrollment_required"] is False


def test_enrollment_token_cannot_remove_factors_or_act_as_session(client, platform_admin_headers):
    _create(client, platform_admin_headers, mfa_required=True)
    enrollment = _bearer(_login(client).json()["enrollment_token"])
    assert client.delete("/api/v1/mfa/totp", headers=enrollment).status_code == 401
    assert client.post("/api/v1/auth/change-password", json={}, headers=enrollment).status_code in (401, 422)


def test_last_factor_cannot_be_removed_while_required(client, platform_admin_headers):
    user_id = _create(client, platform_admin_headers)
    session = _bearer(_login(client).json()["access_token"])
    _enroll_totp(client, session)
    client.post(f"/api/v1/users/{user_id}/mfa-requirement", json={"required": True}, headers=platform_admin_headers)

    refused = client.delete("/api/v1/mfa/totp", headers=session)
    assert refused.status_code == 409
    assert refused.json()["code"] == "mfa_required_last_factor"

    client.post(f"/api/v1/users/{user_id}/mfa-requirement", json={"required": False}, headers=platform_admin_headers)
    assert client.delete("/api/v1/mfa/totp", headers=session).status_code == 204


def test_admin_reset_mfa_removes_all_factors(client, platform_admin_headers, db_session):
    from resurgam_api.models.mfa import RecoveryCode, TotpCredential

    user_id = _create(client, platform_admin_headers, mfa_required=True)
    token = _login(client).json()["enrollment_token"]
    _enroll_totp(client, _bearer(token))
    client.post("/api/v1/mfa/recovery-codes/generate", headers=_bearer(token))

    response = client.post(f"/api/v1/users/{user_id}/reset-mfa", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["mfa_enrolled"] is False
    assert db_session.query(TotpCredential).filter_by(user_id=user_id).count() == 0
    assert db_session.query(RecoveryCode).filter_by(user_id=user_id).count() == 0
    assert _login(client).json()["mfa_enrollment_required"] is True


def test_requirement_refused_for_breakglass_and_sso_users(client, platform_admin_headers, db_session):
    from resurgam_api.models.identity import User

    users = client.get("/api/v1/users", headers=platform_admin_headers).json()
    breakglass = next(u for u in users if u["is_breakglass"])
    response = client.post(
        f"/api/v1/users/{breakglass['id']}/mfa-requirement", json={"required": True}, headers=platform_admin_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "mfa_not_applicable"

    sso = User(email="sso@example.com", display_name="SSO", external_subject="sub-1")
    db_session.add(sso)
    db_session.commit()
    response = client.post(f"/api/v1/users/{sso.id}/reset-mfa", headers=platform_admin_headers)
    assert response.status_code == 409


def test_admin_cannot_reset_own_mfa(client, platform_admin_headers, db_session):
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, UserRoleAssignment

    admin_id = _create(client, platform_admin_headers, email="admin2@example.com")
    role = db_session.query(Role).filter_by(key=RoleKey.PLATFORM_ADMIN.value).one()
    db_session.add(UserRoleAssignment(user_id=admin_id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    session = _bearer(_login(client, "admin2@example.com").json()["access_token"])
    response = client.post(f"/api/v1/users/{admin_id}/reset-mfa", headers=session)
    assert response.status_code == 409
    assert response.json()["code"] == "cannot_reset_own_mfa"


def test_requirement_endpoints_need_platform_admin(client, platform_admin_headers):
    user_id = _create(client, platform_admin_headers)
    session = _bearer(_login(client).json()["access_token"])
    assert (
        client.post(f"/api/v1/users/{user_id}/mfa-requirement", json={"required": True}, headers=session).status_code
        == 403
    )
    assert client.post(f"/api/v1/users/{user_id}/reset-mfa", headers=session).status_code == 403


# --- global policy -------------------------------------------------------------------


def test_global_policy_forces_enrollment_for_every_local_user(client, platform_admin_headers):
    _create(client, platform_admin_headers)
    assert client.get("/api/v1/mfa-policy", headers=platform_admin_headers).json() == {
        "require_mfa_all_local_users": False
    }
    response = client.put(
        "/api/v1/mfa-policy", json={"require_mfa_all_local_users": True}, headers=platform_admin_headers
    )
    assert response.status_code == 200 and response.json()["require_mfa_all_local_users"] is True

    body = _login(client).json()
    assert body["mfa_enrollment_required"] is True

    users = client.get("/api/v1/users", headers=platform_admin_headers).json()
    alice = next(u for u in users if u["email"] == "alice@example.com")
    breakglass = next(u for u in users if u["is_breakglass"])
    assert alice["mfa_required"] is False and alice["mfa_required_effective"] is True
    assert breakglass["mfa_required_effective"] is False

    # Break-glass login is never affected.
    client.put("/api/v1/mfa-policy", json={"require_mfa_all_local_users": False}, headers=platform_admin_headers)
    assert _login(client).json()["access_token"]


def test_global_policy_protects_the_last_factor(client, platform_admin_headers):
    _create(client, platform_admin_headers)
    session = _bearer(_login(client).json()["access_token"])
    _enroll_totp(client, session)
    client.put("/api/v1/mfa-policy", json={"require_mfa_all_local_users": True}, headers=platform_admin_headers)
    assert client.get("/api/v1/mfa/status", headers=session).json()["mfa_required"] is True
    assert client.delete("/api/v1/mfa/totp", headers=session).json()["code"] == "mfa_required_last_factor"


def test_global_policy_requires_platform_admin(client, platform_admin_headers):
    _create(client, platform_admin_headers)
    session = _bearer(_login(client).json()["access_token"])
    assert client.get("/api/v1/mfa-policy", headers=session).status_code == 403
    assert (
        client.put("/api/v1/mfa-policy", json={"require_mfa_all_local_users": True}, headers=session).status_code == 403
    )


# --- enrollment-token replay protection (ADR 0028) ------------------------------------


def _complete(client, token: str):
    return client.post("/api/v1/auth/mfa/enrollment/complete", json={"enrollment_token": token})


def test_enrollment_token_is_single_use(client, platform_admin_headers):
    _create(client, platform_admin_headers, mfa_required=True)
    token = _login(client).json()["enrollment_token"]
    _enroll_totp(client, _bearer(token))
    first = client.post("/api/v1/mfa/recovery-codes/generate", headers=_bearer(token))
    assert first.status_code == 200
    assert _complete(client, token).status_code == 200

    # Replayed after the exchange: no second session, no codes, no extra key.
    second = _complete(client, token)
    assert second.status_code == 401 and second.json()["code"] == "invalid_enrollment_token"
    assert client.post("/api/v1/mfa/recovery-codes/generate", headers=_bearer(token)).status_code == 401
    options = client.post("/api/v1/mfa/webauthn/register-options", json={"nickname": "x"}, headers=_bearer(token))
    assert options.status_code == 401
    assert client.get("/api/v1/mfa/status", headers=_bearer(token)).status_code == 401


def test_stale_enrollment_token_refused_after_another_enrollment(client, platform_admin_headers):
    _create(client, platform_admin_headers, mfa_required=True)
    stale = _login(client).json()["enrollment_token"]
    victim = _login(client).json()["enrollment_token"]
    _enroll_totp(client, _bearer(victim))

    def stale_refused():
        assert _complete(client, stale).status_code == 401
        assert client.post("/api/v1/mfa/recovery-codes/generate", headers=_bearer(stale)).status_code == 401
        options = client.post("/api/v1/mfa/webauthn/register-options", json={"nickname": "x"}, headers=_bearer(stale))
        assert options.status_code == 401

    # Before and after the victim finishes, the older token reaches nothing.
    stale_refused()
    assert _complete(client, victim).status_code == 200
    stale_refused()


def test_recovery_codes_only_once_per_enrollment(client, platform_admin_headers):
    _create(client, platform_admin_headers, mfa_required=True)
    token = _login(client).json()["enrollment_token"]
    _enroll_totp(client, _bearer(token))
    assert client.post("/api/v1/mfa/recovery-codes/generate", headers=_bearer(token)).status_code == 200
    again = client.post("/api/v1/mfa/recovery-codes/generate", headers=_bearer(token))
    assert again.status_code == 403 and again.json()["code"] == "recovery_codes_need_session"
    # A real session can always regenerate.
    session = _bearer(_complete(client, token).json()["access_token"])
    assert client.post("/api/v1/mfa/recovery-codes/generate", headers=session).status_code == 200


def test_enrollment_token_refused_once_a_factor_exists_from_elsewhere(client, platform_admin_headers):
    user_id = _create(client, platform_admin_headers)
    client.post(f"/api/v1/users/{user_id}/mfa-requirement", json={"required": True}, headers=platform_admin_headers)
    token_holder = _login(client).json()["enrollment_token"]
    # The admin lifts the requirement, the user signs in normally and adds a factor on the profile page.
    client.post(f"/api/v1/users/{user_id}/mfa-requirement", json={"required": False}, headers=platform_admin_headers)
    session = _bearer(_login(client).json()["access_token"])
    _enroll_totp(client, session)
    assert _complete(client, token_holder).status_code == 401
    assert client.post("/api/v1/mfa/recovery-codes/generate", headers=_bearer(token_holder)).status_code == 401


def test_enrollment_token_from_before_a_reset_cannot_finish_the_new_enrollment(client, platform_admin_headers):
    user_id = _create(client, platform_admin_headers, mfa_required=True)
    old = _login(client).json()["enrollment_token"]
    _enroll_totp(client, _bearer(old))  # never completed
    client.post(f"/api/v1/users/{user_id}/reset-mfa", headers=platform_admin_headers)
    new = _login(client).json()["enrollment_token"]
    _enroll_totp(client, _bearer(new))
    assert _complete(client, old).status_code == 401
    assert _complete(client, new).status_code == 200


def test_reset_possible_with_only_unconfirmed_totp_or_recovery_codes(client, platform_admin_headers, db_session):
    from resurgam_api.models.mfa import RecoveryCode

    user_id = _create(client, platform_admin_headers)
    session = _bearer(_login(client).json()["access_token"])

    def alice():
        users = client.get("/api/v1/users", headers=platform_admin_headers).json()
        return next(u for u in users if u["email"] == "alice@example.com")

    assert alice()["mfa_reset_possible"] is False
    client.post("/api/v1/mfa/totp/enroll", headers=session)  # unconfirmed
    assert alice()["mfa_reset_possible"] is True and alice()["mfa_enrolled"] is False
    client.post(f"/api/v1/users/{user_id}/reset-mfa", headers=platform_admin_headers)
    db_session.add(RecoveryCode(user_id=user_id, code_hash="x"))
    db_session.commit()
    assert alice()["mfa_reset_possible"] is True
