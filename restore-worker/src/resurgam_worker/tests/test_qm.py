"""`qm.py` wraps local `qm`/`qemu-img` commands for the Recovery Lab
probe/member VM lifecycle -- `create_vm`/`start_vm`/`stop_vm`/
`destroy_vm`/`create_cow_overlay` all confirmed live (2026-09-19)
against a real PVE 9.2 node (see ADR 0019): a VM built from scratch
around a `proxmox-backup-client map`-produced block device, both
read-only and via a writable COW overlay, booted successfully.
"""

import subprocess

import pytest

from resurgam_worker.pve_client import qm
from resurgam_worker.pve_client.errors import PveBinaryNotFoundError, PveCommandFailedError


def _fake_completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fake_which(monkeypatch):
    monkeypatch.setattr(qm.shutil, "which", lambda name: f"/usr/sbin/{name}")


def test_create_vm_sends_every_config_key_as_a_flag(monkeypatch):
    captured = {}

    def fake_run(command):
        captured["command"] = command
        return _fake_completed(0)

    monkeypatch.setattr(qm, "_run", fake_run)

    qm.create_vm(
        115,
        {
            "name": "lab-appliance-abcd1234",
            "memory": "512",
            "cores": "1",
            "net0": "model=virtio,bridge=labe94e9",
            "scsihw": "virtio-scsi-pci",
            "scsi0": "/dev/loop0,ro=1",
            "agent": "1",
        },
        pool="resurgam-lab",
    )

    assert captured["command"] == [
        "/usr/sbin/qm",
        "create",
        "115",
        "--name",
        "lab-appliance-abcd1234",
        "--memory",
        "512",
        "--cores",
        "1",
        "--net0",
        "model=virtio,bridge=labe94e9",
        "--scsihw",
        "virtio-scsi-pci",
        "--scsi0",
        "/dev/loop0,ro=1",
        "--agent",
        "1",
        "--pool",
        "resurgam-lab",
    ]


def test_create_vm_omits_pool_when_not_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(qm, "_run", lambda command: (captured.update(command=command), _fake_completed(0))[1])

    qm.create_vm(115, {"net0": "model=virtio,bridge=labe94e9", "scsi0": "/dev/loop0"})

    assert "--pool" not in captured["command"]


def test_create_vm_raises_on_failure(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(1, stderr="already exists"))

    with pytest.raises(PveCommandFailedError):
        qm.create_vm(115, {"net0": "net", "scsi0": "dev"})


def test_start_vm_calls_real_command_shape(monkeypatch):
    captured = {}
    monkeypatch.setattr(qm, "_run", lambda command: (captured.update(command=command), _fake_completed(0))[1])

    qm.start_vm(115)

    assert captured["command"] == ["/usr/sbin/qm", "start", "115"]


def test_stop_vm_calls_real_command_shape(monkeypatch):
    captured = {}
    monkeypatch.setattr(qm, "_run", lambda command: (captured.update(command=command), _fake_completed(0))[1])

    qm.stop_vm(115)

    assert captured["command"] == ["/usr/sbin/qm", "stop", "115"]


def test_destroy_vm_includes_purge_by_default(monkeypatch):
    captured = {}
    monkeypatch.setattr(qm, "_run", lambda command: (captured.update(command=command), _fake_completed(0))[1])

    qm.destroy_vm(115)

    assert captured["command"] == ["/usr/sbin/qm", "destroy", "115", "--purge"]


def test_destroy_vm_without_purge(monkeypatch):
    captured = {}
    monkeypatch.setattr(qm, "_run", lambda command: (captured.update(command=command), _fake_completed(0))[1])

    qm.destroy_vm(115, purge=False)

    assert captured["command"] == ["/usr/sbin/qm", "destroy", "115"]


