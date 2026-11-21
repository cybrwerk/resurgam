"""Disk Mounts helper VM (ADR 0025): the in-guest script's pure parts (which
partitions it mounts and how -- always read-only --, smb.conf and exports),
the cloud-init user data (no secrets), the PBS disk listing and the guided
share helper template build (fake SSH, mocked PVE)."""

import base64
import json
import logging
import re
import shlex
from datetime import UTC, datetime

import pytest
import yaml

from resurgam_api.disk_mounts import helper_config, share_helper_guest, snapshot_disks
from resurgam_api.disk_mounts import share_helper_template as template_module
from resurgam_api.lab_setup import template_build
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.pve import LabUplinkIpAllocation, PveCluster
from tests.lab_ssh_fakes import FakeSshBackend, guest_exec, ok
from tests.test_lab_provisioning_api import (  # noqa: F401 -- fixtures and helpers shared with the probe build
    PASSWORD,
    _all_audit_text,
    _assert_no_secrets,
    _create_cluster,
    _pve_for_template,
    _ssh,
    sync_jobs,
)

LSBLK = {
    "blockdevices": [
        {
            "name": "sda",
            "path": "/dev/sda",
            "serial": None,
            "type": "disk",
            "fstype": None,
            "size": 3 * 2**30,
            "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "ext4", "size": 3 * 2**30}],
        },
        {
            "name": "sdb",
            "path": "/dev/sdb",
            "serial": "rsgm1",
            "type": "disk",
            "fstype": None,
            "size": 2**31,
            "children": [
                {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "ntfs", "size": 500},
                {"name": "sdb2", "path": "/dev/sdb2", "type": "part", "fstype": "BitLocker", "size": 600},
                {"name": "sdb3", "path": "/dev/sdb3", "type": "part", "fstype": "LVM2_member", "size": 700},
                {"name": "sdb4", "path": "/dev/sdb4", "type": "part", "fstype": None, "size": 16},
            ],
        },
        {"name": "sdc", "path": "/dev/sdc", "serial": "rsgm2", "type": "disk", "fstype": "xfs", "size": 2**30},
    ]
}
DISKS = [
    {"index": 1, "serial": "rsgm1", "archive": "drive-scsi0.img.fidx"},
    {"index": 2, "serial": "rsgm2", "archive": "drive-scsi1.img.fidx"},
    {"index": 3, "serial": "rsgm3", "archive": "drive-sata0.img.fidx"},
]


# --- in-guest script ---------------------------------------------------------


def test_plan_finds_disks_by_serial_and_never_touches_the_helpers_own_disk():
    candidates, missing = share_helper_guest.plan_volumes(LSBLK, DISKS)
    assert [(c["device"], c["part"], c["fstype"]) for c in candidates] == [
        ("/dev/sdb1", "part1", "ntfs"),
        ("/dev/sdb2", "part2", "BitLocker"),
        ("/dev/sdb3", "part3", "LVM2_member"),
        ("/dev/sdb4", "part4", None),
        ("/dev/sdc", "whole", "xfs"),
    ]
    assert all(not c["device"].startswith("/dev/sda") for c in candidates)
    assert missing == [DISKS[2]]


@pytest.mark.parametrize(
    ("fstype", "expected"),
    [
        ("ntfs", ("mount", None)),
        ("ext4", ("mount", None)),
        ("xfs", ("mount", None)),
        ("btrfs", ("mount", None)),
        ("vfat", ("mount", None)),
        ("LVM2_member", ("lvm", None)),
        ("crypto_LUKS", ("skip", "encrypted")),
        ("BitLocker", ("skip", "encrypted")),
        ("swap", ("skip", "swap")),
        ("zfs_member", ("skip", "unsupported")),
        ("ReFS", ("skip", "unsupported")),
        ("hfsplus", ("skip", "unsupported")),
        (None, ("skip", "no_filesystem")),
    ],
)
def test_classify(fstype, expected):
    assert share_helper_guest.classify(fstype) == expected


def test_every_mount_attempt_is_read_only_without_journal_replay():
    for fstype in share_helper_guest.MOUNT_ATTEMPTS:
        for command in share_helper_guest.mount_commands(fstype, "/dev/sdb1", "/srv/resurgam/x"):
            options = command[command.index("-o") + 1].split(",")
            assert options[0] == "ro", command
            assert "rw" not in options
    options = {
        fstype: [c[4] for c in share_helper_guest.mount_commands(fstype, "/dev/x", "/t")]
        for fstype in ("ext4", "ext3", "xfs", "btrfs", "ntfs")
    }
    assert options["ext4"] == ["ro,noload"] and options["ext3"] == ["ro,noload"]
    assert options["xfs"] == ["ro,norecovery,nouuid"]
    assert options["btrfs"] == ["ro,rescue=nologreplay"]
    assert [c[2] for c in share_helper_guest.mount_commands("ntfs", "/dev/x", "/t")] == ["ntfs3", "ntfs-3g"]


