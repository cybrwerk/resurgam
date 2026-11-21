"""Lab node pairing with a one-time code and agent-run template builds
(ADR 0027). PVE is mocked HTTP; nothing real is contacted."""

import hashlib
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from resurgam_api.api.v1.lab_pairing import _expire_if_stale
from resurgam_api.lab_setup import pairing as pairing_module
from resurgam_api.lab_setup.agent_install import render_lab_agent_env_file
from resurgam_api.lab_setup.artifacts import LabArtifact
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.lab_setup import LabAgentSetupJob, LabNodePairing
from resurgam_api.models.pve import LabUplinkIpAllocation, PveCluster
from resurgam_api.restore_test.execution import verify_appliance_run_token
from tests.lab_ssh_fakes import FakeSshBackend
from tests.proxmox_fakes import FakeProxmox, FakeResponse
from tests.test_lab_provisioning_api import (
    BASE,
    _create_cluster,
    _pve_for_template,
    _ssh,
)

API_BASE = "https://resurgam.test.invalid/api/v1"
CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfakeCAforTests\n-----END CERTIFICATE-----\n"


@pytest.fixture
def ca_file(tmp_path, test_settings):
    path = tmp_path / "root.crt"
    path.write_text(CA_PEM)
    test_settings.appliance_tls_ca_file = str(path)
    return path


@pytest.fixture(autouse=True)
def pve_nodes(db_session, monkeypatch):
    """The cluster's PVE API knows the nodes pve1 and pve2 (pairing checks the node). After `db_session`:
    the fake also answers DNS, which would send the database connection elsewhere."""
    return FakeProxmox(
        routes={("GET", "/api2/json/nodes"): FakeResponse({"data": [{"node": "pve1"}, {"node": "pve2"}]})}
    ).install(monkeypatch)


def _pair(client, headers, cluster_id, node="pve1"):
    response = client.post(f"{BASE}/{cluster_id}/lab-setup/pairings", json={"node": node}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _code_path(code: str) -> str:
    return f"/api/v1/lab-setup/bootstrap/{code}"


def _script_path(pairing_id: str) -> str:
    return f"/api/v1/lab-setup/bootstrap/p/{pairing_id}/script"


def _paired_agent(client, headers, db_session, cluster_id) -> str:
    """Pairs pve1 and returns the agent token from the env file."""
    created = _pair(client, headers, cluster_id)
    env = client.post(f"{_code_path(created['code'])}/pair", json={"node": "pve1", "pbs_token_provided": True}).text
    return next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("RESURGAM_LAB_AGENT_TOKEN="))


