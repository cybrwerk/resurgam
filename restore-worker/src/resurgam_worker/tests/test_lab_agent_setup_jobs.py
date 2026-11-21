"""The agent's template builds (ADR 0027): spec validation, the local shell,
and the reported step log -- the build itself is the shared lab_build code,
covered by the backend's SSH tests; here it is replaced by a fake."""

import threading
import time

import httpx
import pytest

from resurgam_worker.lab_agent import setup_jobs
from resurgam_worker.lab_agent.client import LabAgentApiError, SetupJob
from resurgam_worker.lab_agent.config import LabAgentConfig, load_config
from resurgam_worker.lab_build.errors import StepFailed

CONFIG = LabAgentConfig(backend_url="https://r.example/api/v1", agent_token="t", node_name="pve1")


def _spec(**overrides) -> dict:
    spec = {
        "vmid": 9010,
        "storage": "local-lvm",
        "snippet_storage": "local",
        "image_source": "download",
        "node_image": None,
        "uplink": {"bridge": "vmbr0", "vlan_tag": None, "ip_mode": "dhcp"},
        "uplink_address": None,
        "release_version": "0.2.5",
    }
    spec.update(overrides)
    return spec


class FakeClient:
    def __init__(self):
        self.claimed = []
        self.progress = []
        self.final = None

    def claim_setup_job(self, job_id):
        self.claimed.append(job_id)

    def report_setup_progress(self, job_id, steps, result):
        self.progress.append([s["key"] for s in steps])

    def report_setup_status(self, job_id, *, steps, result, **kwargs):
        self.final = {"steps": steps, "result": result, **kwargs}


def test_parse_common_accepts_dhcp_and_static_uplinks():
    common = setup_jobs.parse_common(_spec())
    assert common.vmid == 9010 and common.uplink.bridge == "vmbr0" and not common.uplink.is_static
    static = setup_jobs.parse_common(
        _spec(
            uplink={
                "bridge": "vmbr1",
                "vlan_tag": 20,
                "ip_mode": "static_range",
                "range_start": "10.0.0.10",
                "range_end": "10.0.0.20",
                "prefix_length": 24,
                "gateway": "10.0.0.1",
                "dns_servers": ["10.0.0.2"],
            },
            uplink_address="10.0.0.10",
        )
    )
    assert static.uplink.is_static and static.uplink_address == "10.0.0.10" and static.uplink.vlan_tag == 20


@pytest.mark.parametrize(
    "overrides",
    [
        {"vmid": "9010; rm -rf /"},
        {"vmid": 5},
        {"storage": "local; reboot"},
        {"snippet_storage": "$(id)"},
        {"image_source": "anything"},
        {"image_source": "node_image", "node_image": "relative/path.qcow2"},
        {"uplink": {"bridge": "vmbr0 && reboot"}},
        {"uplink": {"bridge": "vmbr0", "vlan_tag": 5000}},
        {"uplink": {"bridge": "vmbr0", "ip_mode": "static_range", "range_start": "x"}},
        {"uplink_address": "not-an-ip"},
        {"release_version": "1.0 && reboot"},
    ],
)
def test_parse_common_rejects_anything_malformed(overrides):
    with pytest.raises(StepFailed) as excinfo:
        setup_jobs.parse_common(_spec(**overrides))
    assert excinfo.value.code == "invalid_spec"


def test_local_shell_runs_and_times_out(tmp_path):
    shell = setup_jobs.LocalShell()
    result = shell.run("echo hello; echo oops >&2; exit 3")
    assert result.exit_status == 3 and result.stdout.strip() == "hello" and "oops" in result.combined()
    assert shell.run("sleep 5", timeout=0.2).exit_status == 124
    target = tmp_path / "f"
    shell.session.put_bytes(b"data", str(target), mode=0o600)
    assert target.read_bytes() == b"data" and (target.stat().st_mode & 0o777) == 0o600


def test_job_for_another_node_is_refused():
    client = FakeClient()
    job = SetupJob(id="j1", kind="share_helper_template_build", node="pve2", spec=_spec())
    setup_jobs.process_setup_job(job, CONFIG, client)
    assert client.final["status"] == "failed" and client.final["error_code"] == "wrong_node"


def test_share_helper_job_runs_the_shared_build_and_reports(monkeypatch):
    seen = {}

    def fake_build(shell, log, spec, *, commit_template):
        seen["spec"] = spec
        seen["privilege_mode"] = shell.privilege_mode
        step = log.start("create", "Creating VM")
        log.finish(step, "ok")
        log.set_result(template_vmid=spec.vmid, template_version="1")

    monkeypatch.setattr(setup_jobs, "run_share_helper_template_build", fake_build)
    client = FakeClient()
    job = SetupJob(id="j1", kind="share_helper_template_build", node="pve1", spec=_spec())
    setup_jobs.process_setup_job(job, CONFIG, client)
    assert client.claimed == ["j1"]
    assert seen["spec"].vmid == 9010 and seen["spec"].storage == "local-lvm" and seen["privilege_mode"] == "root"
    assert client.final["status"] == "succeeded" and client.final["result"]["template_vmid"] == 9010
    assert client.progress[-1] == ["create"]