def test_volume_names_are_share_safe():
    assert share_helper_guest.volume_name(1, "part2", "ntfs") == "disk1-part2-ntfs"
    assert share_helper_guest.volume_name(2, "vg/root;rm", "ext4") == "disk2-vg-root-rm-ext4"


def test_smb_conf_is_read_only_smb3_one_user_and_client_restricted():
    conf = share_helper_guest.render_smb_conf("rsgm-1a2b3c4d", ["192.168.20.0/24", "10.1.2.3/32"])
    settings = {k.strip(): v.strip() for k, sep, v in (line.partition("=") for line in conf.splitlines()) if sep == "="}
    assert settings["server min protocol"] == "SMB3_00"
    assert settings["server smb encrypt"] == "desired"
    assert settings["map to guest"] == "never"
    assert settings["restrict anonymous"] == "2"
    assert settings["read only"] == "yes" and settings["writable"] == "no"
    assert settings["guest ok"] == "no"
    assert settings["valid users"] == "rsgm-1a2b3c4d"
    assert settings["wide links"] == "no"
    assert settings["hosts allow"] == "192.168.20.0/255.255.255.0 10.1.2.3"
    assert settings["hosts deny"] == "ALL"
    assert "[restore]" in conf and "path = /srv/resurgam" in conf
    assert "hosts allow" not in share_helper_guest.render_smb_conf("rsgm-1a2b3c4d", [])
    with pytest.raises(ValueError):
        share_helper_guest.render_smb_conf("root; rm -rf /", [])


def test_exports_are_read_only_and_only_for_the_given_clients():
    volumes = [
        {"name": "disk1-part1-ntfs", "mounted": True},
        {"name": None, "mounted": False},
        {"name": "disk2-whole-xfs", "mounted": True},
    ]
    exports = share_helper_guest.render_exports(volumes, ["192.168.20.0/24", "10.1.2.3"])
    lines = [line for line in exports.splitlines() if not line.startswith("#")]
    assert lines == [
        (
            "/srv/resurgam/disk1-part1-ntfs 192.168.20.0/24(ro,no_subtree_check,no_root_squash,sec=sys,fsid=1) "
            "10.1.2.3/32(ro,no_subtree_check,no_root_squash,sec=sys,fsid=1)"
        ),
        (
            "/srv/resurgam/disk2-whole-xfs 192.168.20.0/24(ro,no_subtree_check,no_root_squash,sec=sys,fsid=2) "
            "10.1.2.3/32(ro,no_subtree_check,no_root_squash,sec=sys,fsid=2)"
        ),
    ]
    assert "rw" not in exports and "*(" not in exports and "insecure" not in exports
    with pytest.raises(ValueError):
        share_helper_guest.render_exports(volumes, [])
    with pytest.raises(ValueError):
        share_helper_guest.render_exports(volumes, ["0.0.0.0/0"])


def test_set_smb_password_reads_stdin_and_never_uses_argv(monkeypatch):
    calls = []

    def fake_run(command, *, input_text=None, check=False):
        calls.append((command, input_text))

        class R:
            returncode = 0
            stdout = stderr = ""

        return R()

    monkeypatch.setattr(share_helper_guest, "_run", fake_run)
    monkeypatch.setattr(share_helper_guest.sys, "stdin", __import__("io").StringIO("S3cret-pw\n"))
    assert share_helper_guest.set_smb_password("rsgm-1a2b3c4d") == 0
    assert calls[0] == (["smbpasswd", "-a", "-s", "rsgm-1a2b3c4d"], "S3cret-pw\nS3cret-pw\n")
    assert all("S3cret-pw" not in " ".join(command) for command, _ in calls)


# --- cloud-init user data ----------------------------------------------------------------------


