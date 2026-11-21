"""Guided lab provisioning: shipped package downloads, the one-time SSH flow
(host-key confirmation, agent install, template build) and the manual
template script. SSH is a fake (`tests/lab_ssh_fakes.py`), PVE is mocked
HTTP -- nothing real is contacted."""

import hashlib
import json
import logging
import re
import shlex
from pathlib import Path

import pytest

from resurgam_api.api.v1.pve_lab_provisioning import get_job_session_runner
from resurgam_api.lab_setup import jobs as jobs_module
from resurgam_api.lab_setup import template_build as template_module
from resurgam_api.lab_setup.template_build import PROBER_VERSION_MARKER, parse_prober_version
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.pve import LabUplinkIpAllocation, PveCluster
from resurgam_api.restore_test.execution import hash_appliance_run_token, verify_appliance_run_token
from tests.lab_ssh_fakes import FINGERPRINT, OTHER_FINGERPRINT, FakeSshBackend, fail, guest_exec, ok
from tests.proxmox_fakes import FakeProxmox, FakeResponse

BASE = "/api/v1/pve-clusters"
SECRET = "d63e505a-e3ec-449a-9bc7-1da610d4ccde"
PASSWORD = "Correct-Horse-Battery-Staple-42"
PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "QyNTUxOQAAACBhc2RmYXNkZmFzZGZhc2RmYXNkZmFzZGZhc2RmYXNkZmFzZAAAAJhUNIQUE\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
PBS_TOKEN = "lab@pbs!agent:7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
REPO = Path(__file__).resolve().parents[2]
VERSION = (REPO / "VERSION").read_text().strip()


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def sync_jobs(client, db_session, monkeypatch):
    """Runs guided jobs synchronously and writes through the test session."""
    monkeypatch.setattr(jobs_module.registry, "executor", lambda fn: fn())
    monkeypatch.setattr(jobs_module.registry, "_jobs", {})

    def runner():
        def run(fn):
            fn(db_session)
            db_session.commit()

        return run

    client.app.dependency_overrides[get_job_session_runner] = runner
    yield
    client.app.dependency_overrides.pop(get_job_session_runner, None)


