"""resurgam-lab-agent's Disk Mounts work (ADR 0025): provisioning order and
payloads (map read-only, attach ro + serial, password only via stdin),
cleanup on failure, and the teardown guardrails."""

import logging
import subprocess

import pytest

from resurgam_worker.lab_agent import disk_mount
from resurgam_worker.lab_agent.client import DiskMountClaim, DiskMountJob, LabAgentApiError
from resurgam_worker.lab_agent.config import LabAgentConfig
from resurgam_worker.pbs_client import map as pbs_map
from resurgam_worker.pbs_client.models import MappedImage
from resurgam_worker.pve_client import qm
from resurgam_worker.pve_client.errors import PveCommandFailedError

PASSWORD = "Gx7-very-secret-pw-QZ01"
MOUNT_ID = "0b6f1c2e-0000-4000-8000-000000000001"
SNAPSHOT = "vm/116/2026-09-14T00:48:57Z"

CONFIG = LabAgentConfig(
    backend_url="https://resurgam.example/api/v1",
    agent_token="agent-token",
    pbs_token="resurgam@pbs!lab:pbs-secret",
    node_name="pve1",
    disk_mount_ready_timeout_seconds=30,
)


def _claim(**overrides) -> DiskMountClaim:
    data = {
        "id": MOUNT_ID,
        "target_node": "pve1",
        "target_storage": "local-lvm",
        "helper_name": "rsgm-mount-0b6f1c2e",
        "helper_template_vmid": 9100,
        "pbs_host": "pbs.example",
        "pbs_port": 8007,
        "pbs_datastore": "store1",
        "pbs_namespace": "Kunde-A",
        "snapshot_path": SNAPSHOT,
        "disks": [
            {"archive": "drive-scsi0.img.fidx", "serial": "rsgm1", "slot": "scsi1"},
            {"archive": "drive-scsi1.img.fidx", "serial": "rsgm2", "slot": "scsi2"},
        ],
        "net0": "model=virtio,bridge=vmbr0,tag=20",
        "ipconfig0": "ip=192.168.20.50/24,gw=192.168.20.1",
        "snippet_storage": "local",
        "snippet_filename": "resurgam-disk-mount-0b6f1c2e0000.yaml",
        "user_data": "#cloud-config\n",
        "smb_username": "rsgm-1a2b3c4d",
        "share_password": PASSWORD,
        "encryption_keyfile": None,
    }
    data.update(overrides)
    return DiskMountClaim.model_validate(data)


def _job(kind="provision", **overrides) -> DiskMountJob:
    data = {
        "id": MOUNT_ID,
        "kind": kind,
        "target_node": "pve1",
        "helper_name": "rsgm-mount-0b6f1c2e",
        "snapshot_path": SNAPSHOT,
    }
    data.update(overrides)
    return DiskMountJob.model_validate(data)


class FakeClient:
    def __init__(self, claim=None):
        self.claim = claim or _claim()
        self.calls: list[tuple] = []

    def claim_disk_mount_job(self, mount_id):
        self.calls.append(("claim", mount_id))
        return self.claim

    def report_disk_mount_progress(self, mount_id, **kw):
        self.calls.append(("progress", kw))

    def report_disk_mount_status(self, mount_id, status, **kw):
        self.calls.append(("status", status, kw))

    def report_disk_mount_teardown(self, mount_id, status, **kw):
        self.calls.append(("teardown", status, kw))