def test_user_data_carries_config_and_script_but_no_secret():
    config = helper_config.render_helper_config(
        mount_id="m1",
        smb_user="rsgm-1a2b3c4d",
        protocols=["smb", "nfs"],
        allowed_clients=["192.168.20.0/24"],
        archives=["drive-scsi0.img.fidx", "drive-scsi1.img.fidx"],
    )
    user_data = helper_config.render_user_data(config, hostname="rsgm-mount-0b6f1c2e")
    assert user_data.startswith("#cloud-config\n")
    parsed = yaml.safe_load(user_data)
    files = {f["path"]: base64.b64decode(f["content"]).decode() for f in parsed["write_files"]}
    shipped = json.loads(files["/etc/resurgam-share-helper/config.json"])
    assert shipped["disks"] == [
        {"index": 1, "serial": "rsgm1", "archive": "drive-scsi0.img.fidx"},
        {"index": 2, "serial": "rsgm2", "archive": "drive-scsi1.img.fidx"},
    ]
    assert set(shipped) == {"mount_id", "smb_user", "protocols", "allowed_clients", "disks"}
    assert files["/usr/local/sbin/resurgam-share-helper"] == helper_config.guest_script()
    assert parsed["runcmd"] == [
        ["/usr/bin/python3", "/usr/local/sbin/resurgam-share-helper", "setup", "/etc/resurgam-share-helper/config.json"]
    ]
    assert parsed["hostname"] == "rsgm-mount-0b6f1c2e" and parsed["ssh_pwauth"] is False
    assert "password" not in user_data.lower()


def test_guest_script_runs_with_the_standard_library_only():
    imports = re.findall(r"^(?:import|from) (\S+)", helper_config.guest_script(), flags=re.MULTILINE)
    assert set(imports) <= {"__future__", "ipaddress", "json", "os", "re", "subprocess", "sys"}


def test_serials_fit_qemu_and_nic_ipconfig_values():
    assert len(helper_config.disk_serial(30)) <= 20
    assert helper_config.helper_nic("vmbr0", None) == "model=virtio,bridge=vmbr0"
    assert helper_config.helper_nic("vmbr1", 20) == "model=virtio,bridge=vmbr1,tag=20"
    assert helper_config.helper_ipconfig("dhcp", None, None) == "ip=dhcp"
    assert helper_config.helper_ipconfig("static", "10.0.0.5/24", None) == "ip=10.0.0.5/24"
    assert helper_config.helper_ipconfig("static", "10.0.0.5/24", "10.0.0.1") == "ip=10.0.0.5/24,gw=10.0.0.1"


# --- PBS disk listing ---------------------------------------------------------------------------


def test_list_snapshot_disks_asks_pbs_for_that_snapshot_and_keeps_only_disks(monkeypatch):
    captured = {}

    def fake_get(host, port, trust, token, path, timeout, params):
        captured.update(path=path, params=params)
        return [
            {"filename": "qemu-server.conf.blob", "size": 400},
            {"filename": "drive-scsi1.img.fidx", "size": 2**30, "crypt-mode": "none"},
            {"filename": "drive-scsi0.img.fidx", "size": 2**31, "crypt-mode": "encrypt"},
            {"filename": "index.json.blob"},
        ], None

    monkeypatch.setattr(snapshot_disks, "_get_data", fake_get)
    when = datetime(2026, 9, 14, 0, 48, 57, tzinfo=UTC)
    disks = snapshot_disks.list_snapshot_disks(
        "pbs",
        8007,
        None,
        "tok",
        5.0,
        datastore="store1",
        namespace="Kunde",
        backup_type="vm",
        backup_id="116",
        snapshot_time=when,
    )
    assert captured["path"] == "/api2/json/admin/datastore/store1/files"
    assert captured["params"] == {
        "backup-type": "vm",
        "backup-id": "116",
        "backup-time": int(when.timestamp()),
        "ns": "Kunde",
    }
    assert [(d.archive, d.slot, d.crypt_mode) for d in disks] == [
        ("drive-scsi0.img.fidx", "scsi0", "encrypt"),
        ("drive-scsi1.img.fidx", "scsi1", "none"),
    ]
    assert snapshot_disks.snapshot_path("vm", "116", when) == "vm/116/2026-09-14T00:48:57Z"
    monkeypatch.setattr(snapshot_disks, "_get_data", lambda *a: (None, "HTTP 403: forbidden"))
    with pytest.raises(snapshot_disks.SnapshotFilesError):
        snapshot_disks.list_snapshot_disks(
            "pbs",
            8007,
            None,
            "tok",
            5.0,
            datastore="s",
            namespace="",
            backup_type="vm",
            backup_id="1",
            snapshot_time=when,
        )


# --- share helper template build ----------------------------------------------------------------


def test_build_script_installs_the_share_tools_and_keeps_services_off():
    script = template_module.render_build_script()
    for package in ("samba", "nfs-kernel-server", "ntfs-3g", "lvm2", "xfsprogs", "btrfs-progs", "qemu-guest-agent"):
        assert package in script
    assert "systemctl disable --now" in script and "smbd" in script and "nfs-server" in script
    assert template_build.RESULT_FILE in script
    user_data = yaml.safe_load(template_module.render_user_data())
    assert base64.b64decode(user_data["write_files"][0]["content"]).decode() == script


