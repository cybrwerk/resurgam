"""resurgam-share-helper -- runs INSIDE the Disk Mounts helper VM (ADR 0025).

This file is shipped verbatim into every helper VM through its cloud-init user
data (`disk_mounts/helper_config.py`) and must therefore stay a standalone
Python 3 script with the standard library only (the Debian 13 genericcloud
image ships python3 for cloud-init). The pure functions below are also
imported by the backend's tests.

Commands:

- `setup <config.json>`: finds the backup's disks by their QEMU serial, mounts
  every recognisable file system READ-ONLY under `/srv/resurgam/<volume>`,
  writes smb.conf / exports, starts the services and writes
  `/run/resurgam-share-helper/result.json` (read back by resurgam-lab-agent
  through the guest agent).
- `set-smb-password <user>`: reads the password from stdin (handed in by
  `qm guest exec --pass-stdin`, never on a command line or in a file) and
  passes it to `smbpasswd -a -s`.

Mount options (kernel docs): ext3/ext4 `noload` ("Don't load the journal on
mounting"), XFS `norecovery` ("must be mounted read-only"), btrfs
`rescue=nologreplay` ("the tree log is replayed even with a read-only mount!"),
NTFS via `ntfs3` and `ntfs-3g` as the fallback (ntfs3 refuses a volume marked
dirty unless `force`). The disks are also attached with `ro=1` by QEMU, so a
write could never reach the backup anyway.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys

SHARE_ROOT = "/srv/resurgam"
RESULT_DIR = "/run/resurgam-share-helper"
RESULT_FILE = f"{RESULT_DIR}/result.json"
SMB_CONF = "/etc/samba/smb.conf"
EXPORTS = "/etc/exports"
SMB_SHARE_NAME = "restore"

# File systems this helper mounts, and how (read-only, no journal/log replay).
_COMMON_RO_OWNER = "uid=0,gid=0,umask=0022"
MOUNT_ATTEMPTS: dict[str, list[tuple[str, str]]] = {
    "ntfs": [("ntfs3", f"ro,{_COMMON_RO_OWNER}"), ("ntfs-3g", f"ro,{_COMMON_RO_OWNER}")],
    "ext4": [("ext4", "ro,noload")],
    "ext3": [("ext3", "ro,noload")],
    "ext2": [("ext2", "ro")],
    "xfs": [("xfs", "ro,norecovery,nouuid")],
    "btrfs": [("btrfs", "ro,rescue=nologreplay")],
    "vfat": [("vfat", f"ro,{_COMMON_RO_OWNER}")],
    "exfat": [("exfat", f"ro,{_COMMON_RO_OWNER}")],
}
# Recognised but deliberately not mounted -- reported with a reason instead.
_NOT_MOUNTABLE = {
    "crypto_LUKS": "encrypted",
    "BitLocker": "encrypted",
    "swap": "swap",
    "zfs_member": "unsupported",
    "ReFS": "unsupported",
    "refs": "unsupported",
}

_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_name(value: str) -> str:
    return _NAME_SAFE.sub("-", value).strip("-") or "volume"


def volume_name(disk_index: int, part: str, fstype: str) -> str:
    """`disk1-part2-ntfs`, `disk1-vg0-root-ext4` -- the folder under the SMB
    share and the NFS export path. Stable and readable on a Windows client."""
    return safe_name(f"disk{disk_index}-{part}-{fstype}")


def classify(fstype: str | None) -> tuple[str, str | None]:
    """('mount', None) for a file system this helper mounts, ('lvm', None) for
    an LVM PV, else ('skip', reason)."""
    if fstype in MOUNT_ATTEMPTS:
        return "mount", None
    if fstype == "LVM2_member":
        return "lvm", None
    if not fstype:
        return "skip", "no_filesystem"
    return "skip", _NOT_MOUNTABLE.get(fstype, "unsupported")


def mount_commands(fstype: str, device: str, target: str) -> list[list[str]]:
    """Every `mount` invocation to try for one file system, in order. Always
    read-only; `-o ro` is never dropped."""
    commands = []
    for kernel_type, options in MOUNT_ATTEMPTS[fstype]:
        assert options.split(",")[0] == "ro"
        commands.append(["mount", "-t", kernel_type, "-o", options, device, target])
    return commands


def _partition_label(disk_name: str, part_name: str) -> str:
    """`sdb2` -> `part2`, `nvme0n1p3` -> `part3`; falls back to the kernel name."""
    suffix = part_name.removeprefix(disk_name)
    digits = re.sub(r"^\D*", "", suffix)
    return f"part{digits}" if digits else suffix


def plan_volumes(lsblk: dict, disks: list[dict]) -> tuple[list[dict], list[dict]]:
    """From `lsblk -J -b -o NAME,PATH,SERIAL,TYPE,FSTYPE,SIZE` output and the
    mount's disk list (`[{"index": 1, "serial": "rsgm1", "archive": "drive-scsi0.img.fidx"}]`)
    returns (candidates, missing_disks). A candidate is one block device that
    may hold a file system: each partition, or the whole disk when it has no
    partition table."""
    by_serial = {}
    for dev in lsblk.get("blockdevices", []):
        if dev.get("type") == "disk" and dev.get("serial"):
            by_serial[str(dev["serial"]).strip()] = dev
    candidates: list[dict] = []
    missing: list[dict] = []
    for disk in disks:
        dev = by_serial.get(disk["serial"])
        if dev is None:
            missing.append(disk)
            continue
        children = [c for c in dev.get("children") or [] if c.get("type") == "part"]
        if not children:
            candidates.append(
                {
                    "disk_index": disk["index"],
                    "archive": disk["archive"],
                    "device": dev["path"],
                    "part": "whole",
                    "fstype": dev.get("fstype"),
                    "size_bytes": _int(dev.get("size")),
                }
            )
            continue
        for child in children:
            candidates.append(
                {
                    "disk_index": disk["index"],
                    "archive": disk["archive"],
                    "device": child["path"],
                    "part": _partition_label(dev["name"], child["name"]),
                    "fstype": child.get("fstype"),
                    "size_bytes": _int(child.get("size")),
                }
            )
    return candidates, missing


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def samba_hosts_allow(clients: list[str]) -> str:
    """smb.conf `hosts allow` in network/netmask form (smb.conf(5) example:
    `150.203.15.0/255.255.255.0`); a single address stays a bare address."""
    entries = []
    for client in clients:
        net = ipaddress.IPv4Network(client, strict=False)
        if net.prefixlen == 32:
            entries.append(str(net.network_address))
        else:
            entries.append(f"{net.network_address}/{net.netmask}")
    return " ".join(entries)


def render_smb_conf(user: str, clients: list[str], *, share_root: str = SHARE_ROOT) -> str:
    """Read-only SMB3 share for exactly one user (ADR 0025 §6). `force user =
    root` makes every file of the backup readable whatever its owner; `wide
    links = no` (also Samba's default) stops an absolute symlink from the
    backup reaching the helper's own files."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", user):
        raise ValueError(f"invalid share user {user!r}")
    lines = [
        "# Generated by resurgam-share-helper (RESURGAM Disk Mounts). Read-only, one user, SMB3 only.",
        "[global]",
        "   server role = standalone server",
        "   server string = RESURGAM disk mount",
        "   security = user",
        "   passdb backend = tdbsam",
        "   server min protocol = SMB3_00",
        "   server smb encrypt = desired",
        "   map to guest = never",
        "   restrict anonymous = 2",
        "   disable netbios = yes",
        "   smb ports = 445",
        "   load printers = no",
        "   printing = bsd",
        "   printcap name = /dev/null",
        "   disable spoolss = yes",
        "   usershare max shares = 0",
        "   log file = /var/log/samba/log.%m",
        "   max log size = 1000",
        "   logging = file",
    ]
    if clients:
        lines += [f"   hosts allow = {samba_hosts_allow(clients)}", "   hosts deny = ALL"]
    lines += [
        "",
        f"[{SMB_SHARE_NAME}]",
        "   comment = RESURGAM backup (read-only)",
        f"   path = {share_root}",
        "   browseable = yes",
        "   read only = yes",
        "   writable = no",
        "   guest ok = no",
        f"   valid users = {user}",
        "   force user = root",
        "   follow symlinks = yes",
        "   wide links = no",
        "   create mask = 0000",
        "   directory mask = 0000",
        "",
    ]
    return "\n".join(lines)


def render_exports(volumes: list[dict], clients: list[str], *, share_root: str = SHARE_ROOT) -> str:
    """`/etc/exports`: one read-only line per mounted volume, only for the
    requester's clients (exports(5)). `fsid=` is set for every export -- it is
    required for file systems not stored on a block device (ntfs-3g/FUSE) and
    keeps file handles stable."""
    if not clients:
        raise ValueError("NFS needs at least one client restriction")
    for client in clients:
        net = ipaddress.IPv4Network(client, strict=False)
        if net.prefixlen < 8:
            raise ValueError(f"client restriction {client} is too broad")
    lines = ["# Generated by resurgam-share-helper (RESURGAM Disk Mounts). Read-only."]
    for fsid, volume in enumerate((v for v in volumes if v.get("mounted")), start=1):
        options = f"ro,no_subtree_check,no_root_squash,sec=sys,fsid={fsid}"
        targets = " ".join(f"{ipaddress.IPv4Network(c, strict=False)}({options})" for c in clients)
        lines.append(f"{share_root}/{volume['name']} {targets}")
    return "\n".join(lines) + "\n"


# --- side effects (only run inside the helper VM) -----------------------------


def _run(command: list[str], *, input_text: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(command, input=input_text, capture_output=True, text=True, check=check, timeout=300)


def _lsblk() -> dict:
    out = _run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SERIAL,TYPE,FSTYPE,SIZE"], check=True).stdout
    return json.loads(out)


def _blkid_type(device: str) -> str | None:
    result = _run(["blkid", "-o", "value", "-s", "TYPE", device])
    value = result.stdout.strip()
    return value or None


def _try_mount(fstype: str, device: str, target: str) -> tuple[bool, str]:
    os.makedirs(target, exist_ok=True)
    errors = []
    for command in mount_commands(fstype, device, target):
        result = _run(command)
        if result.returncode == 0:
            return True, command[2]
        errors.append(f"{command[2]}: {(result.stderr or result.stdout).strip()[:200]}")
    try:
        os.rmdir(target)
    except OSError:
        pass
    return False, "; ".join(errors)


def _mount_candidate(candidate: dict, volumes: list[dict]) -> None:
    kind, reason = classify(candidate["fstype"])
    base = {
        "disk_index": candidate["disk_index"],
        "archive": candidate["archive"],
        "partition": candidate["part"],
        "fstype": candidate["fstype"],
        "size_bytes": candidate["size_bytes"],
    }
    if kind == "skip":
        volumes.append({**base, "name": None, "mounted": False, "reason": reason, "detail": None})
        return
    if kind == "lvm":
        _mount_lvm(candidate, base, volumes)
        return
    name = volume_name(candidate["disk_index"], candidate["part"], candidate["fstype"])
    ok, detail = _try_mount(candidate["fstype"], candidate["device"], f"{SHARE_ROOT}/{name}")
    volumes.append(
        {
            **base,
            "name": name if ok else None,
            "mounted": ok,
            "reason": None if ok else "mount_failed",
            "detail": None if ok else detail,
            "driver": detail if ok else None,
        }
    )


def _mount_lvm(candidate: dict, base: dict, volumes: list[dict]) -> None:
    """Activates the PV's volume group READ-ONLY (`activation/read_only_volume_list`
    covering the VG, `--devices` limited to this PV -- vgchange(8)) and mounts
    every LV it can read. `--readonly` is not used: it cannot activate."""
    pv = candidate["device"]
    vg = _run(["pvs", "--devices", pv, "--noheadings", "-o", "vg_name", pv]).stdout.strip()
    if not vg or not re.fullmatch(r"[A-Za-z0-9+_.-]+", vg):
        volumes.append({**base, "name": None, "mounted": False, "reason": "lvm_unreadable", "detail": None})
        return
    ro_config = f'activation {{ read_only_volume_list = [ "{vg}" ] }}'
    _run(["vgchange", "-an", "--devices", pv, vg])
    activated = _run(["vgchange", "-ay", "--devices", pv, "--config", ro_config, vg])
    if activated.returncode != 0:
        volumes.append(
            {
                **base,
                "name": None,
                "mounted": False,
                "reason": "lvm_activation_failed",
                "detail": activated.stderr.strip()[:300],
            }
        )
        return
    listing = _run(["lvs", "--devices", pv, "--noheadings", "--separator", "|", "-o", "lv_name,lv_path", vg])
    for line in listing.stdout.splitlines():
        lv_name, _, lv_path = line.strip().partition("|")
        if not lv_path:
            continue
        fstype = _blkid_type(lv_path)
        lv_candidate = {**candidate, "device": lv_path, "part": f"{vg}-{lv_name}", "fstype": fstype}
        _mount_candidate(lv_candidate, volumes)


def _addresses() -> list[str]:
    result = _run(["ip", "-j", "-4", "addr", "show", "scope", "global"])
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return [info["local"] for iface in data for info in iface.get("addr_info", []) if info.get("local")]


def _write_result(payload: dict) -> None:
    os.makedirs(RESULT_DIR, exist_ok=True)
    tmp = f"{RESULT_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, RESULT_FILE)


def setup(config_path: str) -> int:
    with open(config_path, encoding="utf-8") as fh:
        config = json.load(fh)
    protocols = config.get("protocols") or []
    clients = config.get("allowed_clients") or []
    try:
        # Nothing else should listen while this box holds a customer's disks.
        _run(["systemctl", "disable", "--now", "ssh.service", "ssh.socket"])
        os.makedirs(SHARE_ROOT, exist_ok=True)
        candidates, missing = plan_volumes(_lsblk(), config["disks"])
        volumes: list[dict] = []
        for candidate in candidates:
            _mount_candidate(candidate, volumes)
        mounted = [v for v in volumes if v["mounted"]]
        if "smb" in protocols:
            user = config["smb_user"]
            if _run(["id", user]).returncode != 0:
                _run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", user], check=True)
            with open(SMB_CONF, "w", encoding="utf-8") as fh:
                fh.write(render_smb_conf(user, clients))
            _run(["systemctl", "enable", "--now", "smbd.service"], check=True)
            _run(["systemctl", "restart", "smbd.service"], check=True)
        if "nfs" in protocols:
            with open(EXPORTS, "w", encoding="utf-8") as fh:
                fh.write(render_exports(volumes, clients))
            _run(["systemctl", "enable", "--now", "nfs-server.service"], check=True)
            _run(["exportfs", "-ra"], check=True)
        _write_result(
            {
                "status": "ok",
                "volumes": volumes,
                "missing_disks": missing,
                "mounted_count": len(mounted),
                "addresses": _addresses(),
            }
        )
        return 0
    except Exception as exc:  # noqa: BLE001 -- report every failure instead of leaving the agent waiting
        _write_result({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:1000]})
        return 1


def set_smb_password(user: str) -> int:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", user):
        print("invalid user", file=sys.stderr)
        return 2
    password = sys.stdin.read().strip("\r\n")
    if not password:
        print("no password on stdin", file=sys.stderr)
        return 2
    result = _run(["smbpasswd", "-a", "-s", user], input_text=f"{password}\n{password}\n")
    if result.returncode != 0:
        print("smbpasswd failed", file=sys.stderr)  # never echo smbpasswd's own output (stdin echo)
        return 1
    _run(["smbpasswd", "-e", user])
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "setup":
        return setup(argv[2])
    if len(argv) == 3 and argv[1] == "set-smb-password":
        return set_smb_password(argv[2])
    print("usage: resurgam-share-helper setup <config.json> | set-smb-password <user>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