def test_create_cow_overlay_sends_expected_qemu_img_shape(monkeypatch):
    captured = {}
    monkeypatch.setattr(qm, "_run", lambda command: (captured.update(command=command), _fake_completed(0))[1])

    qm.create_cow_overlay("/dev/loop0", "/var/lib/resurgam-lab-agent/overlays/abc.qcow2")

    assert captured["command"] == [
        "/usr/sbin/qemu-img",
        "create",
        "-f",
        "qcow2",
        "-b",
        "/dev/loop0",
        "-F",
        "raw",
        "/var/lib/resurgam-lab-agent/overlays/abc.qcow2",
    ]


def test_missing_binary_raises_clearly(monkeypatch):
    monkeypatch.setattr(qm.shutil, "which", lambda name: None)

    with pytest.raises(PveBinaryNotFoundError):
        qm.start_vm(115)


# --- guest_exec ------------------------------------------------------------------
#
# A real, decisive gap found live (2026-09-25): `qm guest exec`'s own
# default (`--synchronous 1 --timeout 30`) only blocks for 30s -- past
# that it returns just `{"pid": N}` while the guest-side command keeps
# running *unsupervised* in the background. A real full-drive
# enumeration on a real production VM hit exactly this: the caller saw
# an apparently-successful call with no output yet, and a second
# trigger for the same VM raced a second, independent background
# process against the first, corrupting both runs' output. `guest_exec`
# now launches with `--synchronous 0` explicitly and polls
# `exec-status` itself instead of trusting `qm`'s own wait.


def test_guest_exec_launches_synchronous_zero_then_polls_status(monkeypatch):
    commands = []

    def fake_run_subprocess(command, **kw):
        commands.append(command)
        return _fake_completed(0, stdout='{"pid": 4242}')

    def fake_run(command):
        commands.append(command)
        return _fake_completed(0, stdout='{"exitcode": 0, "exited": 1, "out-data": "hello\\n"}')

    monkeypatch.setattr(qm, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(qm, "_run", fake_run)
    monkeypatch.setattr(qm.time, "sleep", lambda s: None)

    result = qm.guest_exec(114, ["echo", "hello"])

    assert commands[0] == ["/usr/sbin/qm", "guest", "exec", "114", "--synchronous", "0", "--", "echo", "hello"]
    assert commands[1] == ["/usr/sbin/qm", "guest", "exec-status", "114", "4242"]
    assert result == {"exitcode": 0, "exited": 1, "out-data": "hello\n"}


def test_guest_exec_polls_multiple_times_until_exited(monkeypatch):
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: _fake_completed(0, stdout='{"pid": 1}'))
    statuses = iter(
        [
            _fake_completed(0, stdout='{"exited": 0}'),
            _fake_completed(0, stdout='{"exited": 0}'),
            _fake_completed(0, stdout='{"exitcode": 0, "exited": 1, "out-data": "done\\n"}'),
        ]
    )
    monkeypatch.setattr(qm, "_run", lambda command: next(statuses))
    sleeps = []
    monkeypatch.setattr(qm.time, "sleep", lambda s: sleeps.append(s))

    result = qm.guest_exec(114, ["echo", "hi"], poll_interval_seconds=2.0)

    assert result["out-data"] == "done\n"
    assert sleeps == [2.0, 2.0]


def test_guest_exec_raises_on_launch_failure(monkeypatch):
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: _fake_completed(1, stderr="no such vmid"))

    with pytest.raises(PveCommandFailedError):
        qm.guest_exec(999, ["echo", "hi"])


def test_guest_exec_raises_on_unparseable_launch_output(monkeypatch):
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: _fake_completed(0, stdout="not json"))

    with pytest.raises(qm.PveOutputParseError):
        qm.guest_exec(114, ["echo", "hi"])


