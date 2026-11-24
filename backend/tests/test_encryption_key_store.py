"""ADR 0031 stage 2: the encryption key store -- keys by fingerprint, the right
key per backup, download/key sheet/export only with a fresh confirmation, tenant
admins only when allowed, and the key-store alarms."""

import json
import time
import uuid

import jwt
import pytest
from starlette.requests import Request

from resurgam_api import encryption_keys
from resurgam_api.auth import attempt_limit
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.step_up import require_step_up
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.encryption_key import EncryptionKey
from resurgam_api.models.identity import User
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from tests.conftest import TEST_BREAKGLASS_PASSWORD
from tests.test_api_tokens import _customer, _session, _user
from tests.test_backup_encryption_visibility import _datastore, _group_with_snapshots

FP_A = ":".join(["aa"] * 32)
FP_B = ":".join(["bb"] * 32)
BREAKGLASS = {"confirmation": {"password": TEST_BREAKGLASS_PASSWORD}}


def _keyfile(fingerprint: str) -> str:
    return json.dumps(
        {
            "kdf": None,
            "created": "2026-11-22T10:00:00+01:00",
            "modified": "2026-11-22T10:00:00+01:00",
            "data": "c2VjcmV0LWtleS1tYXRlcmlhbA==",
            "fingerprint": fingerprint,
        }
    )


