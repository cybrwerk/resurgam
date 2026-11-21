"""WebAuthn (FIDO2) registration and login. The underlying cryptographic
verification (attestation/assertion signature checking) belongs to the
`webauthn` PyPI library, not RESURGAM — re-testing it here would mean
hand-rolling a fake authenticator's CBOR/COSE encoding, which tests the
library, not this codebase. Instead, `webauthn.verify_registration_response`/
`verify_authentication_response` are monkeypatched at the call site
(module-level, so every real request/response field around them is
exercised for real): ceremony-token round-tripping (a stateless JWT
carrying the server challenge, since there's no session store), DB
storage, sign_count persistence, and every error path RESURGAM itself is
responsible for.
"""

import types

import webauthn


def _create_local_user_and_login(client, platform_admin_headers, email="alice@example.com") -> dict:
    client.post(
        "/api/v1/users",
        json={"email": email, "display_name": "Alice", "password": "correct horse battery"},
        headers=platform_admin_headers,
    )
    login = client.post("/api/v1/auth/local-login", json={"email": email, "password": "correct horse battery"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _register_fake_credential(client, headers, monkeypatch, *, credential_id: bytes, nickname="YubiKey"):
    options_response = client.post(
        "/api/v1/mfa/webauthn/register-options", json={"nickname": nickname}, headers=headers
    )
    assert options_response.status_code == 200
    ceremony_token = options_response.json()["ceremony_token"]

    fake_result = types.SimpleNamespace(
        credential_id=credential_id, credential_public_key=b"fake-public-key-bytes", sign_count=0
    )
    monkeypatch.setattr(webauthn, "verify_registration_response", lambda **kwargs: fake_result)

    register_response = client.post(
        "/api/v1/mfa/webauthn/register",
        json={"ceremony_token": ceremony_token, "credential": {"id": "irrelevant-mocked", "type": "public-key"}},
        headers=headers,
    )
    assert register_response.status_code == 200
    return register_response.json()


def test_register_options_then_register_stores_credential(client, platform_admin_headers, monkeypatch):
    headers = _create_local_user_and_login(client, platform_admin_headers)

    body = _register_fake_credential(client, headers, monkeypatch, credential_id=b"credential-one-bytes")
    assert body["nickname"] == "YubiKey"

    status = client.get("/api/v1/mfa/status", headers=headers)
    assert len(status.json()["webauthn_credentials"]) == 1
    assert status.json()["webauthn_credentials"][0]["nickname"] == "YubiKey"


def test_register_options_excludes_existing_credentials(client, platform_admin_headers, monkeypatch):
    headers = _create_local_user_and_login(client, platform_admin_headers)
    _register_fake_credential(client, headers, monkeypatch, credential_id=b"credential-one-bytes")

    captured = {}
    real_generate = webauthn.generate_registration_options

    def spy(**kwargs):
        captured["exclude_credentials"] = kwargs.get("exclude_credentials")
        return real_generate(**kwargs)

    monkeypatch.setattr(webauthn, "generate_registration_options", spy)
    client.post("/api/v1/mfa/webauthn/register-options", json={"nickname": "Second key"}, headers=headers)

    assert len(captured["exclude_credentials"]) == 1


def test_remove_credential(client, platform_admin_headers, monkeypatch):
    headers = _create_local_user_and_login(client, platform_admin_headers)
    credential = _register_fake_credential(client, headers, monkeypatch, credential_id=b"credential-one-bytes")

    remove_response = client.delete(f"/api/v1/mfa/webauthn/{credential['id']}", headers=headers)
    assert remove_response.status_code == 204

    status = client.get("/api/v1/mfa/status", headers=headers)
    assert status.json()["webauthn_credentials"] == []


def test_login_requires_webauthn_and_succeeds(client, platform_admin_headers, monkeypatch):
    headers = _create_local_user_and_login(client, platform_admin_headers)
    _register_fake_credential(client, headers, monkeypatch, credential_id=b"credential-one-bytes")

    login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    assert login.status_code == 200
    body = login.json()
    assert body["mfa_required"] is True
    assert "webauthn" in body["available_methods"]
    challenge_token = body["challenge_token"]

    options_response = client.post(
        "/api/v1/auth/mfa/webauthn/authenticate-options", json={"challenge_token": challenge_token}
    )
    assert options_response.status_code == 200
    ceremony_token = options_response.json()["ceremony_token"]
    credential_id_b64url = webauthn.helpers.bytes_to_base64url(b"credential-one-bytes")

    fake_result = types.SimpleNamespace(new_sign_count=7)
    monkeypatch.setattr(webauthn, "verify_authentication_response", lambda **kwargs: fake_result)

    authenticate_response = client.post(
        "/api/v1/auth/mfa/webauthn/authenticate",
        json={"ceremony_token": ceremony_token, "credential": {"id": credential_id_b64url, "type": "public-key"}},
    )
    assert authenticate_response.status_code == 200
    session_token = authenticate_response.json()["access_token"]

    whoami = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {session_token}"})
    assert whoami.status_code == 200
    assert whoami.json()["email"] == "alice@example.com"


def test_authenticate_rejects_unknown_credential_id(client, platform_admin_headers, monkeypatch):
    headers = _create_local_user_and_login(client, platform_admin_headers)
    _register_fake_credential(client, headers, monkeypatch, credential_id=b"credential-one-bytes")

    login = client.post(
        "/api/v1/auth/local-login", json={"email": "alice@example.com", "password": "correct horse battery"}
    )
    options_response = client.post(
        "/api/v1/auth/mfa/webauthn/authenticate-options",
        json={"challenge_token": login.json()["challenge_token"]},
    )
    ceremony_token = options_response.json()["ceremony_token"]

    response = client.post(
        "/api/v1/auth/mfa/webauthn/authenticate",
        json={"ceremony_token": ceremony_token, "credential": {"id": "not-a-real-credential-id", "type": "public-key"}},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credential"


def test_register_ceremony_token_cannot_be_used_for_authentication(client, platform_admin_headers, monkeypatch):
    headers = _create_local_user_and_login(client, platform_admin_headers)
    options_response = client.post("/api/v1/mfa/webauthn/register-options", json={"nickname": "Key"}, headers=headers)
    register_ceremony_token = options_response.json()["ceremony_token"]

    response = client.post(
        "/api/v1/auth/mfa/webauthn/authenticate",
        json={"ceremony_token": register_ceremony_token, "credential": {"id": "x", "type": "public-key"}},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_challenge"