class FakeNode:
    """Stands in for proxmox-backup-client / qm / pvesh / pvesm on the node."""

    def __init__(self, tmp_path, *, helper_result=None, vm_name="rsgm-mount-0b6f1c2e"):
        self.tmp_path = tmp_path
        self.ops: list[tuple] = []
        self.next_loop = 0
        self.vms: dict[int, dict] = {}
        self.helper_result = helper_result or {
            "status": "ok",
            "addresses": ["192.168.20.50"],
            "volumes": [{"disk_index": 1, "archive": "drive-scsi0.img.fidx", "partition": "part1", "mounted": True}],
        }
        self.vm_name = vm_name
        self.linked_clone_fails = False
        self.mappings: dict[str, str] = {}

    def install(self, monkeypatch):
        m = disk_mount
        monkeypatch.setattr(m, "list_snapshot_archives", self.list_snapshot_archives)
        monkeypatch.setattr(m, "map_snapshot", self.map_snapshot)
        monkeypatch.setattr(m, "unmap", self.unmap)
        monkeypatch.setattr(m, "list_mappings", lambda: dict(self.mappings))
        monkeypatch.setattr(m, "next_free_vmid", lambda: 4242)
        monkeypatch.setattr(m, "list_cluster_vmids", lambda: set(self.vms))
        monkeypatch.setattr(m, "clone_vm", self.clone_vm)
        monkeypatch.setattr(m, "set_vm_options", self.set_vm_options)
        monkeypatch.setattr(m, "delete_vm_options", self.delete_vm_options)
        monkeypatch.setattr(m, "get_vm_config", lambda vmid: dict(self.vms[vmid]))
        monkeypatch.setattr(m, "start_vm", lambda vmid: self.ops.append(("start", vmid)))
        monkeypatch.setattr(m, "stop_vm", lambda vmid: self.ops.append(("stop", vmid)))
        monkeypatch.setattr(m, "destroy_vm", self.destroy_vm)
        monkeypatch.setattr(m, "wait_for_guest_agent", lambda vmid, timeout_seconds: self.ops.append(("agent", vmid)))
        monkeypatch.setattr(m, "_read_helper_result", lambda vmid: self.helper_result)
        monkeypatch.setattr(m, "guest_exec_with_stdin", self.guest_exec_with_stdin)
        monkeypatch.setattr(m, "resolve_storage_path", lambda volume: str(self.tmp_path / volume.split("/")[-1]))
        return self

    def list_snapshot_archives(self, repository, snapshot, *, namespace, token, keyfile):
        self.ops.append(("list", repository, snapshot, namespace))
        return ["drive-scsi0.img.fidx", "drive-scsi1.img.fidx"]

    def map_snapshot(self, repository, snapshot, archive, *, namespace, token, keyfile):
        device = f"/dev/loop{self.next_loop}"
        self.next_loop += 1
        self.mappings[device] = f"{repository}:{snapshot}/{archive.removesuffix('.fidx')}"
        self.ops.append(("map", archive, device))
        return MappedImage(device_path=device)

    def unmap(self, device, token=None):
        self.ops.append(("unmap", device))
        self.mappings.pop(device, None)

    def clone_vm(self, source, target, *, name, description=None, full=False, storage=None):
        if self.linked_clone_fails and not full:
            raise PveCommandFailedError(["qm", "clone"], 255, "linked clone feature is not supported")
        self.ops.append(("clone", source, target, name, full, storage))
        self.vms[target] = {"name": self.vm_name, "scsi0": "local-lvm:base-9100-disk-0/vm-4242-disk-0"}

    def set_vm_options(self, vmid, options):
        self.ops.append(("set", vmid, dict(options)))
        self.vms[vmid].update(options)

    def delete_vm_options(self, vmid, keys):
        self.ops.append(("delete", vmid, list(keys)))
        for key in keys:
            self.vms[vmid].pop(key, None)

    def destroy_vm(self, vmid, purge=True):
        self.ops.append(("destroy", vmid, purge))
        self.vms.pop(vmid)

    def guest_exec_with_stdin(self, vmid, args, stdin_text, timeout_seconds=60):
        self.ops.append(("exec_stdin", vmid, list(args), stdin_text))
        return {"exitcode": 0, "exited": 1}


