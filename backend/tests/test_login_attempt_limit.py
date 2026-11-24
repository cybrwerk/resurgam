"""Wrong passwords and codes are limited (`auth.attempt_limit`): 5 failures
within 15 minutes lock that account -- even the right password is refused
until the window has passed. Counted from the audit log; a successful sign-in
starts the count again."""

import pyotp

from resurgam_api.auth import attempt_limit
from tests.conftest import TEST_BREAKGLASS_PASSWORD, TEST_BREAKGLASS_USERNAME

PASSWORD = "correct horse battery"


def _create_user(client, admin_headers, email="alice@example.com"):
    response = client.post(
        "/api/v1/users", json={"email": email, "display_name": "Alice", "password": PASSWORD}, headers=admin_headers
    )
    assert response.status_code == 200, response.text


def _login(client, email="alice@example.com", password=PASSWORD):
    return client.post("/api/v1/auth/local-login", json={"email": email, "password": password})


def _fail(client, count, email="alice@example.com"):
    return [_login(client, email, "wrong password") for _ in range(count)]


def test_local_login_locks_after_five_wrong_passwords(client, platform_admin_headers):
    _create_user(client, platform_admin_headers)
    _create_user(client, platform_admin_headers, email="bob@example.com")

    answers = _fail(client, 5)
    assert [(a.status_code, a.json()["code"]) for a in answers[:4]] == [(401, "invalid_credentials")] * 4
    assert [a.json()["details"]["attempts_left"] for a in answers[:4]] == [4, 3, 2, 1]
    assert (answers[4].status_code, answers[4].json()["code"]) == (429, "login_locked")
    assert 0 < answers[4].json()["details"]["retry_after_seconds"] <= attempt_limit.WINDOW_SECONDS

    locked = _login(client)
    assert (locked.status_code, locked.json()["code"]) == (429, "login_locked")
    # The address is compared case-insensitively, so changing the case does not get around it.
    assert _login(client, "Alice@Example.COM").status_code == 429
    # Other accounts are not affected.
    assert _login(client, "bob@example.com").status_code == 200


def test_unknown_addresses_lock_like_real_ones(client):
    """Otherwise the lock would tell which addresses have an account."""
    answers = _fail(client, 5, email="nobody@example.com")
    assert [a.status_code for a in answers] == [401, 401, 401, 401, 429]


def test_a_successful_login_starts_the_count_again(client, platform_admin_headers):
    _create_user(client, platform_admin_headers)
    _fail(client, 4)
    assert _login(client).status_code == 200
    again = _fail(client, 4)
    assert [a.status_code for a in again] == [401] * 4
    assert again[-1].json()["details"]["attempts_left"] == 1


def test_lock_ends_when_the_window_has_passed(client, platform_admin_headers, monkeypatch):
    _create_user(client, platform_admin_headers)
    _fail(client, 5)
    assert _login(client).status_code == 429
    monkeypatch.setattr(attempt_limit, "WINDOW_SECONDS", 0)
    assert _login(client).status_code == 200


def test_breakglass_login_locks_after_five_wrong_passwords(client):
    url = "/api/v1/auth/breakglass-login"
    answers = [client.post(url, json={"username": TEST_BREAKGLASS_USERNAME, "password": "wrong"}) for _ in range(5)]
    assert [a.status_code for a in answers] == [401, 401, 401, 401, 429]
    right = client.post(url, json={"username": TEST_BREAKGLASS_USERNAME, "password": TEST_BREAKGLASS_PASSWORD})
    assert (right.status_code, right.json()["code"]) == (429, "login_locked")


def test_second_factor_codes_are_limited_per_account(client, platform_admin_headers):
    _create_user(client, platform_admin_headers)
    headers = {"Authorization": f"Bearer {_login(client).json()['access_token']}"}
    secret = client.post("/api/v1/mfa/totp/enroll", headers=headers).json()["secret"]
    client.post("/api/v1/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=headers)

    challenge = _login(client).json()["challenge_token"]
    wrong_code = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
    answers = [
        client.post("/api/v1/auth/mfa/verify-totp", json={"challenge_token": challenge, "code": wrong_code})
        for _ in range(5)
    ]
    assert [a.status_code for a in answers] == [401, 401, 401, 401, 429]
    right = client.post(
        "/api/v1/auth/mfa/verify-totp", json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()}
    )
    assert (right.status_code, right.json()["code"]) == (429, "login_locked")
    # Recovery codes share the count: no fresh guesses by switching method.
    recovery = client.post(
        "/api/v1/auth/mfa/verify-recovery-code", json={"challenge_token": challenge, "code": "aaaa-bbbb"}
    )
    assert recovery.status_code == 429


def test_password_change_is_limited_and_audited(client, platform_admin_headers, db_session):
    from resurgam_api.models.audit import AuditEvent

    _create_user(client, platform_admin_headers)
    headers = {"Authorization": f"Bearer {_login(client).json()['access_token']}"}
    url = "/api/v1/auth/change-password"
    body = {"current_password": "wrong password", "new_password": "a brand new password 1"}
    answers = [client.post(url, json=body, headers=headers) for _ in range(5)]
    assert [a.status_code for a in answers] == [401, 401, 401, 401, 429]
    assert answers[4].json()["code"] == "password_change_locked"
    right = client.post(url, json={**body, "current_password": PASSWORD}, headers=headers)
    assert right.status_code == 429
    assert db_session.query(AuditEvent).filter_by(action="password_change_failed").count() == 5
