import json


def test_status_reports_needs_setup_on_a_fresh_database(client):
    response = client.get("/api/v1/setup/status")
    assert response.status_code == 200
    body = response.json()
    assert body["needs_setup"] is True
    assert "kvm_detail" in body
    assert isinstance(body["kvm_available"], bool)


def test_status_is_unauthenticated(client):
    # No Authorization header at all -- this must work before any login exists.
    response = client.get("/api/v1/setup/status")
    assert response.status_code == 200


def test_complete_creates_the_first_admin_and_logs_them_in(client):
    response = client.post(
        "/api/v1/setup/complete",
        json={"email": "admin@example.com", "display_name": "First Admin", "password": "a-real-password"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "admin@example.com"
    assert body["has_password"] is True
    assert body["is_breakglass"] is False

    # Setup is now complete.
    status = client.get("/api/v1/setup/status")
    assert status.json()["needs_setup"] is False

    # The new account can actually log in and reaches a platform-admin-gated endpoint.
    login = client.post("/api/v1/auth/local-login", json={"email": "admin@example.com", "password": "a-real-password"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    pbs_instances = client.get("/api/v1/pbs-instances", headers={"Authorization": f"Bearer {token}"})
    assert pbs_instances.status_code == 200


def test_complete_rejects_a_short_password(client):
    response = client.post(
        "/api/v1/setup/complete",
        json={"email": "admin@example.com", "display_name": "First Admin", "password": "short"},
    )
    assert response.status_code == 422


def test_complete_refuses_once_setup_already_happened(client, platform_admin_headers):
    # platform_admin_headers already grants a real global platform_admin
    # assignment -- setup is already "done" before this request.
    response = client.post(
        "/api/v1/setup/complete",
        json={"email": "second-admin@example.com", "display_name": "Second Admin", "password": "a-real-password"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "setup_already_complete"


def test_status_reports_kvm_from_the_host_probe_state_file(client, test_settings, tmp_path):
    # The appliance's API container never sees /dev/kvm itself -- it must
    # report what resurgam-host-probe.service found natively instead.
    state_file = tmp_path / "host-capabilities.json"
    state_file.write_text(json.dumps({"kvm": {"available": True, "detail": "probed natively"}, "checked_at": "x"}))
    test_settings.host_capabilities_file = str(state_file)

    body = client.get("/api/v1/setup/status").json()

    assert body["kvm_available"] is True
    assert body["kvm_detail"] == "probed natively"


def test_status_reports_a_missing_host_probe_state_file(client, test_settings, tmp_path):
    test_settings.host_capabilities_file = str(tmp_path / "missing.json")

    body = client.get("/api/v1/setup/status").json()

    assert body["kvm_available"] is False
    assert "resurgam-host-probe" in body["kvm_detail"]