def test_provision_maps_attaches_read_only_and_reports_every_resource(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    node = FakeNode(tmp_path).install(monkeypatch)
    client = FakeClient()

    disk_mount.provision(_job(), CONFIG, client)

    # Each disk mapped with the namespace, from the repository with the port inside.
    assert ("list", "resurgam@pbs!lab@pbs.example:8007:store1", SNAPSHOT, "Kunde-A") in node.ops
    assert [op for op in node.ops if op[0] == "map"] == [
        ("map", "drive-scsi0.img.fidx", "/dev/loop0"),
        ("map", "drive-scsi1.img.fidx", "/dev/loop1"),
    ]
    # Every resource reported before the next step, in creation order.
    progress = [c[1] for c in client.calls if c[0] == "progress"]
    assert progress == [
        {"mapped_device": {"archive": "drive-scsi0.img.fidx", "device": "/dev/loop0"}},
        {"mapped_device": {"archive": "drive-scsi1.img.fidx", "device": "/dev/loop1"}},
        {"helper_vmid": 4242},
        {"snippet_volume": "local:snippets/resurgam-disk-mount-0b6f1c2e0000.yaml"},
    ]
    # Linked clone of the template, named for the guardrail.
    assert ("clone", 9100, 4242, "rsgm-mount-0b6f1c2e", False, None) in node.ops
    # Attached read-only, with serial, never backed up; network + cloud-init.
    options = next(op[2] for op in node.ops if op[0] == "set")
    assert options["scsi1"] == "/dev/loop0,ro=1,serial=rsgm1,backup=0"
    assert options["scsi2"] == "/dev/loop1,ro=1,serial=rsgm2,backup=0"
    assert options["net0"] == "model=virtio,bridge=vmbr0,tag=20"
    assert options["ipconfig0"] == "ip=192.168.20.50/24,gw=192.168.20.1"
    assert options["cicustom"] == "user=local:snippets/resurgam-disk-mount-0b6f1c2e0000.yaml"
    assert "boot" not in options and "scsi0" not in options
    # The password only travels on stdin.
    exec_op = next(op for op in node.ops if op[0] == "exec_stdin")
    # Through the interpreter: executed directly, the script failed (live finding 2026-11-21).
    assert exec_op[2] == [
        "/usr/bin/python3",
        "/usr/local/sbin/resurgam-share-helper",
        "set-smb-password",
        "rsgm-1a2b3c4d",
    ]
    assert exec_op[3] == f"{PASSWORD}\n"
    assert all(PASSWORD not in " ".join(map(str, op[:3])) for op in node.ops)
    # The snippet was written and removed again after the start.
    assert not (tmp_path / "resurgam-disk-mount-0b6f1c2e0000.yaml").exists()
    status = [c for c in client.calls if c[0] == "status"]
    assert status == [
        (
            "status",
            "ready",
            {
                "addresses": ["192.168.20.50"],
                "volumes": [
                    {"disk_index": 1, "archive": "drive-scsi0.img.fidx", "partition": "part1", "mounted": True}
                ],
            },
        )
    ]
    assert PASSWORD not in caplog.text


def test_provision_refuses_a_mount_for_another_node_without_touching_anything(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)
    client = FakeClient(_claim(target_node="pve2"))

    disk_mount.provision(_job(target_node="pve2"), CONFIG, client)

    assert node.ops == []
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "failed" and kw["cleaned_up"] is True
    assert "pve1" in kw["error"] and "pve2" in kw["error"]


def test_provision_failure_after_boot_removes_vm_unmaps_and_deletes_snippet(tmp_path, monkeypatch, caplog):
    node = FakeNode(tmp_path, helper_result={"status": "failed", "error": "OSError: no space"}).install(monkeypatch)
    client = FakeClient()

    disk_mount.provision(_job(), CONFIG, client)

    kinds = [op[0] for op in node.ops]
    assert "destroy" in kinds
    # The loop drives are detached before destroy, so destroy never handles them.
    delete = next(op for op in node.ops if op[0] == "delete")
    assert sorted(delete[2]) == ["scsi1", "scsi2"]
    assert kinds.index("delete") < kinds.index("destroy")
    assert ("unmap", "/dev/loop0") in node.ops and ("unmap", "/dev/loop1") in node.ops
    assert not any(op[0] == "exec_stdin" for op in node.ops)  # no password handed to a broken helper
    assert not (tmp_path / "resurgam-disk-mount-0b6f1c2e0000.yaml").exists()
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "failed" and kw["cleaned_up"] is True
    assert "no space" in kw["error"]
    assert PASSWORD not in kw["error"] and PASSWORD not in caplog.text


def test_provision_falls_back_to_a_full_clone_on_the_requested_storage(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)
    node.linked_clone_fails = True
    disk_mount.provision(_job(), CONFIG, FakeClient())
    assert ("clone", 9100, 4242, "rsgm-mount-0b6f1c2e", True, "local-lvm") in node.ops


def test_provision_treats_a_vmid_with_a_foreign_name_as_never_created(tmp_path, monkeypatch):
    # The VMID now carries a foreign name (someone else took it) -- never destroyed,
    # and not a leftover of this mount either.
    node = FakeNode(tmp_path, helper_result={"status": "failed"}, vm_name="someone-elses-vm").install(monkeypatch)
    client = FakeClient()
    disk_mount.provision(_job(), CONFIG, client)
    assert not any(op[0] in ("destroy", "stop") for op in node.ops)
    assert ("unmap", "/dev/loop0") in node.ops
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "failed" and kw["cleaned_up"] is True


def test_provision_keeps_disks_mapped_when_the_helper_cannot_be_destroyed(tmp_path, monkeypatch):
    node = FakeNode(tmp_path, helper_result={"status": "failed"}).install(monkeypatch)

    def destroy_fails(vmid, purge=True):
        raise PveCommandFailedError(["qm", "destroy"], 255, "VM is locked")

    monkeypatch.setattr(disk_mount, "destroy_vm", destroy_fails)
    client = FakeClient()
    disk_mount.provision(_job(), CONFIG, client)
    assert not any(op[0] == "unmap" for op in node.ops)
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "failed" and kw["cleaned_up"] is False


def test_provision_backend_unreachable_after_mapping_still_unmaps(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)

    class FlakyClient(FakeClient):
        def report_disk_mount_progress(self, mount_id, **kw):
            raise LabAgentApiError(0, "ConnectError: connection refused")

    client = FlakyClient()
    disk_mount.provision(_job(), CONFIG, client)
    assert ("unmap", "/dev/loop0") in node.ops
    assert not node.mappings
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "failed" and kw["cleaned_up"] is True


def test_provision_unexpected_error_still_cleans_up(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)

    def boom(vmid, options):
        raise KeyError("scsi9")

    monkeypatch.setattr(disk_mount, "set_vm_options", boom)
    client = FakeClient()
    disk_mount.provision(_job(), CONFIG, client)
    assert ("destroy", 4242, True) in node.ops and ("unmap", "/dev/loop1") in node.ops
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "failed" and kw["cleaned_up"] is True


def test_provision_truncates_the_ready_report_to_what_the_backend_accepts(tmp_path, monkeypatch):
    result = {
        "status": "ok",
        "addresses": [f"10.0.0.{i}" for i in range(40)],
        "volumes": [{"disk_index": 1, "partition": f"p{i}", "mounted": False} for i in range(300)],
    }
    FakeNode(tmp_path, helper_result=result).install(monkeypatch)
    client = FakeClient()
    disk_mount.provision(_job(), CONFIG, client)
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "ready"
    assert len(kw["addresses"]) == 16 and len(kw["volumes"]) == 256


@pytest.mark.parametrize(
    "overrides",
    [
        {"snippet_filename": "../../etc/cron.d/evil"},
        {"snippet_filename": "resurgam-x.yaml\n"},
        {"snippet_storage": "local:../x"},
    ],
)
def test_provision_refuses_a_malformed_snippet_location(tmp_path, monkeypatch, overrides):
    node = FakeNode(tmp_path).install(monkeypatch)
    client = FakeClient(_claim(**overrides))
    disk_mount.provision(_job(), CONFIG, client)
    assert not any(op[0] == "set" for op in node.ops)
    (_, status, kw) = next(c for c in client.calls if c[0] == "status")
    assert status == "failed" and "snippet" in kw["error"]


def _teardown_job(**overrides):
    data = {
        "helper_vmid": 4242,
        "mapped_devices": [
            {"archive": "drive-scsi0.img.fidx", "device": "/dev/loop0"},
            {"archive": "drive-scsi1.img.fidx", "device": "/dev/loop1"},
        ],
        "snippet_volume": "local:snippets/resurgam-disk-mount-0b6f1c2e0000.yaml",
    }
    data.update(overrides)
    return _job("teardown", **data)


def test_teardown_removes_everything_this_mount_owns(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)
    node.vms[4242] = {"name": "rsgm-mount-0b6f1c2e", "scsi1": "/dev/loop0,ro=1,serial=rsgm1,backup=0"}
    node.mappings = {
        "/dev/loop0": f"resurgam@pbs!lab@pbs.example:8007:store1:{SNAPSHOT}/drive-scsi0.img",
        "/dev/loop1": f"resurgam@pbs!lab@pbs.example:8007:store1:{SNAPSHOT}/drive-scsi1.img",
    }
    (tmp_path / "resurgam-disk-mount-0b6f1c2e0000.yaml").write_text("#cloud-config\n")
    client = FakeClient()

    disk_mount.teardown(_teardown_job(), client)

    assert ("stop", 4242) in node.ops and ("destroy", 4242, True) in node.ops
    assert ("delete", 4242, ["scsi1"]) in node.ops
    assert ("unmap", "/dev/loop0") in node.ops and ("unmap", "/dev/loop1") in node.ops
    assert not (tmp_path / "resurgam-disk-mount-0b6f1c2e0000.yaml").exists()
    assert client.calls == [("teardown", "done", {})]


def test_teardown_of_resources_already_gone_is_done(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)
    client = FakeClient()
    disk_mount.teardown(_teardown_job(), client)
    assert not any(op[0] in ("destroy", "unmap") for op in node.ops)
    assert client.calls == [("teardown", "done", {})]


def test_teardown_treats_a_renamed_vm_and_a_reused_loop_device_as_already_gone(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)
    node.vms[4242] = {"name": "production-db"}
    node.mappings = {"/dev/loop0": "root@pam@pbs.example:8007:store1:vm/999/2026-10-01T00:00:00Z/drive-scsi0.img"}
    client = FakeClient()

    disk_mount.teardown(_teardown_job(), client)

    # Never touches the foreign VM or mapping -- and is not stuck on them either.
    assert not any(op[0] in ("destroy", "stop", "unmap", "delete") for op in node.ops)
    assert client.calls == [("teardown", "done", {})]


def test_teardown_keeps_disks_mapped_when_destroying_the_helper_fails(tmp_path, monkeypatch):
    node = FakeNode(tmp_path).install(monkeypatch)
    node.vms[4242] = {"name": "rsgm-mount-0b6f1c2e", "scsi1": "/dev/loop0,ro=1,serial=rsgm1,backup=0"}
    node.mappings = {"/dev/loop0": f"x:{SNAPSHOT}/drive-scsi0.img"}

    def destroy_fails(vmid, purge=True):
        raise PveCommandFailedError(["qm", "destroy"], 255, "VM is locked (backup)")

    monkeypatch.setattr(disk_mount, "destroy_vm", destroy_fails)
    client = FakeClient()
    disk_mount.teardown(_teardown_job(), client)
    assert not any(op[0] == "unmap" for op in node.ops)
    (_, status, kw) = client.calls[0]
    assert status == "failed" and "4242" in kw["error"] and "mapped" in kw["error"]


@pytest.mark.parametrize(
    "volume",
    ["local:snippets/../../../etc/passwd", "local:iso/resurgam-x.yaml", "local:snippets/other.yaml", "/etc/passwd"],
)
def test_teardown_refuses_to_delete_a_foreign_snippet_volume(tmp_path, monkeypatch, volume):
    FakeNode(tmp_path).install(monkeypatch)
    resolved = []
    monkeypatch.setattr(disk_mount, "resolve_storage_path", lambda v: resolved.append(v) or str(tmp_path / "x"))
    client = FakeClient()
    disk_mount.teardown(_teardown_job(helper_vmid=None, mapped_devices=[], snippet_volume=volume), client)
    assert resolved == []
    (_, status, kw) = client.calls[0]
    assert status == "failed" and "Refused" in kw["error"]


# --- the new low-level helpers ------------------------------------------------


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def binaries(monkeypatch):
    # qm and pbs_client.map share the one `shutil` module.
    monkeypatch.setattr(qm.shutil, "which", lambda name: f"/usr/sbin/{name}")


def test_guest_exec_with_stdin_passes_the_secret_only_on_stdin(monkeypatch, binaries):
    captured = {}

    def fake(command, *, timeout, on_timeout, input_text=None, **kw):
        captured.update(command=command, input_text=input_text)
        return _completed(0, '{"exitcode": 0, "exited": 1}')

    monkeypatch.setattr(qm, "run_subprocess", fake)
    result = qm.guest_exec_with_stdin(4242, ["/usr/local/sbin/resurgam-share-helper", "set-smb-password", "u"], "pw\n")
    assert result["exitcode"] == 0
    assert captured["command"][:7] == ["/usr/sbin/qm", "guest", "exec", "4242", "--pass-stdin", "1", "--timeout"]
    assert "pw" not in " ".join(captured["command"])
    assert captured["input_text"] == "pw\n"


def test_guest_exec_with_stdin_error_does_not_echo_output(monkeypatch, binaries):
    monkeypatch.setattr(qm, "run_subprocess", lambda *a, **kw: _completed(0, "not json pw"))
    with pytest.raises(qm.PveOutputParseError) as excinfo:
        qm.guest_exec_with_stdin(1, ["x"], "pw")
    assert "not json pw" not in str(excinfo.value) and excinfo.value.output == "(output withheld)"


def test_clone_vm_linked_and_full_shapes(monkeypatch, binaries):
    commands = []
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: (commands.append(command), _completed())[1])
    qm.clone_vm(9100, 4242, name="rsgm-mount-1", description="d")
    qm.clone_vm(9100, 4243, name="rsgm-mount-2", full=True, storage="local-lvm")
    assert commands[0] == ["/usr/sbin/qm", "clone", "9100", "4242", "--name", "rsgm-mount-1", "--description", "d"]
    assert commands[1][-4:] == ["--full", "1", "--storage", "local-lvm"]