def test_build_failure_marks_the_running_step_failed(monkeypatch):
    def failing_build(shell, log, spec, *, commit_template):
        log.start("first_boot", "Starting the build VM")
        raise StepFailed("build_timeout", "No result within 30 minutes.", "console tail")

    monkeypatch.setattr(setup_jobs, "run_share_helper_template_build", failing_build)
    client = FakeClient()
    setup_jobs.process_setup_job(
        SetupJob(id="j1", kind="share_helper_template_build", node="pve1", spec=_spec()), CONFIG, client
    )
    assert client.final["status"] == "failed" and client.final["error_code"] == "build_timeout"
    step = client.final["steps"][-1]
    assert step["status"] == "failed" and step["output"] == "console tail"


def test_config_pbs_token_and_ca_file_are_optional(monkeypatch):
    monkeypatch.setenv("RESURGAM_BACKEND_URL", "https://r.example/api/v1/")
    monkeypatch.setenv("RESURGAM_LAB_AGENT_TOKEN", "tok")
    monkeypatch.delenv("RESURGAM_LAB_AGENT_PBS_TOKEN", raising=False)
    monkeypatch.setenv("RESURGAM_LAB_AGENT_CA_FILE", "/etc/resurgam-lab-agent/appliance-ca.pem")
    config = load_config()
    assert config.pbs_token == "" and config.ca_file == "/etc/resurgam-lab-agent/appliance-ca.pem"


# --- robustness against an unreachable backend ----------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"storage": "local-lvm\n"},
        {"release_version": "0.2.5\n"},
        {"image_source": "node_image", "node_image": "/var/lib/vz/images/x.qcow2\n"},
        {"image_source": "node_image", "node_image": "/var/lib/vz/../../etc/shadow"},
        {"image_source": "node_image", "node_image": "local:iso/x.img\n"},
    ],
)
def test_parse_common_rejects_trailing_newlines_and_parent_segments(overrides):
    with pytest.raises(StepFailed) as excinfo:
        setup_jobs.parse_common(_spec(**overrides))
    assert excinfo.value.code == "invalid_spec"


def test_parse_common_still_accepts_node_image_paths_and_volume_ids():
    for image in ("/var/lib/vz/template/iso/debian.qcow2", "local:iso/debian-12.img"):
        assert setup_jobs.parse_common(_spec(image_source="node_image", node_image=image)).node_image == image


class FlakyProgressClient(FakeClient):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def report_setup_progress(self, job_id, steps, result):
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), LabAgentApiError(0, "ReadTimeout: timed out"), RuntimeError("anything")],
)
def test_progress_failures_never_fail_the_build(monkeypatch, exc):
    def fake_build(shell, log, spec, *, commit_template):
        log.finish(log.start("create", "Creating VM"), "ok")

    monkeypatch.setattr(setup_jobs, "run_share_helper_template_build", fake_build)
    client = FlakyProgressClient(exc)
    setup_jobs.process_setup_job(
        SetupJob(id="j1", kind="share_helper_template_build", node="pve1", spec=_spec()), CONFIG, client
    )
    assert client.final["status"] == "succeeded"


def test_heartbeat_survives_send_errors(monkeypatch):
    monkeypatch.setattr(setup_jobs, "HEARTBEAT_SECONDS", 0.01)
    attempts = []

    class Client:
        def report_setup_progress(self, job_id, steps, result):
            attempts.append(1)
            raise httpx.ReadTimeout("timed out")

    log = setup_jobs.ReportingLog(Client(), "j1")
    stop = threading.Event()
    beat = threading.Thread(target=log.heartbeat, args=(stop,), daemon=True)
    beat.start()
    deadline = time.monotonic() + 2
    while len(attempts) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    beat.join(1)
    assert len(attempts) >= 3


def test_final_status_is_retried_through_transport_errors(monkeypatch):
    monkeypatch.setattr(setup_jobs.time, "sleep", lambda s: None)
    calls = []

    class Client:
        def report_setup_status(self, job_id, *, steps, result, **kwargs):
            calls.append(kwargs)
            if len(calls) < 4:
                raise LabAgentApiError(0, "ConnectError: refused") if len(calls) % 2 else LabAgentApiError(503, "")

    log = setup_jobs.ReportingLog(Client(), "j1")
    setup_jobs._report_final(Client(), "j1", log, status="succeeded")
    assert len(calls) == 4


def test_final_status_is_not_retried_when_the_backend_rejects_it(monkeypatch):
    monkeypatch.setattr(setup_jobs.time, "sleep", lambda s: None)
    calls = []

    class Client:
        def report_setup_status(self, job_id, *, steps, result, **kwargs):
            calls.append(1)
            raise LabAgentApiError(409, "not running")

    setup_jobs._report_final(Client(), "j1", setup_jobs.ReportingLog(Client(), "j1"), status="succeeded")
    assert calls == [1]