def _helper_node(fake: FakeSshBackend, *, verdict=None):
    fake.respond(r"^id -u$", ok("0\n"))
    fake.respond(
        r"^command -v qm pvesm", ok("".join(f"/usr/bin/{t}\n" for t in ("qm", "pvesm", "pvesh", "curl", "sha512sum")))
    )
    fake.respond(r"mktemp -d /var/tmp/resurgam-template", ok("/var/tmp/resurgam-template.h3lp\n"))
    fake.respond(r"^pvesm path local:snippets/", ok("/var/lib/vz/snippets/resurgam-share-helper-build-9010.yaml\n"))
    fake.respond(
        r"cat /var/lib/resurgam-template-build/result.json",
        guest_exec(verdict if verdict is not None else {"status": "ok", "template_version": "1"}),
    )
    fake.respond(r"tail -n 40", guest_exec(None))
    fake.respond(r"^qm status", ok("status: running\n"))
    return fake


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(template_module.time, "sleep", lambda s: None)


def _start(client, headers, cluster_id, **extra):
    body = {"ssh": _ssh(), "storage": "local-lvm", **extra}
    return client.post(
        f"/api/v1/pve-clusters/{cluster_id}/lab-setup/share-helper-template-build-jobs", json=body, headers=headers
    )


@pytest.mark.usefixtures("sync_jobs", "no_sleep")
def test_share_helper_template_build(client, platform_admin_headers, db_session, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, is_lab_compute=True)
    _pve_for_template(monkeypatch)
    fake = _helper_node(FakeSshBackend(accept_password=PASSWORD)).install(monkeypatch)

    response = _start(client, platform_admin_headers, cluster_id)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "succeeded", job
    assert job["kind"] == "share_helper_template_build"
    assert [s["key"] for s in job["steps"]] == [
        "connect",
        "privileges",
        "preflight",
        "image",
        "snippet",
        "create",
        "first_boot",
        "finalize",
    ]
    create = next(c for c in fake.commands if c.startswith("qm create"))
    opts = dict(zip(shlex.split(create)[3::2], shlex.split(create)[4::2], strict=False))
    assert opts["--name"] == "resurgam-share-helper-template"
    assert opts["--tags"] == "resurgam-share-helper"
    assert "--ide3" not in opts  # no seed ISO
    assert opts["--cicustom"] == "user=local:snippets/resurgam-share-helper-build-9010.yaml"
    assert "qm set 9010 --delete cicustom,ipconfig0,net0" in fake.commands
    description = next(c for c in fake.commands if c.startswith("qm set 9010 --description"))
    assert "resurgam-share-helper-template: 1" in description
    assert "qm template 9010" in fake.commands
    assert not any("qm destroy" in c for c in fake.commands)

    db_session.expire_all()
    assert db_session.get(PveCluster, cluster_id).disk_mount_helper_template_vmid == 9010
    audit = _all_audit_text(db_session)
    assert "pve_cluster_share_helper_template_built" in audit
    _assert_no_secrets(response.text, audit, "\n".join(fake.commands), caplog.text)
    assert template_module.parse_template_version(description.split("--description ", 1)[1]) == "1"
    assert db_session.query(LabUplinkIpAllocation).count() == 0


@pytest.mark.usefixtures("sync_jobs", "no_sleep")
def test_share_helper_template_build_failure_destroys_only_its_vm(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, is_lab_compute=True)
    _pve_for_template(monkeypatch)
    fake = _helper_node(
        FakeSshBackend(accept_password=PASSWORD), verdict={"status": "failed", "step": "install_packages"}
    )
    fake.install(monkeypatch)
    job = _start(client, platform_admin_headers, cluster_id).json()
    assert job["status"] == "failed" and job["error_code"] == "build_failed"
    assert "install_packages" in job["error_message"]
    assert any(c.startswith("qm stop 9010") and "qm destroy 9010" in c for c in fake.commands)
    db_session.expire_all()
    assert db_session.get(PveCluster, cluster_id).disk_mount_helper_template_vmid is None
    failed = [
        e for e in db_session.query(AuditEvent).all() if e.action == "pve_cluster_share_helper_template_build_failed"
    ]
    assert failed and failed[0].detail["error_code"] == "build_failed"


def test_share_helper_template_build_needs_a_lab_cluster(client, platform_admin_headers, db_session):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = _start(client, platform_admin_headers, cluster_id)
    assert response.status_code == 400 and response.json()["code"] == "not_lab_compute"
