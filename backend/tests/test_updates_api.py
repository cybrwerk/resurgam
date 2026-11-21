"""Appliance updates API (ADR 0024): bundle upload into the shared
directory, verify/apply request files, and reading the host updater's
status/history/backups back -- all against a tmp dir as `updates_dir`."""

import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def updates_dirs(tmp_path, test_settings):
    updates = tmp_path / "updates"
    backups = tmp_path / "backups"
    updates.mkdir()
    backups.mkdir()
    test_settings.updates_dir = str(updates)
    test_settings.backups_dir = str(backups)
    # No baked release key in tests unless a test provides one.
    test_settings.release_public_key_file = str(tmp_path / "no-release-key.pub")
    return updates, backups


def _bundle_bytes(*, manifest: dict | None = None, signature: bool = True, extra_size: int = 0) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

        if manifest is not False:
            body = manifest or {
                "format": 1,
                "version": "0.3.0",
                "created_at": "2026-10-07T10:00:00Z",
                "min_upgrade_from": "0.2.0",
                "changelog": "Fixes.",
                "components": {},
            }
            add("manifest.json", json.dumps(body).encode())
        if signature:
            add("manifest.json.sig", b"\x00" * 64)
        if extra_size:
            add("images/backend.tar", b"x" * extra_size)
    return buffer.getvalue()


def _upload(client, headers, data: bytes, filename: str = "resurgam-0.3.0.bundle"):
    return client.post(
        f"/api/v1/updates/bundles?filename={filename}",
        content=data,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )


def _customer_operator_headers(db_session, test_settings) -> dict:
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment
    from resurgam_api.models.tenancy import Customer

    customer = Customer(name="Acme", slug="acme", debitor_number="debitor-acme", match_code="ACM")
    db_session.add(customer)
    db_session.flush()
    user = User(email="operator@example.com", display_name="Operator")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.RESTORE_OPERATOR.value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer.id)
    )
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


# --- version ---------------------------------------------------------------------


def test_version_prefers_env(client, monkeypatch):
    monkeypatch.setenv("RESURGAM_VERSION", "9.8.7")
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {"version": "9.8.7"}


def test_version_falls_back_to_version_file_then_dev(monkeypatch, tmp_path):
    from resurgam_api import version as version_module

    monkeypatch.delenv("RESURGAM_VERSION", raising=False)
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n")
    monkeypatch.setattr(version_module, "_version_file_candidates", lambda: [tmp_path / "missing", version_file])
    assert version_module.running_version() == "1.2.3"

    version_file.write_text("garbage")
    assert version_module.running_version() == "0.0.0-dev"


def test_version_needs_no_auth(client):
    assert client.get("/api/v1/version").status_code == 200


# --- availability and roles --------------------------------------------------------


def test_endpoints_answer_503_when_updates_dir_missing(client, platform_admin_headers, test_settings, tmp_path):
    test_settings.updates_dir = str(tmp_path / "does-not-exist")
    for path in ("/bundles", "/requests", "/history", "/backups"):
        response = client.get(f"/api/v1/updates{path}", headers=platform_admin_headers)
        assert response.status_code == 503, path
        assert response.json()["code"] == "updates_unavailable"
        assert "only available on the RESURGAM appliance" in response.json()["message"]

    overview = client.get("/api/v1/updates", headers=platform_admin_headers)
    assert overview.status_code == 200
    assert overview.json()["available"] is False
    assert overview.json()["unavailable_reason"]


def test_endpoints_require_platform_admin(client, db_session, test_settings, updates_dirs):
    assert client.get("/api/v1/updates/bundles").status_code == 401
    assert client.get("/api/v1/updates").status_code == 401
    operator = _customer_operator_headers(db_session, test_settings)
    assert client.get("/api/v1/updates/history", headers=operator).status_code == 403
    assert _upload(client, operator, _bundle_bytes()).status_code == 403
    response = client.post(
        "/api/v1/updates/requests", json={"action": "verify", "bundle": "x.bundle"}, headers=operator
    )
    assert response.status_code == 403
    updates, _ = updates_dirs
    # Nothing was written by the refused calls.
    assert not (updates / "requests").exists() or not list((updates / "requests").iterdir())
    assert not (updates / "incoming").exists() or not list((updates / "incoming").iterdir())


