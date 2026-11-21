"""Tests for the Credentials admin page's backend
(`GET /credentials`, `POST /credentials/{id}/rotate`). The one thing that
actually matters here beyond ordinary CRUD correctness: `SecretReference`
also holds per-user TOTP secrets (`mfa.py`), and this surface must never
list or rotate one of those, even though they live in the same table as
PBS/PVE tokens.
"""

import uuid


def _create_pbs_instance(client, headers, **overrides) -> str:
    payload = {
        "display_name": "Linked PBS",
        "host": "pbs1.internal.example",
        "port": 8007,
        "tls_trust_mode": "ca",
        "api_token": "test@pbs!token:secretvalue",
    }
    payload.update(overrides)
    response = client.post("/api/v1/pbs-instances", json=payload, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def _create_pve_cluster(client, headers, **overrides) -> str:
    payload = {
        "display_name": "Primary PVE",
        "host": "pve1.internal.example",
        "port": 8006,
        "tls_trust_mode": "ca",
        "api_token": "root@pam!token=secretvalue",
    }
    payload.update(overrides)
    response = client.post("/api/v1/pve-clusters", json=payload, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def _create_viewer_headers(client, db_session, test_settings) -> dict:
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def _create_totp_secret_reference(db_session) -> str:
    """Simulates what `mfa.py` actually creates -- a `SecretReference` not
    referenced by any `PbsInstance`/`PveCluster`, same shape as a real
    user's TOTP secret. Returns its id.
    """
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(
        reference_key=f"totp-secret-{uuid.uuid4()}",
        description="TOTP secret for user 'someone@example.com'",
        purpose=SecretPurpose.MFA_TOTP,
    )
    db_session.add(ref)
    db_session.commit()
    return str(ref.id)


def test_list_credentials_includes_pbs_and_pve_with_owner_info(client, platform_admin_headers, db_session):
    pbs_id = _create_pbs_instance(client, platform_admin_headers)
    pve_id = _create_pve_cluster(client, platform_admin_headers)

    response = client.get("/api/v1/credentials", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2

    by_owner_type = {c["owner_type"]: c for c in body}
    assert by_owner_type["pbs_instance"]["owner_id"] == pbs_id
    assert by_owner_type["pbs_instance"]["owner_label"] == "Linked PBS"
    assert by_owner_type["pve_cluster"]["owner_id"] == pve_id
    assert by_owner_type["pve_cluster"]["owner_label"] == "Primary PVE"

    # Never the decrypted value, only the pointer/metadata.
    for c in body:
        assert "api_token" not in c
        assert "value" not in c
        assert "secretvalue" not in str(c)


def test_list_credentials_excludes_mfa_totp_secrets(client, platform_admin_headers, db_session):
    _create_pbs_instance(client, platform_admin_headers)
    _create_totp_secret_reference(db_session)

    response = client.get("/api/v1/credentials", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["owner_type"] == "pbs_instance"
    assert not any("TOTP" in c["description"] for c in body)


def test_rotate_credential_updates_what_the_provider_decrypts(
    client, platform_admin_headers, db_session, test_settings
):
    from resurgam_api.models.pbs import PbsInstance
    from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

    pbs_id = _create_pbs_instance(client, platform_admin_headers)

    list_response = client.get("/api/v1/credentials", headers=platform_admin_headers)
    secret_reference_id = list_response.json()[0]["secret_reference_id"]

    response = client.post(
        f"/api/v1/credentials/{secret_reference_id}/rotate",
        json={"new_value": "test@pbs!token:rotated-secret-value"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["owner_id"] == pbs_id

    instance = db_session.get(PbsInstance, uuid.UUID(pbs_id))
    from resurgam_api.models.secrets import SecretReference

    secret_ref = db_session.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key)
    assert provider.retrieve(secret_ref.reference_key) == "test@pbs!token:rotated-secret-value"


def test_list_credentials_includes_last_rotated_at_defaulting_to_created_at(client, platform_admin_headers, db_session):
    """A real UX-audit finding (2026-09-24): the page's own description
    promised "wann er zuletzt rotiert wurde" but no such field existed.
    A never-rotated credential's last_rotated_at equals its created_at
    -- the value in place right now was set then.
    """
    _create_pbs_instance(client, platform_admin_headers)

    response = client.get("/api/v1/credentials", headers=platform_admin_headers)
    credential = response.json()[0]
    assert credential["last_rotated_at"] == credential["created_at"]


def test_rotate_credential_updates_last_rotated_at(client, platform_admin_headers, db_session):
    _create_pbs_instance(client, platform_admin_headers)
    list_response = client.get("/api/v1/credentials", headers=platform_admin_headers)
    before = list_response.json()[0]
    secret_reference_id = before["secret_reference_id"]

    rotate_response = client.post(
        f"/api/v1/credentials/{secret_reference_id}/rotate",
        json={"new_value": "test@pbs!token:rotated-secret-value"},
        headers=platform_admin_headers,
    )
    after = rotate_response.json()
    assert after["last_rotated_at"] != before["last_rotated_at"]
    assert after["created_at"] == before["created_at"]

    listed_after = client.get("/api/v1/credentials", headers=platform_admin_headers).json()[0]
    assert listed_after["last_rotated_at"] == after["last_rotated_at"]


def test_updating_api_token_via_pbs_instance_patch_also_updates_last_rotated_at(
    client, platform_admin_headers, db_session
):
    """The dedicated /credentials/{id}/rotate endpoint isn't the only way
    a token changes -- PbsInstance/PveCluster's own PATCH endpoints can
    too (rotate_credential's own docstring: "additive, not a
    replacement"). Both paths must keep last_rotated_at accurate.
    """
    pbs_id = _create_pbs_instance(client, platform_admin_headers)
    before = client.get("/api/v1/credentials", headers=platform_admin_headers).json()[0]

    patch_response = client.patch(
        f"/api/v1/pbs-instances/{pbs_id}",
        json={"api_token": "test@pbs!token:rotated-via-patch"},
        headers=platform_admin_headers,
    )
    assert patch_response.status_code == 200

    after = client.get("/api/v1/credentials", headers=platform_admin_headers).json()[0]
    assert after["last_rotated_at"] != before["last_rotated_at"]


def test_rotate_credential_records_audit_event(client, platform_admin_headers, db_session):
    _create_pbs_instance(client, platform_admin_headers)
    from resurgam_api.models.audit import AuditEvent

    list_response = client.get("/api/v1/credentials", headers=platform_admin_headers)
    secret_reference_id = list_response.json()[0]["secret_reference_id"]

    client.post(
        f"/api/v1/credentials/{secret_reference_id}/rotate",
        json={"new_value": "test@pbs!token:rotated"},
        headers=platform_admin_headers,
    )

    event = db_session.query(AuditEvent).filter_by(action="credential_rotated", target_id=secret_reference_id).one()
    assert event.detail["owner_type"] == "pbs_instance"


def test_rotate_rejects_a_secret_not_owned_by_pbs_or_pve(client, platform_admin_headers, db_session):
    """The critical security case: a TOTP secret's id must never be
    rotatable through this generic endpoint, even though it lives in the
    same `secret_references` table as real infra credentials.
    """
    totp_ref_id = _create_totp_secret_reference(db_session)

    response = client.post(
        f"/api/v1/credentials/{totp_ref_id}/rotate",
        json={"new_value": "attacker-controlled-value"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404


def test_list_credentials_requires_platform_admin(client, db_session, test_settings):
    viewer_headers = _create_viewer_headers(client, db_session, test_settings)
    response = client.get("/api/v1/credentials", headers=viewer_headers)
    assert response.status_code == 403


def test_rotate_credential_requires_platform_admin(client, db_session, test_settings):
    viewer_headers = _create_viewer_headers(client, db_session, test_settings)
    response = client.post(
        "/api/v1/credentials/00000000-0000-0000-0000-000000000000/rotate",
        json={"new_value": "x"},
        headers=viewer_headers,
    )
    assert response.status_code == 403