def _upload(client, headers, fingerprint=FP_A, **extra):
    response = client.post(
        "/api/v1/encryption-keys", json={"keyfile_content": _keyfile(fingerprint), **extra}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- parsing ------------------------------------------------------------------


def test_parse_keyfile_reads_fingerprint_and_refuses_unusable_files():
    parsed = encryption_keys.parse_keyfile(_keyfile(FP_A.upper()))
    assert (parsed.fingerprint, parsed.kdf) == (FP_A, "none")
    assert parsed.created_at is not None

    for content, code in [
        ("not json", "invalid_keyfile"),
        ('{"fingerprint": "x"}', "invalid_keyfile"),
        ('{"data": "x"}', "keyfile_without_fingerprint"),
        (_keyfile(FP_A).replace('"kdf": null', '"kdf": {"Scrypt": {"n": 65536}}'), "password_protected_key"),
    ]:
        with pytest.raises(encryption_keys.KeyfileError) as exc:
            encryption_keys.parse_keyfile(content)
        assert exc.value.code == code


# --- the right key per backup ------------------------------------------------------


def test_each_backup_gets_exactly_its_own_key_and_unencrypted_ones_none(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    datastore_id = _datastore(client, platform_admin_headers, monkeypatch)
    _group_with_snapshots(db_session, datastore_id, "100", [("none", None), ("encrypt", FP_A), ("encrypt", FP_B)])
    _upload(client, platform_admin_headers, FP_A)
    snapshots = {s.key_fingerprint: s for s in db_session.query(encryption_keys.BackupSnapshot).all()}

    assert encryption_keys.keyfile_for_snapshot(db_session, test_settings, snapshots[None]) is None
    assert json.loads(encryption_keys.keyfile_for_snapshot(db_session, test_settings, snapshots[FP_A]))[
        "fingerprint"
    ] == (FP_A)
    with pytest.raises(encryption_keys.EncryptionKeyMissingError) as exc:
        encryption_keys.keyfile_for_snapshot(db_session, test_settings, snapshots[FP_B])
    assert exc.value.fingerprint == FP_B
    assert "Administration -> Encryption keys" in str(exc.value)


# --- upload / list / update / delete ----------------------------------------------


def test_upload_list_and_duplicate(client, platform_admin_headers, db_session):
    body = _upload(client, platform_admin_headers, FP_A, label="pve storage pbs-main")
    assert body["fingerprint"] == FP_A
    assert body["copy_confirmed_at"] is None
    assert body["can_download"] is True
    assert "keyfile" not in json.dumps(body).replace("keyfile_", "")

    dup = client.post(
        "/api/v1/encryption-keys", json={"keyfile_content": _keyfile(FP_A)}, headers=platform_admin_headers
    )
    assert (dup.status_code, dup.json()["code"]) == (409, "key_exists")

    listed = client.get("/api/v1/encryption-keys", headers=platform_admin_headers).json()
    assert [k["label"] for k in listed] == ["pve storage pbs-main"]
    assert db_session.query(AuditEvent).filter_by(action="encryption_key_uploaded").count() == 1


def test_delete_refuses_a_key_in_use_unless_forced(client, platform_admin_headers, db_session, monkeypatch):
    datastore_id = _datastore(client, platform_admin_headers, monkeypatch)
    _group_with_snapshots(db_session, datastore_id, "100", [("encrypt", FP_A)])
    key = _upload(client, platform_admin_headers, FP_A)
    assert key["backup_count"] == 1

    refused = client.delete(f"/api/v1/encryption-keys/{key['id']}", headers=platform_admin_headers)
    assert (refused.status_code, refused.json()["code"]) == (409, "key_in_use")

    forced = client.delete(f"/api/v1/encryption-keys/{key['id']}?force=true", headers=platform_admin_headers)
    assert forced.status_code == 204
    assert db_session.query(EncryptionKey).count() == 0
    assert db_session.query(SecretReference).filter_by(purpose=SecretPurpose.PBS_ENCRYPTION_KEY).count() == 0


def test_only_platform_admins_manage_keys(client, platform_admin_headers):
    tenant = _customer(client, platform_admin_headers, "Tenant A")
    tenant_admin = _user(client, platform_admin_headers, "ta@example.com", [("customer_admin", "customer", tenant)])
    key = _upload(client, platform_admin_headers, FP_A, customer_id=tenant)

    upload = client.post(
        "/api/v1/encryption-keys", json={"keyfile_content": _keyfile(FP_B)}, headers=_session(tenant_admin)
    )
    assert upload.status_code == 403
    patch = client.patch(f"/api/v1/encryption-keys/{key['id']}", json={"label": "x"}, headers=_session(tenant_admin))
    assert patch.status_code == 403
    export = client.post(
        "/api/v1/encryption-keys/export",
        json={"passphrase": "long enough passphrase", "confirmation": {"password": "correct horse battery staple"}},
        headers=_session(tenant_admin),
    )
    assert export.status_code == 403


# --- download with fresh confirmation ---------------------------------------------------


def test_download_needs_confirmation_and_is_audited(client, platform_admin_headers, db_session):
    key = _upload(client, platform_admin_headers, FP_A)
    url = f"/api/v1/encryption-keys/{key['id']}/download"

    missing = client.post(url, json={}, headers=platform_admin_headers)
    assert (missing.status_code, missing.json()["code"]) == (403, "confirmation_required")
    wrong = client.post(url, json={"confirmation": {"password": "nope"}}, headers=platform_admin_headers)
    assert (wrong.status_code, wrong.json()["code"]) == (403, "confirmation_failed")

    ok = client.post(url, json=BREAKGLASS, headers=platform_admin_headers)
    assert ok.status_code == 200
    assert json.loads(ok.content)["fingerprint"] == FP_A
    assert "attachment" in ok.headers["content-disposition"]
    event = db_session.query(AuditEvent).filter_by(action="encryption_key_downloaded").one()
    assert event.detail["confirmed_by"] == "password"


def test_local_user_confirms_with_own_password(client, platform_admin_headers):
    admin = _user(client, platform_admin_headers, "pa@example.com", [("platform_admin", "global", None)])
    key = _upload(client, platform_admin_headers, FP_A)
    url = f"/api/v1/encryption-keys/{key['id']}/download"
    assert client.post(url, json=BREAKGLASS, headers=_session(admin)).status_code == 403
    ok = client.post(url, json={"confirmation": {"password": "correct horse battery staple"}}, headers=_session(admin))
    assert ok.status_code == 200


def test_wrong_confirmations_are_audited_and_lock_after_five(client, platform_admin_headers, db_session, monkeypatch):
    admin = _user(client, platform_admin_headers, "pa@example.com", [("platform_admin", "global", None)])
    other = _user(client, platform_admin_headers, "pb@example.com", [("platform_admin", "global", None)])
    key = _upload(client, platform_admin_headers, FP_A)
    url = f"/api/v1/encryption-keys/{key['id']}/download"
    wrong = {"confirmation": {"password": "not my password"}}
    right = {"confirmation": {"password": "correct horse battery staple"}}

    for left in (4, 3, 2, 1):
        response = client.post(url, json=wrong, headers=_session(admin))
        assert (response.status_code, response.json()["code"]) == (403, "confirmation_failed")
        assert response.json()["details"]["attempts_left"] == left
    fifth = client.post(url, json=wrong, headers=_session(admin))
    assert (fifth.status_code, fifth.json()["code"]) == (429, "confirmation_locked")
    assert 0 < fifth.json()["details"]["retry_after_seconds"] <= 15 * 60

    # Locked means locked: the right password does not get through either,
    # and the guesses never reach the key.
    locked = client.post(url, json=right, headers=_session(admin))
    assert (locked.status_code, locked.json()["code"]) == (429, "confirmation_locked")
    failures = db_session.query(AuditEvent).filter_by(action="step_up_confirmation_failed").all()
    assert len(failures) == 5
    assert {f.detail["path"] for f in failures} == {url}
    assert db_session.query(AuditEvent).filter_by(action="encryption_key_downloaded").count() == 0

    # Another account is not affected.
    assert client.post(url, json=right, headers=_session(other)).status_code == 200

    # Once the window has passed, the account can confirm again (the audit
    # log is append-only, so shrink the window instead of ageing the rows).
    monkeypatch.setattr(attempt_limit, "WINDOW_SECONDS", 0)
    assert client.post(url, json=right, headers=_session(admin)).status_code == 200


def test_key_sheet_is_a_pdf(client, platform_admin_headers, db_session):
    key = _upload(client, platform_admin_headers, FP_A, label="pbs-main")
    response = client.post(
        f"/api/v1/encryption-keys/{key['id']}/key-sheet", json=BREAKGLASS, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert db_session.query(AuditEvent).filter_by(action="encryption_key_sheet_printed").count() == 1


def test_tenant_admin_downloads_own_keys_only_when_allowed(client, platform_admin_headers):
    tenant_a = _customer(client, platform_admin_headers, "Tenant A")
    tenant_b = _customer(client, platform_admin_headers, "Tenant B")
    admin_a = _user(client, platform_admin_headers, "ta@example.com", [("customer_admin", "customer", tenant_a)])
    viewer_a = _user(client, platform_admin_headers, "va@example.com", [("viewer", "customer", tenant_a)])
    key_a = _upload(client, platform_admin_headers, FP_A, customer_id=tenant_a)
    key_b = _upload(client, platform_admin_headers, FP_B, customer_id=tenant_b)
    confirm = {"confirmation": {"password": "correct horse battery staple"}}

    listed = client.get("/api/v1/encryption-keys", headers=_session(admin_a)).json()
    assert [(k["id"], k["can_download"]) for k in listed] == [(key_a["id"], False)]
    assert client.get("/api/v1/encryption-keys", headers=_session(viewer_a)).json() == []

    url_a = f"/api/v1/encryption-keys/{key_a['id']}/download"
    refused = client.post(url_a, json=confirm, headers=_session(admin_a))
    assert (refused.status_code, refused.json()["code"]) == (403, "key_download_not_allowed")

    # Only a platform admin may flip the switch.
    perm_url = f"/api/v1/encryption-keys/tenants/{tenant_a}/download-permission"
    assert client.put(perm_url, json={"allowed": True}, headers=_session(admin_a)).status_code == 403
    assert client.put(perm_url, json={"allowed": True}, headers=platform_admin_headers).json()["allowed"] is True
    assert client.get(perm_url, headers=_session(admin_a)).json()["allowed"] is True

    assert client.post(url_a, json=confirm, headers=_session(admin_a)).status_code == 200
    other = client.post(f"/api/v1/encryption-keys/{key_b['id']}/download", json=confirm, headers=_session(admin_a))
    assert other.status_code == 404


def test_api_tokens_can_never_download(client, platform_admin_headers):
    admin = _user(client, platform_admin_headers, "pa@example.com", [("platform_admin", "global", None)])
    key = _upload(client, platform_admin_headers, FP_A)
    token = client.post(
        "/api/v1/api-tokens",
        json={
            "name": "script",
            "grants": [{"role": "platform_admin", "scope_type": "global", "scope_id": None}],
            "read_only": False,
            "expires_in_days": 30,
        },
        headers=_session(admin),
    )
    assert token.status_code in (200, 201), token.text
    bearer = {"Authorization": f"Bearer {token.json()['token']}"}
    response = client.post(
        f"/api/v1/encryption-keys/{key['id']}/download",
        json={"confirmation": {"password": "correct horse battery staple"}},
        headers=bearer,
    )
    assert response.status_code in (401, 403)
    assert response.json()["code"] != "confirmation_failed"


# --- export / import --------------------------------------------------------------------


def test_export_import_round_trip(client, platform_admin_headers, db_session):
    _upload(client, platform_admin_headers, FP_A, label="first")
    _upload(client, platform_admin_headers, FP_B, label="second")

    short = client.post(
        "/api/v1/encryption-keys/export", json={"passphrase": "short", **BREAKGLASS}, headers=platform_admin_headers
    )
    assert short.status_code == 422
    export = client.post(
        "/api/v1/encryption-keys/export",
        json={"passphrase": "a long export passphrase", **BREAKGLASS},
        headers=platform_admin_headers,
    )
    assert export.status_code == 200
    document = export.content.decode()
    assert "c2VjcmV0" not in document  # the key material is not readable in the file
    assert json.loads(document)["count"] == 2

    for key in client.get("/api/v1/encryption-keys", headers=platform_admin_headers).json():
        client.delete(f"/api/v1/encryption-keys/{key['id']}", headers=platform_admin_headers)

    wrong = client.post(
        "/api/v1/encryption-keys/import",
        json={"export_content": document, "passphrase": "wrong passphrase!"},
        headers=platform_admin_headers,
    )
    assert (wrong.status_code, wrong.json()["code"]) == (400, "wrong_passphrase")

    imported = client.post(
        "/api/v1/encryption-keys/import",
        json={"export_content": document, "passphrase": "a long export passphrase"},
        headers=platform_admin_headers,
    ).json()
    assert sorted(imported["imported"]) == [FP_A, FP_B] and imported["skipped"] == []
    again = client.post(
        "/api/v1/encryption-keys/import",
        json={"export_content": document, "passphrase": "a long export passphrase"},
        headers=platform_admin_headers,
    ).json()
    assert again["imported"] == [] and sorted(again["skipped"]) == [FP_A, FP_B]
    labels = {k.fingerprint: (k.label, k.source) for k in db_session.query(EncryptionKey).all()}
    assert labels[FP_A] == ("first", "import")


# --- alarms ----------------------------------------------------------------------------------


def test_missing_key_and_unconfirmed_copy_alarms(client, platform_admin_headers, db_session, monkeypatch):
    datastore_id = _datastore(client, platform_admin_headers, monkeypatch)
    _group_with_snapshots(db_session, datastore_id, "100", [("encrypt", FP_A)])
    _group_with_snapshots(db_session, datastore_id, "101", [("encrypt", FP_A)])
    _group_with_snapshots(db_session, datastore_id, "102", [("none", None)])

    def alarms(kind):
        return [a for a in client.get("/api/v1/alarms", headers=platform_admin_headers).json() if a["type"] == kind]

    missing = alarms("encryption_key_missing")
    assert len(missing) == 1  # one per datastore and key, not per group
    assert missing[0]["entity_id"] == str(datastore_id) and "2 backup group(s)" in missing[0]["message"]
    assert client.get("/api/v1/encryption-keys/missing", headers=platform_admin_headers).json() == [FP_A]
    datastore = client.get(f"/api/v1/datastores/{datastore_id}", headers=platform_admin_headers).json()
    assert datastore["keys_missing"] == [FP_A]

    key = _upload(client, platform_admin_headers, FP_A)
    assert alarms("encryption_key_missing") == []
    assert [a["entity_id"] for a in alarms("encryption_key_copy_unconfirmed")] == [key["id"]]

    confirmed = client.post(f"/api/v1/encryption-keys/{key['id']}/confirm-copy", headers=platform_admin_headers)
    assert confirmed.json()["copy_confirmed_at"] is not None
    assert alarms("encryption_key_copy_unconfirmed") == []


# --- adopting ADR 0022 per-datastore keys -------------------------------------------------------


def test_adopt_datastore_keys_moves_them_into_the_store(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    datastore_id = _datastore(client, platform_admin_headers, monkeypatch)
    ref = SecretReference(
        reference_key=f"datastore-encryption-key-{uuid.uuid4()}",
        description="old per-datastore key",
        purpose=SecretPurpose.PBS_ENCRYPTION_KEY,
    )
    db_session.add(ref)
    db_session.flush()
    LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key).store(
        ref.reference_key, _keyfile(FP_A)
    )
    db_session.get(Datastore, datastore_id).encryption_key_secret_ref_id = ref.id
    db_session.commit()

    assert encryption_keys.adopt_datastore_keys(db_session, test_settings) == 1
    assert encryption_keys.adopt_datastore_keys(db_session, test_settings) == 0
    key = db_session.query(EncryptionKey).one()
    assert (key.fingerprint, key.source) == (FP_A, "datastore")
    assert db_session.get(Datastore, datastore_id).encryption_key_secret_ref_id is None
    assert db_session.query(SecretReference).filter_by(purpose=SecretPurpose.PBS_ENCRYPTION_KEY).count() == 1


# --- step-up for SSO-only accounts -----------------------------------------------------------------


def _request_with_session(test_settings, user_id, issued_at):
    token = jwt.encode({"sub": str(user_id), "iat": int(issued_at)}, test_settings.session_secret, algorithm="HS256")
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})


def test_sso_only_account_needs_a_recent_sign_in(db_session, test_settings):
    user = User(email="sso@example.com", display_name="sso", external_subject="sub-1")
    db_session.add(user)
    db_session.flush()
    auth = AuthenticatedUser(user_id=user.id, email=user.email, display_name="sso", is_breakglass=False, assignments=[])

    fresh = _request_with_session(test_settings, user.id, time.time() - 60)
    assert require_step_up(db_session, test_settings, fresh, auth, None) == "recent_login"

    stale = _request_with_session(test_settings, user.id, time.time() - 3600)
    with pytest.raises(ResurgamApiError) as exc:
        require_step_up(db_session, test_settings, stale, auth, None)
    assert exc.value.code == "confirmation_required"
    assert exc.value.details == {"method": "recent_login"}