def test_unauthenticated_gets_401_even_off_appliance(client, test_settings, tmp_path):
    test_settings.updates_dir = str(tmp_path / "nope")
    assert client.get("/api/v1/updates/history").status_code == 401


# --- upload ---------------------------------------------------------------------------


def test_upload_streams_bundle_into_incoming_and_audits(client, platform_admin_headers, updates_dirs, db_session):
    from resurgam_api.models.audit import AuditEvent

    updates, _ = updates_dirs
    data = _bundle_bytes(extra_size=200_000)
    response = _upload(client, platform_admin_headers, data)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == "0.3.0"
    assert body["size_bytes"] == len(data)
    stored = updates / "incoming" / body["name"]
    assert stored.read_bytes() == data
    # No leftover partial files.
    assert [p.name for p in (updates / "incoming").iterdir()] == [body["name"]]

    event = db_session.query(AuditEvent).filter_by(action="update_bundle_uploaded").one()
    assert event.target_id == body["name"]
    assert event.detail["original_filename"] == "resurgam-0.3.0.bundle"

    listed = client.get("/api/v1/updates/bundles", headers=platform_admin_headers).json()
    assert [b["name"] for b in listed] == [body["name"]]
    assert listed[0]["version"] == "0.3.0"


def test_upload_rejects_non_tar_early(client, platform_admin_headers, updates_dirs):
    updates, _ = updates_dirs
    response = _upload(client, platform_admin_headers, b"PK\x03\x04" + b"z" * 5000, filename="evil.zip")
    assert response.status_code == 400
    assert response.json()["code"] == "not_a_bundle"
    assert list((updates / "incoming").iterdir()) == []


def test_upload_rejects_tiny_body(client, platform_admin_headers, updates_dirs):
    response = _upload(client, platform_admin_headers, b"hello")
    assert response.status_code == 400
    assert response.json()["code"] == "not_a_bundle"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [({"signature": False}, "manifest.json.sig"), ({"manifest": False}, "no manifest.json")],
)
def test_upload_rejects_tar_without_manifest_or_signature(
    client, platform_admin_headers, updates_dirs, kwargs, fragment
):
    updates, _ = updates_dirs
    data = _bundle_bytes(extra_size=10, **kwargs)
    response = _upload(client, platform_admin_headers, data)
    assert response.status_code == 400
    assert fragment in response.json()["message"]
    assert list((updates / "incoming").iterdir()) == []


def test_upload_enforces_size_limit(client, platform_admin_headers, updates_dirs, test_settings):
    updates, _ = updates_dirs
    test_settings.updates_max_bundle_bytes = 4096
    response = _upload(client, platform_admin_headers, _bundle_bytes(extra_size=20_000))
    assert response.status_code == 413
    assert response.json()["code"] == "bundle_too_large"
    assert list((updates / "incoming").iterdir()) == []