def test_guest_exec_tolerates_transient_poll_failure_then_succeeds(monkeypatch):
    """A real gap found live (2026-09-25): an isolated `exec-status`
    poll failure ("got timeout", confirmed on a real, actively-used
    production VM) must not kill the whole job -- only a poll that's
    still failing once the overall deadline has passed should.
    """
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: _fake_completed(0, stdout='{"pid": 1}'))
    responses = iter(
        [
            _fake_completed(1, stderr="got timeout"),
            _fake_completed(0, stdout='{"exitcode": 0, "exited": 1, "out-data": "ok\\n"}'),
        ]
    )
    monkeypatch.setattr(qm, "_run", lambda command: next(responses))
    monkeypatch.setattr(qm.time, "sleep", lambda s: None)

    result = qm.guest_exec(114, ["echo", "hi"])

    assert result["out-data"] == "ok\n"


def test_guest_exec_raises_last_poll_error_once_deadline_passes(monkeypatch):
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: _fake_completed(0, stdout='{"pid": 1}'))
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(1, stderr="got timeout"))
    monkeypatch.setattr(qm.time, "sleep", lambda s: None)
    # deadline call, then two "still within budget" checks, then one past it
    clock = iter([0.0, 0.0, 0.0, 5.0])
    monkeypatch.setattr(qm.time, "monotonic", lambda: next(clock))

    with pytest.raises(PveCommandFailedError):
        qm.guest_exec(114, ["echo", "hi"], timeout_seconds=1.0)


def test_guest_exec_raises_timeout_error_when_never_exits(monkeypatch):
    from resurgam_worker.pve_client.errors import GuestExecTimeoutError

    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: _fake_completed(0, stdout='{"pid": 1}'))
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(0, stdout='{"exited": 0}'))
    monkeypatch.setattr(qm.time, "sleep", lambda s: None)
    # Force the deadline to already be in the past on the very first check.
    monkeypatch.setattr(qm.time, "monotonic", lambda: 0.0)

    with pytest.raises(GuestExecTimeoutError):
        qm.guest_exec(114, ["echo", "hi"], timeout_seconds=-1.0)


# --- guest_file_read_all ---------------------------------------------------------
#
# A real, decisive correction found live (2026-09-24): `qm guest cmd`
# does not expose QGA's raw guest-file-open/-read/-close at all (its own
# real command whitelist has no file-* commands) -- every real call
# failed "400 too many arguments". The actual mechanism is PVE's own
# REST API (via the local `pvesh` CLI), which wraps the whole
# open/read-loop/close sequence into one `file-read` call and returns
# already-decoded content directly.


def test_guest_file_read_all_sends_expected_command_and_parses_content(monkeypatch):
    captured = {}

    def fake_run(command):
        captured["command"] = command
        return _fake_completed(0, stdout='{"bytes-read": 11, "content": "hello world"}')

    monkeypatch.setattr(qm, "_run", fake_run)

    result = qm.guest_file_read_all(114, "/tmp/resurgam-index.tsv", node="pve1")

    assert result == "hello world"
    assert captured["command"] == [
        "/usr/sbin/pvesh",
        "get",
        "/nodes/pve1/qemu/114/agent/file-read",
        "--file",
        "/tmp/resurgam-index.tsv",
        "--output-format",
        "json",
    ]


def test_guest_file_read_all_raises_on_command_failure(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(1, stderr="400 too many arguments"))

    with pytest.raises(PveCommandFailedError):
        qm.guest_file_read_all(114, "/tmp/x", node="pve1", retries=1)


def test_guest_file_read_all_retries_on_transient_failure_then_succeeds(monkeypatch):
    """A real, live-found transient case (2026-09-24): Windows can still
    hold the just-written enumeration output locked for a moment after
    guest_exec's own command already exited (Defender's real-time scan).
    """
    attempts = {"count": 0}

    def fake_run(command):
        attempts["count"] += 1
        if attempts["count"] < 2:
            return _fake_completed(255, stderr="Agent error: failed to open file: being used by another process")
        return _fake_completed(0, stdout='{"bytes-read": 5, "content": "hello"}')

    monkeypatch.setattr(qm, "_run", fake_run)
    monkeypatch.setattr(qm.time, "sleep", lambda seconds: None)

    result = qm.guest_file_read_all(114, "/tmp/x", node="pve1", retries=3)

    assert result == "hello"
    assert attempts["count"] == 2