def test_pairing_command_script_and_one_time_exchange(client, platform_admin_headers, db_session, artifacts, ca_file):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    created = _pair(client, platform_admin_headers, cluster_id)
    code = created["code"]
    assert created["state"] == "waiting"
    assert created["script_sha256"] in created["command"]
    # The unverified (-k) download goes by the pairing id; the code is only the script's argument.
    download, _, run = created["command"].partition(" && echo ")
    assert download == f"curl -fsSk -o /root/resurgam-pair.sh {API_BASE}/lab-setup/bootstrap/p/{created['id']}/script"
    assert code not in download
    assert run.endswith(f"&& sh /root/resurgam-pair.sh {code}")
    assert created["shell_url"] == "https://pve.lab.example:8006/?console=shell&xtermjs=1&vmid=0&vmname=&node=pve1&cmd="

    assert client.get(f"{_code_path(code)}/script").status_code == 404  # the code never fetches the script
    script = client.get(_script_path(created["id"]))
    assert script.status_code == 200
    assert hashlib.sha256(script.text.encode()).hexdigest() == created["script_sha256"]
    assert CA_PEM.strip() in script.text and "--cacert /etc/resurgam-lab-agent/appliance-ca.pem" in script.text
    assert artifacts["resurgam-lab-agent"]["sha256"] in script.text
    assert "RESURGAM_LAB_AGENT_TOKEN=" not in script.text.replace("'^RESURGAM_LAB_AGENT_TOKEN='", "")
    assert code not in script.text  # holds no secret: it was fetched without TLS verification
    # The code is sent with the CA-pinned options only, and the one-shot POST is never retried.
    for line in script.text.splitlines():
        if "$CODE/" in line:
            assert line.lstrip().startswith(("curl -fsS --retry 3 $TLS ", "STATUS=$(curl -sS $TLS ", '-d "')), line
    assert '"$API/$CODE/pair"' in script.text and "--retry 3 $TLS -o" in script.text
    pair_call = next(line for line in script.text.splitlines() if line.startswith("STATUS=$(curl"))
    assert "--retry" not in pair_call and " -f" not in pair_call
    # The script is valid POSIX shell.
    assert subprocess.run(["sh", "-n"], input=script.text, text=True, check=False).returncode == 0

    deb = client.get(f"{_code_path(code)}/agent.deb")
    assert deb.status_code == 200
    assert hashlib.sha256(deb.content).hexdigest() == artifacts["resurgam-lab-agent"]["sha256"]

    wrong = client.post(f"{_code_path(code)}/pair", json={"node": "pve2"})
    assert wrong.status_code == 409 and wrong.json()["code"] == "pairing_node_mismatch"

    paired = client.post(f"{_code_path(code)}/pair", json={"node": "pve1", "pbs_token_provided": False})
    assert paired.status_code == 200
    env = dict(line.split("=", 1) for line in paired.text.splitlines() if line and not line.startswith("#"))
    assert env["RESURGAM_BACKEND_URL"] == API_BASE
    assert env["RESURGAM_LAB_AGENT_NODE_NAME"] == "pve1"
    assert "RESURGAM_LAB_AGENT_PBS_TOKEN" not in env
    cluster = db_session.get(PveCluster, cluster_id)
    db_session.refresh(cluster)
    assert verify_appliance_run_token(env["RESURGAM_LAB_AGENT_TOKEN"], cluster.lab_agent_token_hash)
    assert cluster.lab_agent_node == "pve1"

    # Single use: everything under the code is gone now.
    assert client.post(f"{_code_path(code)}/pair", json={"node": "pve1"}).status_code == 404
    assert client.get(_script_path(created["id"])).status_code == 404
    assert client.get(f"{_code_path(code)}/agent.deb").status_code == 404

    status = client.get(f"{BASE}/{cluster_id}/lab-setup/pairings/{created['id']}", headers=platform_admin_headers)
    assert status.json()["state"] == "paired"
    assert status.json()["pbs_token_provided"] is False
    assert status.json()["code"] is None and status.json()["command"] is None

    stored = db_session.get(LabNodePairing, created["id"])
    assert stored.code_hash != code and code not in stored.script.split("API=")[0]
    audit = " ".join(str(e.detail) for e in db_session.query(AuditEvent).all())
    assert env["RESURGAM_LAB_AGENT_TOKEN"] not in audit and code not in audit


def test_script_without_appliance_ca_uses_system_trust(
    client, platform_admin_headers, db_session, artifacts, tmp_path, test_settings
):
    test_settings.appliance_tls_ca_file = str(tmp_path / "missing.crt")
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    script = client.get(_script_path(_pair(client, platform_admin_headers, cluster_id)["id"])).text
    assert "--cacert" not in script and "BEGIN CERTIFICATE" not in script
    assert subprocess.run(["sh", "-n"], input=script, text=True, check=False).returncode == 0


def test_new_code_revokes_old_and_codes_expire(client, platform_admin_headers, db_session, artifacts, ca_file):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    first = _pair(client, platform_admin_headers, cluster_id)
    second = _pair(client, platform_admin_headers, cluster_id)
    assert client.get(_script_path(first["id"])).status_code == 404
    status = client.get(f"{BASE}/{cluster_id}/lab-setup/pairings/{first['id']}", headers=platform_admin_headers)
    assert status.json()["state"] == "revoked"

    row = db_session.get(LabNodePairing, second["id"])
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    assert client.get(_script_path(second["id"])).status_code == 404
    assert client.post(f"{_code_path(second['code'])}/pair", json={"node": "pve1"}).status_code == 404


def test_unknown_code_and_admin_only_creation(client, platform_admin_headers, db_session, artifacts):
    assert client.get(_script_path(str(uuid.uuid4()))).status_code == 404
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    assert client.post(f"{BASE}/{cluster_id}/lab-setup/pairings", json={"node": "pve1"}).status_code == 401