def test_upload_enforces_size_limit_while_streaming_without_content_length(
    client, platform_admin_headers, updates_dirs, test_settings
):
    updates, _ = updates_dirs
    test_settings.updates_max_bundle_bytes = 4096
    data = _bundle_bytes(extra_size=20_000)

    def chunks():
        for start in range(0, len(data), 1024):
            yield data[start : start + 1024]

    response = client.post(
        "/api/v1/updates/bundles",
        content=chunks(),
        headers={**platform_admin_headers, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
    assert list((updates / "incoming").iterdir()) == []


def test_delete_bundle(client, platform_admin_headers, updates_dirs, db_session):
    from resurgam_api.models.audit import AuditEvent

    updates, _ = updates_dirs
    name = _upload(client, platform_admin_headers, _bundle_bytes()).json()["name"]
    response = client.delete(f"/api/v1/updates/bundles/{name}", headers=platform_admin_headers)
    assert response.status_code == 204
    assert not (updates / "incoming" / name).exists()
    assert db_session.query(AuditEvent).filter_by(action="update_bundle_deleted").count() == 1
    assert client.delete(f"/api/v1/updates/bundles/{name}", headers=platform_admin_headers).status_code == 404
    assert client.delete("/api/v1/updates/bundles/..%2Fhistory.json", headers=platform_admin_headers).status_code in (
        400,
        404,
    )


def test_delete_bundle_refused_while_request_active(client, platform_admin_headers, updates_dirs):
    name = _upload(client, platform_admin_headers, _bundle_bytes()).json()["name"]
    client.post("/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers)
    response = client.delete(f"/api/v1/updates/bundles/{name}", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "bundle_in_use"


# --- requests and status -----------------------------------------------------------------


def test_create_verify_request_writes_request_file(client, platform_admin_headers, updates_dirs, db_session):
    from resurgam_api.models.audit import AuditEvent

    updates, _ = updates_dirs
    name = _upload(client, platform_admin_headers, _bundle_bytes()).json()["name"]
    response = client.post(
        "/api/v1/updates/requests", json={"action": "verify", "bundle": name}, headers=platform_admin_headers
    )
    assert response.status_code == 202, response.text
    status = response.json()
    assert status["state"] == "queued"
    request_file = updates / "requests" / f"{status['request_id']}.json"
    written = json.loads(request_file.read_text())
    assert set(written) == {"action", "bundle", "requested_by", "requested_at"}
    assert written["action"] == "verify"
    assert written["bundle"] == name
    # Atomic write leaves no temp files behind.
    assert [p.name for p in (updates / "requests").iterdir()] == [request_file.name]
    assert db_session.query(AuditEvent).filter_by(action="update_verify_requested").count() == 1


def test_create_request_for_unknown_bundle_404s(client, platform_admin_headers, updates_dirs):
    response = client.post(
        "/api/v1/updates/requests",
        json={"action": "verify", "bundle": "missing.bundle"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404
    response = client.post(
        "/api/v1/updates/requests",
        json={"action": "verify", "bundle": "../etc/passwd.bundle"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400


def test_apply_refused_while_another_apply_queued_or_running(client, platform_admin_headers, updates_dirs, db_session):
    from resurgam_api.models.audit import AuditEvent

    updates, _ = updates_dirs
    name = _upload(client, platform_admin_headers, _bundle_bytes()).json()["name"]
    first = client.post(
        "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
    )
    assert first.status_code == 202
    first_id = first.json()["request_id"]
    assert db_session.query(AuditEvent).filter_by(action="update_apply_requested").count() == 1

    second = client.post(
        "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "update_already_in_progress"

    # A verify is still allowed alongside.
    verify = client.post(
        "/api/v1/updates/requests", json={"action": "verify", "bundle": name}, headers=platform_admin_headers
    )
    assert verify.status_code == 202

    # Running (the updater removed the request file and writes status) still blocks.
    (updates / "requests" / f"{first_id}.json").unlink()
    (updates / "status").mkdir()
    (updates / "status" / f"{first_id}.json").write_text(
        json.dumps({"request_id": first_id, "action": "apply", "state": "running", "step": "load_images", "log": []})
    )
    overview = client.get("/api/v1/updates", headers=platform_admin_headers).json()
    assert overview["active_apply"]["request_id"] == first_id
    assert (
        client.post(
            "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
        ).status_code
        == 409
    )

    # Finished: a new apply is accepted again.
    (updates / "status" / f"{first_id}.json").write_text(
        json.dumps({"request_id": first_id, "action": "apply", "state": "rolled_back", "step": "rollback", "log": []})
    )
    assert (
        client.post(
            "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
        ).status_code
        == 202
    )


def test_stale_unpicked_apply_does_not_block_forever(client, platform_admin_headers, updates_dirs):
    updates, _ = updates_dirs
    name = _upload(client, platform_admin_headers, _bundle_bytes()).json()["name"]
    first_id = client.post(
        "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
    ).json()["request_id"]
    old = time.time() - 2 * 3600
    os.utime(updates / "requests" / f"{first_id}.json", (old, old))
    response = client.post(
        "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
    )
    assert response.status_code == 202


def test_status_reading(client, platform_admin_headers, updates_dirs):
    updates, _ = updates_dirs
    name = _upload(client, platform_admin_headers, _bundle_bytes()).json()["name"]
    request_id = client.post(
        "/api/v1/updates/requests", json={"action": "verify", "bundle": name}, headers=platform_admin_headers
    ).json()["request_id"]

    queued = client.get(f"/api/v1/updates/requests/{request_id}", headers=platform_admin_headers).json()
    assert queued["state"] == "queued"
    assert queued["bundle"] == name

    (updates / "status").mkdir()
    (updates / "status" / f"{request_id}.json").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "action": "verify",
                "state": "failed",
                "step": "verify",
                "from_version": "0.2.0",
                "to_version": "0.3.0",
                "message": "signature does not match the release key",
                "log": ["checking signature", "signature invalid"],
                "manifest": {"version": "0.3.0", "changelog": "Fixes.", "min_upgrade_from": "0.2.0"},
                "backup_file": None,
                "started_at": "2026-10-07T10:00:00Z",
                "finished_at": "2026-10-07T10:00:02Z",
            }
        )
    )
    status = client.get(f"/api/v1/updates/requests/{request_id}", headers=platform_admin_headers).json()
    assert status["state"] == "failed"
    assert status["message"] == "signature does not match the release key"
    assert status["manifest"]["changelog"] == "Fixes."
    assert status["log"] == ["checking signature", "signature invalid"]
    assert status["requested_by"]

    listed = client.get("/api/v1/updates/requests", headers=platform_admin_headers).json()
    assert [s["request_id"] for s in listed] == [request_id]

    assert client.get("/api/v1/updates/requests/" + "0" * 32, headers=platform_admin_headers).status_code == 404
    assert client.get("/api/v1/updates/requests/bad.id", headers=platform_admin_headers).status_code == 400


def test_history_and_backups(client, platform_admin_headers, updates_dirs):
    updates, backups = updates_dirs
    assert client.get("/api/v1/updates/history", headers=platform_admin_headers).json() == []
    assert client.get("/api/v1/updates/backups", headers=platform_admin_headers).json() == []

    (updates / "history.json").write_text(
        json.dumps(
            [
                {
                    "request_id": "a" * 32,
                    "state": "rolled_back",
                    "from_version": "0.2.0",
                    "to_version": "0.3.0",
                    "message": "health check failed",
                    "custom_field": 1,
                },
                {"request_id": "b" * 32, "state": "succeeded", "from_version": "0.1.0", "to_version": "0.2.0"},
            ]
        )
    )
    history = client.get("/api/v1/updates/history", headers=platform_admin_headers).json()
    assert [h["state"] for h in history] == ["rolled_back", "succeeded"]
    assert history[0]["message"] == "health check failed"

    older = backups / "pre-0.2.0-20261001T000000.dump"
    newer = backups / "pre-0.3.0-20261007T000000.dump"
    older.write_bytes(b"x" * 10)
    newer.write_bytes(b"y" * 20)
    (backups / "notes.txt").write_text("ignored")
    os.utime(older, (time.time() - 1000, time.time() - 1000))
    listed = client.get("/api/v1/updates/backups", headers=platform_admin_headers).json()
    assert [(b["name"], b["size_bytes"]) for b in listed] == [(newer.name, 20), (older.name, 10)]


def test_broken_history_file_reads_as_empty(client, platform_admin_headers, updates_dirs):
    updates, _ = updates_dirs
    (updates / "history.json").write_text("{not json")
    assert client.get("/api/v1/updates/history", headers=platform_admin_headers).json() == []


def test_overview_on_appliance(client, platform_admin_headers, updates_dirs, monkeypatch):
    monkeypatch.setenv("RESURGAM_VERSION", "0.2.0")
    body = client.get("/api/v1/updates", headers=platform_admin_headers).json()
    assert body["available"] is True
    assert body["current_version"] == "0.2.0"
    assert body["active_apply"] is None
    assert body["max_bundle_bytes"] == 4 * 1024**3


def test_operator_copied_bundle_is_listed(client, platform_admin_headers, updates_dirs):
    updates, _ = updates_dirs
    (updates / "incoming").mkdir()
    Path(updates / "incoming" / "resurgam-0.3.0.bundle").write_bytes(_bundle_bytes())
    listed = client.get("/api/v1/updates/bundles", headers=platform_admin_headers).json()
    assert listed[0]["name"] == "resurgam-0.3.0.bundle"
    assert listed[0]["problem"] is None


# --- signature pre-check with the baked release key ------------------------------------


def _signed_bundle(private_pem: bytes) -> bytes:
    from resurgam_api.updates import bundle

    payloads = {name: f"{name} content".encode() for name in bundle.REQUIRED_COMPONENTS}
    payloads["wheels/resurgam_worker-0.3.0-py3-none-any.whl"] = b"wheel"
    components = {
        name: bundle.Component(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        for name, data in payloads.items()
    }
    manifest = bundle.build_manifest_bytes(
        version="0.3.0", min_upgrade_from="0.2.0", changelog="Signed release.", components=components
    )
    signature = bundle.sign_manifest(manifest, bundle.load_private_key(private_pem))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        members = [("manifest.json", manifest), ("manifest.json.sig", signature), *payloads.items()]
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_upload_signature_precheck_accepts_release_key(
    client, platform_admin_headers, updates_dirs, test_settings, tmp_path
):
    from resurgam_api.updates import bundle

    private_pem, public_pem = bundle.generate_key_pair()
    key_file = tmp_path / "release-signing.pub"
    key_file.write_bytes(public_pem)
    test_settings.release_public_key_file = str(key_file)

    response = _upload(client, platform_admin_headers, _signed_bundle(private_pem))
    assert response.status_code == 201, response.text
    assert response.json()["signature_prechecked"] is True
    assert response.json()["version"] == "0.3.0"


def test_upload_signature_precheck_rejects_other_key(
    client, platform_admin_headers, updates_dirs, test_settings, tmp_path
):
    from resurgam_api.updates import bundle

    updates, _ = updates_dirs
    _, release_public = bundle.generate_key_pair()
    other_private, _ = bundle.generate_key_pair()
    key_file = tmp_path / "release-signing.pub"
    key_file.write_bytes(release_public)
    test_settings.release_public_key_file = str(key_file)

    response = _upload(client, platform_admin_headers, _signed_bundle(other_private))
    assert response.status_code == 400
    assert response.json()["code"] == "bundle_signature_invalid"
    assert list((updates / "incoming").iterdir()) == []


def test_upload_without_baked_key_is_not_prechecked(client, platform_admin_headers, updates_dirs):
    response = _upload(client, platform_admin_headers, _bundle_bytes())
    assert response.status_code == 201
    assert response.json()["signature_prechecked"] is False


# --- host updater conventions (bundle-contract) --------------------------------------------


def test_claimed_request_keeps_its_details_and_still_blocks_apply(client, platform_admin_headers, updates_dirs):
    updates, _ = updates_dirs
    name = _upload(client, platform_admin_headers, _bundle_bytes()).json()["name"]
    request_id = client.post(
        "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
    ).json()["request_id"]
    # The updater claims the request by renaming it, then writes status.
    (updates / "requests" / f"{request_id}.json").rename(updates / "requests" / f"{request_id}.claimed")
    (updates / "status").mkdir()
    (updates / "status" / f"{request_id}.json").write_text(
        json.dumps({"request_id": request_id, "action": "apply", "state": "running", "step": "backup", "log": None})
    )
    status = client.get(f"/api/v1/updates/requests/{request_id}", headers=platform_admin_headers).json()
    assert status["bundle"] == name
    assert status["step"] == "backup"
    assert status["log"] == []
    assert (
        client.post(
            "/api/v1/updates/requests", json={"action": "apply", "bundle": name}, headers=platform_admin_headers
        ).status_code
        == 409
    )


def test_cli_started_run_is_listed(client, platform_admin_headers, updates_dirs):
    updates, _ = updates_dirs
    (updates / "status").mkdir()
    (updates / "status" / "cli-20261007T100000Z.json").write_text(
        json.dumps({"action": "apply", "state": "succeeded", "step": "done", "started_at": "2026-10-07T10:00:00Z"})
    )
    listed = client.get("/api/v1/updates/requests", headers=platform_admin_headers).json()
    assert listed[0]["request_id"] == "cli-20261007T100000Z"
    single = client.get("/api/v1/updates/requests/cli-20261007T100000Z", headers=platform_admin_headers)
    assert single.status_code == 200
    assert single.json()["state"] == "succeeded"