def test_guest_file_read_all_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(255, stderr="still locked"))
    monkeypatch.setattr(qm.time, "sleep", lambda seconds: None)
    calls = []
    monkeypatch.setattr(qm, "_run", lambda command: (calls.append(1), _fake_completed(255, stderr="still locked"))[1])

    with pytest.raises(PveCommandFailedError):
        qm.guest_file_read_all(114, "/tmp/x", node="pve1", retries=3)

    assert len(calls) == 3


def test_guest_file_read_all_raises_on_unparseable_json(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(0, stdout="not json"))

    with pytest.raises(qm.PveOutputParseError):
        qm.guest_file_read_all(114, "/tmp/x", node="pve1")


def test_guest_file_read_all_raises_when_no_content_field(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(0, stdout='{"bytes-read": 0}'))

    with pytest.raises(qm.PveOutputParseError):
        qm.guest_file_read_all(114, "/tmp/x", node="pve1")


# --- get_vm_config (ADR 0021 Sec.4) ---------------------------------------------


def test_get_vm_config_parses_key_value_lines(monkeypatch):
    monkeypatch.setattr(
        qm,
        "_run",
        lambda command: _fake_completed(0, stdout="agent: 1\nostype: l26\nnet0: virtio=AA:BB,bridge=vmbr0\n"),
    )

    config = qm.get_vm_config(114)

    assert config == {"agent": "1", "ostype": "l26", "net0": "virtio=AA:BB,bridge=vmbr0"}


def test_get_vm_config_sends_expected_command(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        qm, "_run", lambda command: (captured.update(command=command), _fake_completed(0, stdout="ostype: l26\n"))[1]
    )

    qm.get_vm_config(114)

    assert captured["command"] == ["/usr/sbin/qm", "config", "114"]


def test_get_vm_config_skips_blank_and_comment_lines(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(0, stdout="\n# a comment\nostype: l26\n"))

    config = qm.get_vm_config(114)

    assert config == {"ostype": "l26"}


def test_get_vm_config_raises_on_command_failure(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(1, stderr="no such vmid"))

    with pytest.raises(PveCommandFailedError):
        qm.get_vm_config(999)


def test_get_vm_config_raises_on_no_parseable_keys(monkeypatch):
    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(0, stdout="not a config line at all"))

    with pytest.raises(qm.PveOutputParseError):
        qm.get_vm_config(114)


# --- wait_for_guest_agent ---------------------------------------------------------


def test_guest_ping_uses_pves_ping_command_and_its_exit_status(monkeypatch):
    # Live finding 2026-11-21: PVE rejects `guest-ping`; its command is `ping`,
    # which prints nothing on success.
    commands = []
    monkeypatch.setattr(qm, "_run", lambda command: commands.append(command) or _fake_completed(0, ""))

    assert qm.guest_ping(114) is True
    assert commands == [["/usr/sbin/qm", "guest", "cmd", "114", "ping"]]

    monkeypatch.setattr(qm, "_run", lambda command: _fake_completed(255, "", "QEMU guest agent is not running"))
    assert qm.guest_ping(114) is False


def test_wait_for_guest_agent_returns_immediately_when_ready(monkeypatch):
    calls = []
    monkeypatch.setattr(qm, "guest_ping", lambda vmid: calls.append(vmid) or True)

    qm.wait_for_guest_agent(114, timeout_seconds=5, poll_interval_seconds=0.01)

    assert calls == [114]


def test_wait_for_guest_agent_retries_until_ready(monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(qm, "guest_ping", lambda vmid: next(answers))

    qm.wait_for_guest_agent(114, timeout_seconds=5, poll_interval_seconds=0.001)


def test_wait_for_guest_agent_raises_timeout_error(monkeypatch):
    monkeypatch.setattr(qm, "guest_ping", lambda vmid: False)

    with pytest.raises(qm.GuestAgentTimeoutError):
        qm.wait_for_guest_agent(114, timeout_seconds=0.02, poll_interval_seconds=0.01)