def test_pairing_needs_the_public_backend_url(client, platform_admin_headers, db_session, artifacts, test_settings):
    test_settings.recovery_lab_public_backend_url = ""
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/pairings", json={"node": "pve1"}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "recovery_lab_backend_url_not_configured"


def test_pairing_refuses_loopback_backend_url(client, platform_admin_headers, db_session, artifacts, test_settings):
    test_settings.recovery_lab_public_backend_url = "https://127.0.0.1:8443/api/v1"
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/pairings", json={"node": "pve1"}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "recovery_lab_backend_url_loopback"
    test_settings.recovery_lab_public_backend_url = "http://localhost/api/v1"
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/pairings", json={"node": "pve1"}, headers=platform_admin_headers
    )
    assert response.json()["code"] == "recovery_lab_backend_url_loopback"


def test_pairing_checks_the_node_belongs_to_the_cluster(
    client, platform_admin_headers, db_session, artifacts, pve_nodes
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/pairings", json={"node": "pve9"}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_nodes" and "pve1" in response.json()["message"]
    assert _pair(client, platform_admin_headers, cluster_id, node="pve2")["node"] == "pve2"

    # PVE unreachable right now: the pairing is still created (the script checks the host name).
    pve_nodes.routes.clear()
    assert _pair(client, platform_admin_headers, cluster_id, node="pve9")["state"] == "waiting"


HOSTILE_NAME = "Zurich\ntouch /tmp/pwned #"


def test_cluster_display_name_rejects_control_characters(client, platform_admin_headers, db_session):
    body = {
        "display_name": HOSTILE_NAME,
        "host": "pve.lab.example",
        "token_id": "resurgam@pve!resurgam",
        "token_secret": "d63e505a-e3ec-449a-9bc7-1da610d4ccde",
        "tls_trust_mode": "ca",
    }
    assert client.post(BASE, json=body, headers=platform_admin_headers).status_code == 422
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    for name in (HOSTILE_NAME, "Lab\tPVE", "Lab\x7f"):
        response = client.patch(f"{BASE}/{cluster_id}", json={"display_name": name}, headers=platform_admin_headers)
        assert response.status_code == 422, name
    ok = client.patch(f"{BASE}/{cluster_id}", json={"display_name": "Zürich (Lab)"}, headers=platform_admin_headers)
    assert ok.status_code == 200, ok.text


def _no_injected_line(text: str) -> None:
    assert not any(line.lstrip().startswith("touch") for line in text.splitlines()), text
    assert "Zurichtouch tmppwned" in text


def test_hostile_cluster_name_never_reaches_a_shell_line(
    client, platform_admin_headers, db_session, artifacts, ca_file
):
    # A name stored before the validation existed: the files written onto the node still stay safe.
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    cluster = db_session.get(PveCluster, cluster_id)
    cluster.display_name = HOSTILE_NAME
    db_session.commit()
    created = _pair(client, platform_admin_headers, cluster_id)
    script = client.get(_script_path(created["id"])).text
    _no_injected_line(script)
    assert subprocess.run(["sh", "-n"], input=script, text=True, check=False).returncode == 0
    env = client.post(f"{_code_path(created['code'])}/pair", json={"node": "pve1"}).text
    _no_injected_line(env)
    _no_injected_line(
        render_lab_agent_env_file(
            cluster_name=HOSTILE_NAME, backend_url=API_BASE, overlay_storage_id="local", overlay_dir="/var/lib/x"
        )
    )


def test_script_reports_pair_failures_by_http_status(tmp_path):
    """Runs the real script against stub commands (curl, qm, systemctl ...)."""
    agent_deb = b"fake agent package"
    script = pairing_module.render_bootstrap_script(
        api_base=API_BASE,
        cluster_name="Lab",
        node="pve1",
        debian_release="trixie",
        agent=LabArtifact(
            name="resurgam-lab-agent",
            filename="agent.deb",
            version="0.2.7-1",
            architecture="amd64",
            sha256=hashlib.sha256(agent_deb).hexdigest(),
            size=len(agent_deb),
            path=tmp_path / "unused.deb",
        ),
        ca_certificate=None,
    ).text
    script = script.replace("/etc/os-release", str(tmp_path / "os-release"))
    script = script.replace("/etc/default/resurgam-lab-agent", str(tmp_path / "env"))
    (tmp_path / "os-release").write_text("VERSION_CODENAME=trixie\n")
    (tmp_path / "agent.bin").write_bytes(agent_deb)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "id": "echo 0",
        "qm": "true",
        "hostname": "echo pve1",
        "apt-get": "true",
        "systemctl": "true",
        "sleep": "true",
        # curl: the GET downloads the package; the POST writes $PAIR_BODY and prints $PAIR_STATUS.
        "curl": (
            'out=""; post=false; prev=""\n'
            'for a in "$@"; do [ "$prev" = -o ] && out=$a; [ "$a" = -d ] && post=true; prev=$a; done\n'
            'echo "$*" >> "$CURL_LOG"\n'
            'if $post; then printf %s "$PAIR_BODY" > "$out"; printf %s "$PAIR_STATUS"; '
            f'else cp {tmp_path / "agent.bin"} "$out"; fi'
        ),
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "pair.sh").write_text(script)

    def run(status: str, body: str, *args: str):
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "PAIR_STATUS": status,
            "PAIR_BODY": body,
            "CURL_LOG": str(tmp_path / "curl.log"),
        }
        return subprocess.run(
            ["sh", str(tmp_path / "pair.sh"), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    assert "No pairing code" in run("200", "").stderr
    assert "not a pairing code" in run("200", "", "AB;rm").stderr
    gone = run("404", '{"code":"pairing_code_invalid"}', "ABCD-EFGH")
    assert gone.returncode == 1 and "HTTP 404" in gone.stderr and "already used" in gone.stderr
    mismatch = run("409", '{"message":"This code is for node pve2"}', "ABCD-EFGH")
    assert "HTTP 409" in mismatch.stderr and "This code is for node pve2" in mismatch.stderr
    assert "HTTP 500" in run("500", "boom", "ABCD-EFGH").stderr
    calls = (tmp_path / "curl.log").read_text().splitlines()
    assert all("ABCD-EFGH" not in c or "-k" not in c.split() for c in calls)
    assert not any("--retry" in c for c in calls if " -d " in c)

    done = run("200", "RESURGAM_LAB_AGENT_TOKEN=tok\n", "ABCD-EFGH")
    assert done.returncode == 0, done.stderr
    assert "RESURGAM_LAB_AGENT_TOKEN=tok" in (tmp_path / "env").read_text()


# --- agent-run template builds ---------------------------------------------------------


def _start_agent_build(client, headers, cluster_id, **extra):
    body = {"kind": "template_build", "storage": "local-lvm", **extra}
    return client.post(f"{BASE}/{cluster_id}/lab-setup/agent-jobs", json=body, headers=headers)


def _steps(*keys: str) -> list[dict]:
    now = datetime.now(UTC).isoformat()
    return [{"key": k, "status": "ok", "message": k, "started_at": now, "finished_at": now} for k in keys]


def test_agent_build_needs_a_paired_agent(client, platform_admin_headers, db_session, artifacts, monkeypatch):
    _pve_for_template(monkeypatch)
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = _start_agent_build(client, platform_admin_headers, cluster_id)
    assert response.status_code == 409
    assert response.json()["code"] == "lab_agent_not_paired"


def test_agent_template_build_round_trip(client, platform_admin_headers, db_session, artifacts, ca_file, monkeypatch):
    _pve_for_template(monkeypatch, nextid="9010")
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    token = _paired_agent(client, platform_admin_headers, db_session, cluster_id)
    agent = {"Authorization": f"Bearer {token}"}

    started = _start_agent_build(client, platform_admin_headers, cluster_id)
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["runner"] == "agent" and job["status"] == "running" and job["node"] == "pve1"
    assert job["steps"][0]["key"] == "agent"
    assert _start_agent_build(client, platform_admin_headers, cluster_id).status_code == 409

    listed = client.get("/api/v1/lab-agent/setup-jobs", headers=agent).json()
    assert len(listed) == 1
    spec = listed[0]["spec"]
    assert spec["vmid"] == 9010 and spec["storage"] == "local-lvm" and spec["snippet_storage"] == "local"
    assert spec["uplink"]["bridge"] == "vmbr0" and spec["image_source"] == "download"
    assert client.get("/api/v1/lab-agent/setup-jobs").status_code == 401

    prober = client.get("/api/v1/lab-agent/setup-artifacts/resurgam-lab-prober", headers=agent).json()
    assert prober["sha256"] == artifacts["resurgam-lab-prober"]["sha256"]
    file = client.get("/api/v1/lab-agent/setup-artifacts/resurgam-lab-prober/file", headers=agent)
    assert hashlib.sha256(file.content).hexdigest() == prober["sha256"]
    assert client.get("/api/v1/lab-agent/setup-artifacts/resurgam-lab-agent", headers=agent).status_code == 422

    assert client.post(f"/api/v1/lab-agent/setup-jobs/{job['id']}/claim", headers=agent).status_code == 204
    assert client.post(f"/api/v1/lab-agent/setup-jobs/{job['id']}/claim", headers=agent).status_code == 409
    progress = client.post(
        f"/api/v1/lab-agent/setup-jobs/{job['id']}/progress",
        json={"steps": _steps("privileges", "preflight"), "result": {"vmid": 9010}},
        headers=agent,
    )
    assert progress.status_code == 204
    polled = client.get(f"{BASE}/{cluster_id}/lab-setup/jobs/{job['id']}", headers=platform_admin_headers).json()
    assert [s["key"] for s in polled["steps"]] == ["privileges", "preflight"]

    done = client.post(
        f"/api/v1/lab-agent/setup-jobs/{job['id']}/status",
        json={
            "status": "succeeded",
            "steps": _steps("privileges", "preflight", "finalize"),
            "result": {"template_vmid": 9010, "prober_version": "0.2.5-1"},
        },
        headers=agent,
    )
    assert done.status_code == 204
    cluster = db_session.get(PveCluster, cluster_id)
    db_session.refresh(cluster)
    assert cluster.lab_appliance_template_vmid == 9010
    polled = client.get(f"{BASE}/{cluster_id}/lab-setup/jobs/{job['id']}", headers=platform_admin_headers).json()
    assert polled["status"] == "succeeded" and polled["result"]["prober_version"] == "0.2.5-1"
    assert client.get("/api/v1/lab-agent/setup-jobs", headers=agent).json() == []


def test_agent_share_helper_build_and_vmid_mismatch(
    client, platform_admin_headers, db_session, artifacts, ca_file, monkeypatch
):
    _pve_for_template(monkeypatch, nextid="9020")
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, is_lab_compute=True)
    token = _paired_agent(client, platform_admin_headers, db_session, cluster_id)
    agent = {"Authorization": f"Bearer {token}"}
    job = _start_agent_build(client, platform_admin_headers, cluster_id, kind="share_helper_template_build").json()
    client.post(f"/api/v1/lab-agent/setup-jobs/{job['id']}/claim", headers=agent)
    client.post(
        f"/api/v1/lab-agent/setup-jobs/{job['id']}/status",
        json={"status": "succeeded", "steps": [], "result": {"template_vmid": 1234}},
        headers=agent,
    )
    polled = client.get(f"{BASE}/{cluster_id}/lab-setup/jobs/{job['id']}", headers=platform_admin_headers).json()
    assert polled["status"] == "failed" and polled["error_code"] == "template_vmid_mismatch"
    cluster = db_session.get(PveCluster, cluster_id)
    db_session.refresh(cluster)
    assert cluster.disk_mount_helper_template_vmid is None


def test_agent_build_static_uplink_allocates_and_releases(
    client, platform_admin_headers, db_session, artifacts, ca_file, monkeypatch
):
    _pve_for_template(monkeypatch)
    cluster_id = _create_cluster(
        client,
        platform_admin_headers,
        db_session,
        lab_appliance_uplink_ip_mode="static_range",
        lab_appliance_uplink_ip_range_start="10.0.0.10",
        lab_appliance_uplink_ip_range_end="10.0.0.20",
        lab_appliance_uplink_ip_prefix_length=24,
        lab_appliance_uplink_gateway="10.0.0.1",
        lab_appliance_uplink_vlan_tag=20,
        lab_appliance_uplink_dns_servers=["10.0.0.2"],
    )
    token = _paired_agent(client, platform_admin_headers, db_session, cluster_id)
    agent = {"Authorization": f"Bearer {token}"}
    started = _start_agent_build(client, platform_admin_headers, cluster_id)
    assert started.status_code == 202, started.text
    job = started.json()
    spec = client.get("/api/v1/lab-agent/setup-jobs", headers=agent).json()[0]["spec"]
    assert spec["uplink_address"] == "10.0.0.10" and spec["uplink"]["vlan_tag"] == 20
    assert db_session.query(LabUplinkIpAllocation).filter_by(pve_cluster_id=cluster_id).count() == 1
    client.post(f"/api/v1/lab-agent/setup-jobs/{job['id']}/claim", headers=agent)
    client.post(
        f"/api/v1/lab-agent/setup-jobs/{job['id']}/status",
        json={"status": "failed", "steps": [], "result": {}, "error_code": "build_timeout", "error_message": "x"},
        headers=agent,
    )
    assert db_session.query(LabUplinkIpAllocation).filter_by(pve_cluster_id=cluster_id).count() == 0


def test_agent_job_blocks_ssh_guided_jobs(client, platform_admin_headers, db_session, artifacts, ca_file, monkeypatch):
    _pve_for_template(monkeypatch)
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, is_lab_compute=True)
    _paired_agent(client, platform_admin_headers, db_session, cluster_id)
    job = _start_agent_build(client, platform_admin_headers, cluster_id)
    assert job.status_code == 202, job.text
    fake = FakeSshBackend()
    fake.install(monkeypatch)
    for path, body in (
        ("template-build-jobs", {"ssh": _ssh(), "storage": "local-lvm"}),
        ("share-helper-template-build-jobs", {"ssh": _ssh(), "storage": "local-lvm"}),
        ("agent-install-jobs", {"ssh": _ssh()}),
    ):
        response = client.post(f"{BASE}/{cluster_id}/lab-setup/{path}", json=body, headers=platform_admin_headers)
        assert response.status_code == 409, (path, response.text)
        assert response.json()["details"]["job_id"] == job.json()["id"]
    assert fake.sessions == []  # nothing was sent to the node