def _create_cluster(client, headers, db_session, **lab) -> str:
    created = client.post(
        BASE,
        json={
            "display_name": "Lab PVE",
            "host": "pve.lab.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "token_id": "resurgam@pve!resurgam",
            "token_secret": SECRET,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    cluster_id = created.json()["id"]
    cluster = db_session.get(PveCluster, cluster_id)
    values = {
        "lab_snippet_storage_id": "local",
        "lab_appliance_uplink_bridge": "vmbr0",
        "lab_agent_token_hash": hash_appliance_run_token("old-agent-token"),
    }
    values.update(lab)
    for key, value in values.items():
        setattr(cluster, key, value)
    db_session.commit()
    return cluster_id


def _ssh(**overrides) -> dict:
    body = {
        "node": "pve1",
        "host": "192.0.2.21",
        "port": 22,
        "username": "root",
        "auth_method": "password",
        "password": PASSWORD,
        "confirmed_fingerprint": FINGERPRINT,
    }
    body.update(overrides)
    return body


def _agent_node(fake: FakeSshBackend, *, uid="0", sudo_ok=True, codename="trixie", env_file="", active="active"):
    """Answers like a healthy PVE 9 node."""
    shipped_sha = {}

    def sha(command):
        path = shlex.split(command)[1]
        data = next(u.data for u in reversed(fake.uploads) if u.remote_path == path)
        shipped_sha[path] = hashlib.sha256(data).hexdigest()
        return ok(f"{shipped_sha[path]}  {path}\n")

    fake.respond(r"^id -u$", ok(f"{uid}\n"))
    fake.respond(r"^sudo -n true$", ok() if sudo_ok else fail("sudo: a password is required"))
    fake.respond(r"command -v qm", ok("/usr/sbin/qm\n/usr/sbin/pvesm\n"))
    fake.respond(r"cat /etc/os-release", ok(f'PRETTY_NAME="Debian"\nVERSION_CODENAME={codename}\n'))
    fake.respond(r"dpkg-query", ok(""))
    fake.respond(r"mktemp -d /tmp/resurgam-lab-agent", ok("/tmp/resurgam-lab-agent.abc123\n"))
    fake.respond(r"^sha256sum ", sha)
    fake.respond(r"cat /etc/default/resurgam-lab-agent", ok(env_file))
    fake.respond(r"systemctl is-active", ok(f"{active}\n"))
    fake.respond(r"journalctl", ok("resurgam-lab-agent: required environment variable RESURGAM_LAB_AGENT_PBS_TOKEN"))
    return fake


def _all_audit_text(db_session) -> str:
    return json.dumps([[e.action, e.detail] for e in db_session.query(AuditEvent).all()], default=str)


def _assert_no_secrets(*texts: str, secrets=(PASSWORD, PBS_TOKEN)) -> None:
    key_lines = [line for line in PRIVATE_KEY.splitlines() if len(line) > 20]
    for text in texts:
        for secret in [*secrets, *key_lines]:
            assert secret not in text, f"secret leaked: {secret[:8]}…"


# --- downloads ---------------------------------------------------------------


def test_downloads_list_file_and_checksum(client, platform_admin_headers, artifacts):
    listing = client.get(f"{BASE}/lab-setup/downloads", headers=platform_admin_headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["debian_release"] == "trixie"
    assert body["release_version"] == VERSION
    agent = next(a for a in body["artifacts"] if a["name"] == "resurgam-lab-agent")
    assert agent["sha256"] == artifacts["resurgam-lab-agent"]["sha256"]
    assert agent["download_path"] == "/pve-clusters/lab-setup/downloads/resurgam-lab-agent"

    deb = client.get(f"/api/v1{agent['download_path']}", headers=platform_admin_headers)
    assert deb.status_code == 200
    assert deb.headers["content-type"] == "application/vnd.debian.binary-package"
    assert hashlib.sha256(deb.content).hexdigest() == agent["sha256"]
    assert artifacts["resurgam-lab-agent"]["filename"] in deb.headers["content-disposition"]

    checksum = client.get(f"/api/v1{agent['sha256_path']}", headers=platform_admin_headers)
    assert checksum.text == f"{agent['sha256']}  {agent['filename']}\n"


def test_downloads_missing_artifacts_are_explained(client, platform_admin_headers, test_settings, tmp_path):
    test_settings.lab_artifacts_dir = str(tmp_path / "nothing-here")
    response = client.get(f"{BASE}/lab-setup/downloads/resurgam-lab-agent", headers=platform_admin_headers)
    assert response.status_code == 404
    assert response.json()["code"] == "lab_artifacts_missing"
    assert "dpkg-buildpackage" in response.json()["message"]


def test_downloads_reject_unknown_names_and_tampered_files(client, platform_admin_headers, artifacts, test_settings):
    assert client.get(f"{BASE}/lab-setup/downloads/etc-passwd", headers=platform_admin_headers).status_code == 422
    deb = Path(test_settings.lab_artifacts_dir) / artifacts["resurgam-lab-prober"]["filename"]
    deb.write_bytes(b"truncated")
    response = client.get(f"{BASE}/lab-setup/downloads/resurgam-lab-prober", headers=platform_admin_headers)
    assert response.status_code == 404
    assert response.json()["code"] == "lab_artifacts_missing"


def test_downloads_require_platform_admin(client, artifacts):
    assert client.get(f"{BASE}/lab-setup/downloads").status_code == 401
    assert client.get(f"{BASE}/lab-setup/downloads/resurgam-lab-agent").status_code == 401


def test_agent_install_info_points_at_the_shipped_package(client, platform_admin_headers, db_session, artifacts):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    body = client.get(f"{BASE}/{cluster_id}/lab-setup/agent-install", headers=platform_admin_headers).json()
    assert body["package_download_path"] == "/pve-clusters/lab-setup/downloads/resurgam-lab-agent"
    assert body["package_sha256"] == artifacts["resurgam-lab-agent"]["sha256"]
    assert body["package_version"] == f"{VERSION}-1"
    assert body["build_steps"] == []
    assert any("sha256sum -c" in step for step in body["install_steps"])


# --- SSH step 1: host key -------------------------------------------------------


def test_ssh_target_defaults_to_the_node_ip_pve_reports(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeProxmox(
        routes={
            ("GET", "/api2/json/cluster/status"): FakeResponse(
                {
                    "data": [
                        {"type": "cluster", "name": "lab", "quorate": 1},
                        {"type": "node", "name": "pve1", "ip": "192.0.2.21", "online": 1},
                        {"type": "node", "name": "pve2", "online": 0},
                    ]
                }
            )
        }
    ).install(monkeypatch)
    one = client.get(f"{BASE}/{cluster_id}/lab-setup/nodes/pve1/ssh-target", headers=platform_admin_headers).json()
    assert one == {
        "node": "pve1",
        "host": "192.0.2.21",
        "host_source": "cluster_status",
        "port": 22,
        "username": "root",
    }
    two = client.get(f"{BASE}/{cluster_id}/lab-setup/nodes/pve2/ssh-target", headers=platform_admin_headers).json()
    assert two["host"] == "pve.lab.example" and two["host_source"] == "cluster_host"


def test_host_key_step_returns_the_fingerprint_without_logging_in(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = FakeSshBackend().install(monkeypatch)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/ssh/host-key",
        json={"node": "pve1", "host": "192.0.2.21", "port": 2222},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fingerprint_sha256"] == FINGERPRINT
    assert body["key_type"] == "ssh-ed25519"
    assert "ssh-keygen -lf" in body["verify_command"]
    assert fake.host_key_calls == [("192.0.2.21", 2222)]
    assert fake.auth_attempts == [] and fake.sessions == []


def test_host_key_step_rejects_shell_metacharacters_in_host(client, platform_admin_headers, db_session):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/ssh/host-key",
        json={"node": "pve1", "host": "pve1; rm -rf /"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


# --- agent install --------------------------------------------------------------


def _start_agent_install(client, headers, cluster_id, **extra):
    body = {"ssh": _ssh(**extra.pop("ssh", {})), **extra}
    return client.post(f"{BASE}/{cluster_id}/lab-setup/agent-install-jobs", json=body, headers=headers)


def test_agent_install_as_root_full_sequence(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs, caplog
):
    caplog.set_level(logging.DEBUG)
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = _agent_node(FakeSshBackend(accept_password=PASSWORD)).install(monkeypatch)

    response = _start_agent_install(
        client, platform_admin_headers, cluster_id, pbs_token=PBS_TOKEN, overlay_storage_id="labov"
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "succeeded", job
    assert [s["key"] for s in job["steps"]] == [
        "connect",
        "privileges",
        "preflight",
        "upload",
        "install",
        "configure",
        "service",
    ]
    assert all(s["status"] == "ok" for s in job["steps"])
    assert job["result"]["privilege_mode"] == "root"
    assert job["result"]["pbs_token_status"] == "provided"

    # Upload -> verify -> install -> env file -> service, never via sudo as root.
    deb = artifacts["resurgam-lab-agent"]["filename"]
    sequence = [c for c in fake.commands if not c.startswith(("id -u", "command -v", "cat /etc/os", "dpkg-query"))]
    assert sequence[0] == 'd=$(mktemp -d /tmp/resurgam-lab-agent.XXXXXX) && chmod 755 "$d" && echo "$d"'
    assert sequence[1] == f"sha256sum /tmp/resurgam-lab-agent.abc123/{deb}"
    assert sequence[2].startswith("DEBIAN_FRONTEND=noninteractive apt-get install -y")
    assert sequence[2].endswith(f"/tmp/resurgam-lab-agent.abc123/{deb}")
    assert "--force-confold" in sequence[2]
    assert (
        sequence[3] == "install -m 0600 -o root -g root /tmp/resurgam-lab-agent.abc123/resurgam-lab-agent.env "
        "/etc/default/resurgam-lab-agent"
    )
    assert sequence[4] == "systemctl enable resurgam-lab-agent && systemctl restart resurgam-lab-agent"
    assert sequence[-1] == "rm -rf /tmp/resurgam-lab-agent.abc123"
    assert not any("sudo" in c for c in fake.commands if c != "sudo -n true")

    env_upload = next(u for u in fake.uploads if u.remote_path.endswith("resurgam-lab-agent.env"))
    assert env_upload.mode == 0o600
    env = dict(
        line.split("=", 1) for line in env_upload.data.decode().splitlines() if line and not line.startswith("#")
    )
    assert env["RESURGAM_BACKEND_URL"] == "https://resurgam.test.invalid/api/v1"
    assert env["RESURGAM_LAB_AGENT_PBS_TOKEN"] == PBS_TOKEN
    assert env["RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID"] == "labov"
    assert env["RESURGAM_LAB_AGENT_OVERLAY_DIR"] == "/var/lib/resurgam-lab-agent/overlays"
    new_token = env["RESURGAM_LAB_AGENT_TOKEN"]

    # The new token is what the cluster now accepts; the old one is gone.
    db_session.expire_all()
    cluster = db_session.get(PveCluster, cluster_id)
    assert verify_appliance_run_token(new_token, cluster.lab_agent_token_hash)
    assert not verify_appliance_run_token("old-agent-token", cluster.lab_agent_token_hash)

    actions = [e.action for e in db_session.query(AuditEvent).order_by(AuditEvent.occurred_at).all()]
    assert "pve_cluster_lab_agent_install_started" in actions
    assert "pve_cluster_lab_agent_token_rotated" in actions
    assert "pve_cluster_lab_agent_installed" in actions

    # Nothing secret anywhere: response, poll, audit, commands, logs.
    polled = client.get(f"{BASE}/{cluster_id}/lab-setup/jobs/{job['id']}", headers=platform_admin_headers)
    assert polled.status_code == 200
    _assert_no_secrets(
        response.text,
        polled.text,
        _all_audit_text(db_session),
        "\n".join(fake.commands),
        caplog.text,
        secrets=(PASSWORD, PBS_TOKEN, new_token),
    )


def test_agent_install_via_sudo_wraps_privileged_commands(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = _agent_node(FakeSshBackend(accept_key=PRIVATE_KEY), uid="1000").install(monkeypatch)

    response = _start_agent_install(
        client,
        platform_admin_headers,
        cluster_id,
        pbs_token=PBS_TOKEN,
        ssh={"username": "admin", "auth_method": "private_key", "password": None, "private_key": PRIVATE_KEY},
    )
    job = response.json()
    assert job["status"] == "succeeded", job
    assert job["result"]["privilege_mode"] == "sudo"
    assert fake.auth_attempts == ["private_key"]
    apt = next(c for c in fake.commands if "apt-get install" in c)
    assert apt.startswith("sudo -n sh -c ")
    assert next(c for c in fake.commands if "/etc/default/resurgam-lab-agent" in c and "install -m" in c).startswith(
        "sudo -n sh -c "
    )
    # Unprivileged steps stay unprivileged.
    assert next(c for c in fake.commands if "mktemp" in c).startswith("d=$(mktemp -d /tmp/resurgam-lab-agent.")
    _assert_no_secrets(response.text, _all_audit_text(db_session), "\n".join(fake.commands))


def test_agent_install_neither_root_nor_sudo_fails_clearly(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = _agent_node(FakeSshBackend(accept_password=PASSWORD), uid="1000", sudo_ok=False).install(monkeypatch)

    job = _start_agent_install(client, platform_admin_headers, cluster_id, ssh={"username": "admin"}).json()

    assert job["status"] == "failed"
    assert job["error_code"] == "privilege_missing"
    assert "passwordless sudo" in job["error_message"]
    assert not any("apt-get" in c for c in fake.commands)
    assert all(s.closed for s in fake.sessions)
    db_session.expire_all()
    assert verify_appliance_run_token("old-agent-token", db_session.get(PveCluster, cluster_id).lab_agent_token_hash)
    assert "pve_cluster_lab_agent_install_failed" in _all_audit_text(db_session)


def test_agent_install_refuses_a_changed_host_key_before_sending_credentials(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = FakeSshBackend(fingerprint=OTHER_FINGERPRINT, accept_password=PASSWORD).install(monkeypatch)

    job = _start_agent_install(client, platform_admin_headers, cluster_id).json()

    assert job["status"] == "failed"
    assert job["error_code"] == "host_key_mismatch"
    assert fake.auth_attempts == []
    assert fake.commands == []
    assert job["steps"][0]["key"] == "connect" and job["steps"][0]["status"] == "failed"


def test_agent_install_wrong_password(client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeSshBackend(accept_password="something-else").install(monkeypatch)
    response = _start_agent_install(client, platform_admin_headers, cluster_id)
    job = response.json()
    assert job["error_code"] == "auth_failed"
    _assert_no_secrets(response.text, _all_audit_text(db_session))


def test_agent_install_keeps_the_nodes_pbs_token_and_warns_without_one(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    existing = f"RESURGAM_BACKEND_URL=https://old\n#RESURGAM_LAB_AGENT_PBS_TOKEN=commented\nRESURGAM_LAB_AGENT_PBS_TOKEN={PBS_TOKEN}\n"
    fake = _agent_node(FakeSshBackend(accept_password=PASSWORD), env_file=existing).install(monkeypatch)
    response = _start_agent_install(client, platform_admin_headers, cluster_id)
    job = response.json()
    assert job["status"] == "succeeded"
    assert job["result"]["pbs_token_status"] == "kept"
    env = next(u for u in fake.uploads if u.remote_path.endswith(".env")).data.decode()
    assert f"RESURGAM_LAB_AGENT_PBS_TOKEN={PBS_TOKEN}\n" in env
    _assert_no_secrets(response.text)

    fake2 = _agent_node(FakeSshBackend(accept_password=PASSWORD), active="failed").install(monkeypatch)
    job = _start_agent_install(client, platform_admin_headers, cluster_id).json()
    assert job["status"] == "succeeded"
    assert job["result"]["pbs_token_status"] == "missing"
    steps = {s["key"]: s for s in job["steps"]}
    assert steps["configure"]["status"] == "warning"
    assert steps["service"]["status"] == "warning"
    assert "RESURGAM_LAB_AGENT_PBS_TOKEN" in steps["configure"]["message"]
    assert any("journalctl" in c for c in fake2.commands)


def test_agent_install_debian_release_mismatch(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = _agent_node(FakeSshBackend(accept_password=PASSWORD), codename="bookworm").install(monkeypatch)
    job = _start_agent_install(client, platform_admin_headers, cluster_id).json()
    assert job["error_code"] == "debian_release_mismatch"
    assert "PVE 8" in job["error_message"] and "trixie" in job["error_message"]
    assert not fake.uploads


def test_agent_install_failure_keeps_the_old_token_and_cleans_up(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = _agent_node(FakeSshBackend(accept_password=PASSWORD))
    fake.respond(r"apt-get install", fail("E: Unable to locate package proxmox-backup-client", 100))
    fake.install(monkeypatch)

    job = _start_agent_install(client, platform_admin_headers, cluster_id, pbs_token=PBS_TOKEN).json()

    assert job["error_code"] == "install_failed"
    install = next(s for s in job["steps"] if s["key"] == "install")
    assert install["status"] == "failed" and "proxmox-backup-client" in install["output"]
    assert fake.commands[-1] == "rm -rf /tmp/resurgam-lab-agent.abc123"
    db_session.expire_all()
    assert verify_appliance_run_token("old-agent-token", db_session.get(PveCluster, cluster_id).lab_agent_token_hash)


def test_agent_install_corrupted_upload_is_not_installed(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = _agent_node(FakeSshBackend(accept_password=PASSWORD))
    fake.respond(r"^sha256sum ", ok("0" * 64 + "  x\n"))
    fake.install(monkeypatch)
    job = _start_agent_install(client, platform_admin_headers, cluster_id).json()
    assert job["error_code"] == "upload_corrupted"
    assert not any("apt-get" in c for c in fake.commands)


def test_agent_install_validation(client, platform_admin_headers, db_session, artifacts, sync_jobs, test_settings):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    missing_password = _start_agent_install(client, platform_admin_headers, cluster_id, ssh={"password": None})
    assert missing_password.status_code == 422
    bad_fp = _start_agent_install(client, platform_admin_headers, cluster_id, ssh={"confirmed_fingerprint": "abc"})
    assert bad_fp.status_code == 422
    bad_pbs = _start_agent_install(client, platform_admin_headers, cluster_id, pbs_token="has space!x:y")
    assert bad_pbs.status_code == 422
    for response in (missing_password, bad_fp, bad_pbs):
        assert PASSWORD not in response.text

    test_settings.recovery_lab_public_backend_url = ""
    no_url = _start_agent_install(client, platform_admin_headers, cluster_id)
    assert no_url.status_code == 400
    assert no_url.json()["code"] == "recovery_lab_backend_url_not_configured"


def test_one_job_per_cluster_and_unknown_job(client, platform_admin_headers, db_session, monkeypatch, artifacts):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    pending = []
    monkeypatch.setattr(jobs_module.registry, "executor", lambda fn: pending.append(fn))
    monkeypatch.setattr(jobs_module.registry, "_jobs", {})
    FakeSshBackend(accept_password=PASSWORD).install(monkeypatch)

    first = _start_agent_install(client, platform_admin_headers, cluster_id)
    assert first.status_code == 202 and first.json()["status"] == "running"
    second = _start_agent_install(client, platform_admin_headers, cluster_id)
    assert second.status_code == 409
    assert second.json()["code"] == "lab_setup_job_running"

    unknown = client.get(
        f"{BASE}/{cluster_id}/lab-setup/jobs/00000000-0000-0000-0000-000000000000", headers=platform_admin_headers
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "lab_setup_job_not_found"


def test_guided_endpoints_require_platform_admin(client, db_session, platform_admin_headers):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    for method, path in (
        ("post", f"{BASE}/{cluster_id}/lab-setup/ssh/host-key"),
        ("post", f"{BASE}/{cluster_id}/lab-setup/agent-install-jobs"),
        ("post", f"{BASE}/{cluster_id}/lab-setup/template-build-jobs"),
        ("get", f"{BASE}/{cluster_id}/lab-setup/next-vmid"),
    ):
        assert getattr(client, method)(path).status_code == 401, path


# --- template build -------------------------------------------------------------


def _pve_for_template(monkeypatch, *, nextid="9010", taken: set[int] | None = None):
    taken = taken or set()

    def nextid_handler(url, kwargs):
        params = kwargs.get("params") or {}
        if "vmid" in params:
            if int(params["vmid"]) in taken:
                return FakeResponse(
                    {"errors": {"vmid": f"VM {params['vmid']} already exists"}},
                    status_code=400,
                    text=f'{{"errors":{{"vmid":"VM {params["vmid"]} already exists"}}}}',
                )
            return FakeResponse({"data": str(params["vmid"])})
        return FakeResponse({"data": nextid})

    return FakeProxmox(
        routes={
            ("GET", "/api2/json/nodes"): FakeResponse({"data": [{"node": "pve1", "status": "online"}]}),
            ("GET", "/api2/json/storage"): FakeResponse(
                {
                    "data": [
                        {"storage": "local", "type": "dir", "content": "iso,snippets", "path": "/var/lib/vz"},
                        {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"},
                    ]
                }
            ),
            ("GET", "/api2/json/cluster/nextid"): nextid_handler,
        }
    ).install(monkeypatch)


def _template_node(fake: FakeSshBackend, *, verdict=None, result_after: int = 2):
    """A node whose build VM reports `verdict` after `result_after` polls."""
    polls = {"n": 0}

    def guest_result(command):
        polls["n"] += 1
        if polls["n"] < result_after:
            return fail("QEMU guest agent is not running", 255)
        return guest_exec(verdict if verdict is not None else {"status": "ok", "prober_version": f"{VERSION}-1"})

    def sha(command):
        path = shlex.split(command)[1]
        data = next(u.data for u in reversed(fake.uploads) if u.remote_path == path)
        return ok(f"{hashlib.sha256(data).hexdigest()}  {path}\n")

    fake.respond(r"^id -u$", ok("0\n"))
    # The per-tool preflight loop (`cmd_check_tools`) prints nothing when all are there.
    fake.respond(r"^missing=; for t in qm pvesm pvesh genisoimage curl sha512sum sha256sum;", ok(""))
    fake.respond(r"mktemp -d /var/tmp/resurgam-template", ok("/var/tmp/resurgam-template.w0rk\n"))
    fake.respond(r"^sha256sum ", sha)
    fake.respond(r"^pvesm path local:snippets/", ok("/var/lib/vz/snippets/resurgam-template-build-9010.yaml\n"))
    fake.respond(r"^pvesm path local:iso/", ok("/var/lib/vz/template/iso/resurgam-template-build-9010-seed.iso\n"))
    fake.respond(r"cat /var/lib/resurgam-template-build/result.json", guest_result)
    fake.respond(r"tail -n 40", guest_exec(None))
    fake.respond(r"^qm status", ok("status: running\n"))
    return fake


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(template_module.time, "sleep", lambda s: None)


def _start_build(client, headers, cluster_id, **extra):
    body = {"ssh": _ssh(**extra.pop("ssh", {})), "storage": "local-lvm", **extra}
    return client.post(f"{BASE}/{cluster_id}/lab-setup/template-build-jobs", json=body, headers=headers)


def test_template_build_success_sequence(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs, no_sleep, caplog
):
    caplog.set_level(logging.DEBUG)
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_appliance_template_vmid=9003)
    _pve_for_template(monkeypatch)
    fake = _template_node(FakeSshBackend(accept_password=PASSWORD)).install(monkeypatch)

    response = _start_build(client, platform_admin_headers, cluster_id)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "succeeded", job
    assert [s["key"] for s in job["steps"]] == [
        "connect",
        "privileges",
        "preflight",
        "image",
        "seed",
        "snippet",
        "create",
        "first_boot",
        "finalize",
    ]
    assert job["result"]["template_vmid"] == 9010
    assert job["result"]["prober_version"] == f"{VERSION}-1"

    commands = fake.commands
    wd = "/var/tmp/resurgam-template.w0rk"
    assert "pvesh get /cluster/nextid --vmid 9010" in commands
    download = next(c for c in commands if "cloud.debian.org" in c)
    assert "debian-13-genericcloud-amd64.qcow2" in download and "sha512sum" in download
    create = next(c for c in commands if c.startswith("qm create"))
    args = shlex.split(create)
    assert args[:3] == ["qm", "create", "9010"]
    opts = dict(zip(args[3::2], args[4::2], strict=False))
    assert opts["--scsi0"] == f"local-lvm:0,import-from={wd}/debian-13-genericcloud-amd64.qcow2"
    assert opts["--ide2"] == "local-lvm:cloudinit"
    # PVE 9 refuses a drive path outside any storage (live finding 2026-11-21).
    assert opts["--ide3"] == "local:iso/resurgam-template-build-9010-seed.iso,media=cdrom"
    assert opts["--net0"] == "model=virtio,bridge=vmbr0"
    assert opts["--ipconfig0"] == "ip=dhcp"
    assert opts["--cicustom"] == "user=local:snippets/resurgam-template-build-9010.yaml"
    assert opts["--agent"] == "enabled=1"
    assert opts["--serial0"] == "socket" and opts["--vga"] == "serial0"
    assert "--nameserver" not in opts
    order = [
        next(i for i, c in enumerate(commands) if c.startswith(prefix))
        for prefix in (
            "qm create",
            "qm start",
            "qm guest exec 9010 --timeout 120",
            "qm shutdown",
            "qm set 9010 --delete",
        )
    ]
    assert order == sorted(order)
    assert any(c.startswith("qm set 9010 --description") and PROBER_VERSION_MARKER in c for c in commands)
    assert commands.index("qm template 9010") > order[-1]
    assert not any(c.startswith("qm destroy") or "qm destroy" in c for c in commands)
    # Clean up after itself.
    assert "rm -f /var/lib/vz/snippets/resurgam-template-build-9010.yaml" in commands
    assert "rm -f /var/lib/vz/template/iso/resurgam-template-build-9010-seed.iso" in commands
    assert commands[-1] == f"rm -rf {wd}"

    seed = {u.remote_path: u for u in fake.uploads}
    assert f"{wd}/seed/{artifacts['resurgam-lab-prober']['filename']}" in seed
    build_sh = seed[f"{wd}/seed/build.sh"].data.decode()
    assert "qemu-guest-agent" in build_sh and artifacts["resurgam-lab-prober"]["filename"] in build_sh
    user_data = seed[f"{wd}/user-data.yaml"].data.decode()
    assert user_data.startswith("#cloud-config") and "RESURGAMSEED" in user_data

    db_session.expire_all()
    cluster = db_session.get(PveCluster, cluster_id)
    assert cluster.lab_appliance_template_vmid == 9010
    built = next(e for e in db_session.query(AuditEvent).all() if e.action == "pve_cluster_lab_template_built")
    assert built.detail["previous_vmid"] == 9003 and built.detail["vmid"] == 9010
    _assert_no_secrets(response.text, _all_audit_text(db_session), "\n".join(commands), caplog.text)


def test_template_build_static_uplink_with_vlan_allocates_and_releases(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs, no_sleep
):
    cluster_id = _create_cluster(
        client,
        platform_admin_headers,
        db_session,
        lab_appliance_uplink_vlan_tag=42,
        lab_appliance_uplink_ip_mode="static_range",
        lab_appliance_uplink_ip_range_start="192.168.40.50",
        lab_appliance_uplink_ip_range_end="192.168.40.60",
        lab_appliance_uplink_ip_prefix_length=24,
        lab_appliance_uplink_gateway="192.168.40.1",
        lab_appliance_uplink_dns_servers=["192.168.40.2", "9.9.9.9"],
    )
    _pve_for_template(monkeypatch)
    seen = {}
    fake = _template_node(FakeSshBackend(accept_password=PASSWORD))

    def create(command):
        seen["allocations"] = [a.ip_address for a in db_session.query(LabUplinkIpAllocation).all()]
        return ok()

    fake.respond(r"^qm create", create)
    fake.install(monkeypatch)

    job = _start_build(client, platform_admin_headers, cluster_id, vmid=9020).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["uplink_address"] == "192.168.40.50"
    opts = shlex.split(next(c for c in fake.commands if c.startswith("qm create")))
    opts = dict(zip(opts[3::2], opts[4::2], strict=False))
    assert opts["--net0"] == "model=virtio,bridge=vmbr0,tag=42"
    assert opts["--ipconfig0"] == "ip=192.168.40.50/24,gw=192.168.40.1"
    assert opts["--nameserver"] == "192.168.40.2 9.9.9.9"
    assert "qm set 9020 --delete ide3,cicustom,ipconfig0,net0,nameserver" in fake.commands
    # Held while the build ran, released afterwards.
    assert seen["allocations"] == ["192.168.40.50"]
    assert db_session.query(LabUplinkIpAllocation).count() == 0


def test_template_build_failure_after_create_destroys_only_its_own_vm(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs, no_sleep
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_appliance_template_vmid=9003)
    _pve_for_template(monkeypatch)
    fake = _template_node(
        FakeSshBackend(accept_password=PASSWORD), verdict={"status": "failed", "step": "apt_update"}
    ).install(monkeypatch)

    job = _start_build(client, platform_admin_headers, cluster_id).json()

    assert job["status"] == "failed"
    assert job["error_code"] == "build_failed"
    assert "apt_update" in job["error_message"]
    steps = {s["key"]: s for s in job["steps"]}
    assert steps["first_boot"]["status"] == "failed"
    assert steps["cleanup"]["status"] == "ok"
    destroy = [c for c in fake.commands if "qm destroy" in c]
    assert destroy == [
        "qm stop 9010 --skiplock 1 >/dev/null 2>&1; qm destroy 9010 --purge 1 --destroy-unreferenced-disks 1"
    ]
    assert "qm template 9010" not in fake.commands
    db_session.expire_all()
    assert db_session.get(PveCluster, cluster_id).lab_appliance_template_vmid == 9003
    assert "pve_cluster_lab_template_build_failed" in _all_audit_text(db_session)


def test_template_build_failure_before_create_destroys_nothing(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs, no_sleep
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    _pve_for_template(monkeypatch)
    fake = _template_node(FakeSshBackend(accept_password=PASSWORD))
    fake.respond(r"cloud\.debian\.org", fail("curl: (6) Could not resolve host: cloud.debian.org", 6))
    fake.install(monkeypatch)

    job = _start_build(client, platform_admin_headers, cluster_id).json()

    assert job["error_code"] == "image_download_failed"
    assert "cloud.debian.org" in job["error_message"]
    assert not any("qm create" in c or "qm destroy" in c for c in fake.commands)
    assert fake.commands[-1] == "rm -rf /var/tmp/resurgam-template.w0rk"


def test_template_build_qm_create_failure_does_not_destroy(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs, no_sleep
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    _pve_for_template(monkeypatch)
    fake = _template_node(FakeSshBackend(accept_password=PASSWORD))
    fake.respond(r"^qm create", fail("unable to create VM 9010 - VM 9010 already exists on node 'pve2'"))
    fake.install(monkeypatch)
    job = _start_build(client, platform_admin_headers, cluster_id).json()
    assert job["error_code"] == "vm_create_failed"
    assert not any("qm destroy" in c for c in fake.commands)


def test_template_build_timeout(client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    _pve_for_template(monkeypatch)
    fake = _template_node(FakeSshBackend(accept_password=PASSWORD), result_after=10**6).install(monkeypatch)
    clock = {"t": 0.0}
    monkeypatch.setattr(template_module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(template_module.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    job = _start_build(client, platform_admin_headers, cluster_id).json()
    assert job["error_code"] == "build_timeout"
    assert "qm terminal 9010" in job["error_message"]
    assert any("qm destroy 9010" in c for c in fake.commands)


def test_template_build_existing_node_image(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs, no_sleep
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    _pve_for_template(monkeypatch)
    fake = _template_node(FakeSshBackend(accept_password=PASSWORD)).install(monkeypatch)
    job = _start_build(
        client,
        platform_admin_headers,
        cluster_id,
        image_source="node_image",
        node_image="local:import/debian-13-genericcloud-amd64.qcow2",
    ).json()
    assert job["status"] == "succeeded", job
    assert "pvesm path local:import/debian-13-genericcloud-amd64.qcow2" in fake.commands
    assert not any("cloud.debian.org" in c for c in fake.commands)
    create = next(c for c in fake.commands if c.startswith("qm create"))
    assert "import-from=local:import/debian-13-genericcloud-amd64.qcow2" in create


def test_template_build_request_checks(client, platform_admin_headers, db_session, monkeypatch, artifacts, sync_jobs):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    _pve_for_template(monkeypatch, taken={9003})
    FakeSshBackend(accept_password=PASSWORD).install(monkeypatch)

    taken = _start_build(client, platform_admin_headers, cluster_id, vmid=9003)
    assert taken.status_code == 409 and taken.json()["code"] == "vmid_taken"
    bad_storage = _start_build(client, platform_admin_headers, cluster_id, storage="local")
    assert bad_storage.status_code == 400 and bad_storage.json()["code"] == "storage_unsuitable"
    bad_node = _start_build(client, platform_admin_headers, cluster_id, ssh={"node": "pve9"})
    assert bad_node.status_code == 400 and bad_node.json()["code"] == "unknown_nodes"
    bad_image = _start_build(
        client, platform_admin_headers, cluster_id, image_source="node_image", node_image="../../etc/shadow"
    )
    assert bad_image.status_code == 422
    no_image = _start_build(client, platform_admin_headers, cluster_id, image_source="node_image")
    assert no_image.status_code == 422

    cluster = db_session.get(PveCluster, cluster_id)
    cluster.lab_snippet_storage_id = None
    db_session.commit()
    missing = _start_build(client, platform_admin_headers, cluster_id)
    assert missing.status_code == 400 and missing.json()["code"] == "lab_template_prerequisites_missing"


def test_template_build_script_and_next_vmid(client, platform_admin_headers, db_session, monkeypatch, artifacts):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_appliance_uplink_vlan_tag=7)
    _pve_for_template(monkeypatch)
    assert client.get(f"{BASE}/{cluster_id}/lab-setup/next-vmid", headers=platform_admin_headers).json() == {
        "vmid": 9010
    }
    response = client.get(
        f"{BASE}/{cluster_id}/lab-setup/template-build-script?node=pve1&storage=local-lvm",
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vmid"] == 9010
    script = body["script"]
    assert script.startswith("#!/bin/sh")
    assert artifacts["resurgam-lab-prober"]["sha256"] in script
    assert "qm create 9010" in script and "tag=7" in script
    assert "qm template 9010" in script
    assert "pvesm path local:snippets/resurgam-template-build-9010.yaml" in script
    assert "cloud.debian.org" in script
    # Every path is literal: the quoted qm arguments must not rely on $VAR expansion.
    assert "import-from=/var/tmp/resurgam-template.9010/debian-13-genericcloud-amd64.qcow2" in script
    assert "$WORKDIR/debian" not in script and "'$WORKDIR" not in script
    # The poll loop greps the JSON-escaped status in qm's output.
    assert re.search(r"grep -q '\\\\\"status\\\\\":\\\\\"ok\\\\\"'", script)


def test_parse_prober_version():
    assert parse_prober_version(f"RESURGAM ...\n{PROBER_VERSION_MARKER}: 0.2.1-1\nbase-image: x") == "0.2.1-1"
    assert parse_prober_version("hand-built template") is None
    assert parse_prober_version(None) is None


def test_openapi_documents_every_provisioning_field(client):
    spec = client.get("/api/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name in (
        "LabArtifactsOut",
        "LabArtifactOut",
        "SshTargetOut",
        "SshHostKeyRequest",
        "SshHostKeyOut",
        "SshLoginRequest",
        "LabAgentInstallJobRequest",
        "LabTemplateBuildJobRequest",
        "LabSetupJobOut",
        "LabSetupJobStepOut",
        "NextVmidOut",
        "LabTemplateBuildScriptOut",
    ):
        for field_name, field in schemas[name]["properties"].items():
            assert field.get("description"), f"{name}.{field_name}"
    for path in (
        "/api/v1/pve-clusters/lab-setup/downloads",
        "/api/v1/pve-clusters/lab-setup/downloads/{artifact}",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/nodes/{node}/ssh-target",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/ssh/host-key",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/agent-install-jobs",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/template-build-jobs",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/jobs/{job_id}",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/template-build-script",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/next-vmid",
    ):
        operation = next(iter(spec["paths"][path].values()))
        assert operation.get("description"), path
    pve_out = schemas["PveClusterOut"]["properties"]
    for field_name in (
        "lab_appliance_uplink_vlan_tag",
        "lab_appliance_uplink_ip_mode",
        "lab_appliance_uplink_ip_range_start",
        "lab_appliance_uplink_ip_range_end",
        "lab_appliance_uplink_ip_prefix_length",
        "lab_appliance_uplink_gateway",
        "lab_appliance_uplink_dns_servers",
    ):
        assert pve_out[field_name].get("description"), field_name


# --- readiness: template prober version ----------------------------------------


def _readiness_template_item(client, headers, db_session, monkeypatch, description):
    from tests.test_pve_lab_setup_api import _ok_routes

    cluster_id = _create_cluster(client, headers, db_session, lab_appliance_template_vmid=9003)
    routes = _ok_routes()
    config = {"name": "lab-appliance", "template": 1}
    if description is not None:
        config["description"] = description
    routes[("GET", "/api2/json/nodes/pve1/qemu/9003/config")] = FakeResponse({"data": config})
    FakeProxmox(routes=routes).install(monkeypatch)
    body = client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=headers).json()
    return next(i for i in body["items"] if i["key"] == "appliance_template")


@pytest.mark.parametrize(
    ("description", "status", "code"),
    [
        (f"RESURGAM ...\n{PROBER_VERSION_MARKER}: {VERSION}-1\n", "ok", "template_ok"),
        (f"RESURGAM ...\n{PROBER_VERSION_MARKER}: 0.1.0-1\n", "warning", "template_outdated"),
        ("hand-built in 2026-09", "warning", "template_version_unknown"),
        (None, "warning", "template_version_unknown"),
    ],
)
def test_readiness_compares_the_template_prober_version(
    client, platform_admin_headers, db_session, monkeypatch, artifacts, description, status, code
):
    item = _readiness_template_item(client, platform_admin_headers, db_session, monkeypatch, description)
    assert (item["status"], item["code"]) == (status, code), item
    assert item["details"]["shipped_prober_version"] == f"{VERSION}-1"


def test_readiness_without_shipped_artifacts_skips_the_version_check(
    client, platform_admin_headers, db_session, monkeypatch, test_settings, tmp_path
):
    test_settings.lab_artifacts_dir = str(tmp_path / "none")
    item = _readiness_template_item(client, platform_admin_headers, db_session, monkeypatch, "hand-built")
    assert (item["status"], item["code"]) == ("ok", "template_ok")


# --- versioning ----------------------------------------------------------------------


def test_debian_changelog_upstream_version_matches_VERSION():  # noqa: N802 - the file is called VERSION
    """restore-worker/README.md "Versioning": the packages shipped in the image
    carry the release version; only the Debian revision may differ."""
    first = (REPO / "restore-worker/debian/changelog").read_text().splitlines()[0]
    match = re.match(r"^resurgam-worker \(([^)]+)\)", first)
    assert match, first
    assert match.group(1).rsplit("-", 1)[0] == VERSION


def test_manifest_script_rejects_a_version_mismatch(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lab_artifacts_manifest", REPO / "appliance/release/lab_artifacts_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.upstream_version("0.2.1-1") == "0.2.1"
    assert module.upstream_version("1:0.2.1-3") == "0.2.1"

    (tmp_path / "resurgam-lab-agent_0.2.0-1_amd64.deb").write_bytes(b"x")
    (tmp_path / "VERSION").write_text("0.2.1\n")
    fields = {"Package": "resurgam-lab-agent", "Version": "0.2.0-1", "Architecture": "amd64"}
    monkeypatch.setattr(module, "_deb_field", lambda path, field: fields[field])
    monkeypatch.setattr(module, "_os_release", lambda: {"VERSION_CODENAME": "trixie", "VERSION_ID": "13"})
    assert module.main(str(tmp_path), str(tmp_path / "VERSION")) == 1
    assert "dch -v 0.2.1-1" in capsys.readouterr().err

    fields["Version"] = "0.2.1-1"
    assert module.main(str(tmp_path), str(tmp_path / "VERSION")) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["debian_release"] == "trixie"
    assert manifest["artifacts"][0]["sha256"] == hashlib.sha256(b"x").hexdigest()


def test_template_build_script_rejects_a_bad_node_image(client, platform_admin_headers, db_session, artifacts):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.get(
        f"{BASE}/{cluster_id}/lab-setup/template-build-script?node=pve1&storage=local-lvm"
        "&image_source=node_image&node_image=../../etc/shadow",
        headers=platform_admin_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_node_image"