def test_next_free_vmid_and_cluster_vmids(monkeypatch, binaries):
    outputs = iter([_completed(0, '"4242"\n'), _completed(0, '[{"vmid": 100, "type": "qemu"}, {"vmid": 4242}]')])
    commands = []
    monkeypatch.setattr(qm, "_run", lambda command: (commands.append(command), next(outputs))[1])
    assert qm.next_free_vmid() == 4242
    assert qm.list_cluster_vmids() == {100, 4242}
    assert commands[0] == ["/usr/sbin/pvesh", "get", "/cluster/nextid", "--output-format", "json"]


def test_delete_vm_options_skips_an_empty_list(monkeypatch, binaries):
    commands = []
    monkeypatch.setattr(qm, "_run", lambda command: (commands.append(command), _completed())[1])
    qm.delete_vm_options(4242, [])
    qm.delete_vm_options(4242, ["scsi1", "scsi2"])
    assert commands == [["/usr/sbin/qm", "set", "4242", "--delete", "scsi1,scsi2"]]


def test_list_mappings_parses_the_unmap_listing(monkeypatch, binaries):
    # Format from PBS's proxmox-backup-client/src/mount.rs: "{loopdev}:\t{name}".
    stderr = (
        "/dev/loop0:\tresurgam@pbs!lab@pbs.example:8007:store1:vm/116/2026-09-14T00:48:57Z/drive-scsi0.img\n"
        "(unmapped):\tstale-name\n"
    )
    monkeypatch.setattr(pbs_map, "_run", lambda command, **kw: _completed(0, "", stderr))
    assert pbs_map.list_mappings() == {
        "/dev/loop0": "resurgam@pbs!lab@pbs.example:8007:store1:vm/116/2026-09-14T00:48:57Z/drive-scsi0.img"
    }
    monkeypatch.setattr(pbs_map, "_run", lambda command, **kw: _completed(0, "", "Nothing mapped.\n"))
    assert pbs_map.list_mappings() == {}


def test_claim_repr_never_shows_the_password():
    claim = _claim()
    assert PASSWORD not in repr(claim) and PASSWORD not in str(claim)