def test_stale_check_does_not_overwrite_a_just_claimed_job(
    client, platform_admin_headers, db_session, artifacts, ca_file, monkeypatch
):
    _pve_for_template(monkeypatch)
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    _paired_agent(client, platform_admin_headers, db_session, cluster_id)
    job_id = _start_agent_build(client, platform_admin_headers, cluster_id).json()["id"]
    job = db_session.get(LabAgentSetupJob, job_id)
    job.created_at = datetime.now(UTC) - timedelta(minutes=16)
    db_session.commit()
    db_session.refresh(job)
    # The agent claims it meanwhile (written past this session's loaded copy).
    now = datetime.now(UTC)
    db_session.execute(
        update(LabAgentSetupJob)
        .where(LabAgentSetupJob.id == job.id)
        .values(status="running", claimed_at=now, last_progress_at=now)
        .execution_options(synchronize_session=False)
    )
    assert job.status == "queued"  # the stale in-memory copy
    _expire_if_stale(db_session, job)
    assert job.status == "running" and job.error_code is None


def test_unclaimed_and_silent_agent_jobs_fail(
    client, platform_admin_headers, db_session, artifacts, ca_file, monkeypatch
):
    _pve_for_template(monkeypatch)
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    _paired_agent(client, platform_admin_headers, db_session, cluster_id)
    job = _start_agent_build(client, platform_admin_headers, cluster_id).json()
    row = db_session.get(LabAgentSetupJob, job["id"])
    row.created_at = datetime.now(UTC) - timedelta(minutes=16)
    db_session.commit()
    polled = client.get(f"{BASE}/{cluster_id}/lab-setup/jobs/{job['id']}", headers=platform_admin_headers).json()
    assert polled["status"] == "failed" and polled["error_code"] == "agent_not_responding"

    job = _start_agent_build(client, platform_admin_headers, cluster_id).json()
    row = db_session.get(LabAgentSetupJob, job["id"])
    row.status = "running"
    row.claimed_at = row.last_progress_at = datetime.now(UTC) - timedelta(minutes=11)
    db_session.commit()
    polled = client.get(f"{BASE}/{cluster_id}/lab-setup/jobs/{job['id']}", headers=platform_admin_headers).json()
    assert polled["error_code"] == "agent_stopped_reporting"
