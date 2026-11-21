from __future__ import annotations

import json
import re
import shutil

from resurgam_worker.pbs_client._subprocess import materialized_keyfile
from resurgam_worker.pbs_client._subprocess import run as _run
from resurgam_worker.pbs_client.errors import (
    InvalidPathError,
    PbsBinaryNotFoundError,
    PbsCommandFailedError,
    PbsOutputParseError,
)
from resurgam_worker.pbs_client.models import MappedImage

_DEFAULT_BINARY = "proxmox-backup-client"

# Confirmed live (2026-09-19) against a real PBS 4.2 instance:
# "Image '<repo>:<datastore>:<snapshot>/<archive>' mapped on /dev/loop0"
# -- but confirmed live again (2026-09-21, real end-to-end Recovery Lab
# run) that this line is written to the process's *stderr*, not stdout;
# the original code only ever searched stdout, so a genuinely successful
# map always looked like a parse failure. See `map_snapshot` below.
_MAP_OUTPUT_PATTERN = re.compile(r"mapped on (/dev/\S+)")

_SYSTEMD_RUN_BINARY = "systemd-run"
# What `map_snapshot` accepts as a scope name: letters, digits, `-`, `_`, `.`,
# without the `.scope` suffix systemd adds itself.
_SCOPE_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}")
# A restore of a whole disk image (EFI vars, TPM state) can take a while on a
# slow link; both are a few MiB, but the budget is generous.
_IMAGE_RESTORE_TIMEOUT_SECONDS = 15 * 60


def _resolve_binary(binary: str) -> str:
    binary_path = shutil.which(binary)
    if binary_path is None:
        raise PbsBinaryNotFoundError(binary)
    return binary_path


def map_snapshot(
    repository: str,
    snapshot: str,
    archive_name: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    scope_unit: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> MappedImage:
    """Map one disk archive from a snapshot as a local block device via
    `proxmox-backup-client map` -- zero-copy, no restore, confirmed live
    (2026-09-19): a real 130MB VM disk archive mapped as `/dev/loop0`, a
    genuine readable MBR disk (`fdisk -l` confirmed a real partition
    table). `snapshot` takes no namespace prefix -- confirmed live that a
    naively-prefixed path (`f"{namespace}:{snapshot}"`) 400s; namespace is
    always the separate `--ns` flag, matching every other PBS client call
    in this codebase.

    No `--port` flag -- deliberately, confirmed live (2026-09-21): PBS's
    CLI rejects `--repository` combined with `--port` outright
    ("mutually exclusive"). The port belongs inside `repository` itself
    (`[[user@]server[:port]:]datastore`, see `lab_agent.daemon._pbs_repository`),
    not as a separate flag here.

    `keyfile`, when given, is a PBS encryption keyfile's raw content (not
    a path -- matches how `token` is already passed as content, not a
    file reference). Confirmed live (2026-09-25) that `map` accepts
    `--keyfile <path>` directly; materialized to a restricted-permission
    temp file only for the duration of this one call
    (`_subprocess.materialized_keyfile`), see docs/adr/0022. v1 always
    passes it whenever the caller has one configured for this datastore,
    rather than pre-checking this specific snapshot's own `crypt-mode` --
    see the ADR for why, and for the one thing this hasn't been
    live-verified against yet (an unnecessary `--keyfile` on an
    unencrypted snapshot).

    `scope_unit`, when given, starts the call in its own transient systemd
    scope (`systemd-run --scope --unit=<scope_unit>`). `map` forks a
    background process that serves the device; without a scope that process
    stays in the caller's cgroup, and `resurgam-lab-agent.service`'s default
    `KillMode=control-group` would kill it -- and with it the disk of a VM
    running from the backup -- whenever the agent restarts (Instant Recovery,
    ADR 0030 §6). A scope keeps the caller's environment (`PBS_PASSWORD`) and
    stdio, so the "mapped on" line is parsed exactly as without one.
    """
    binary_path = _resolve_binary(binary)
    if not snapshot:
        raise InvalidPathError(snapshot, "snapshot must not be empty")
    if not archive_name:
        raise InvalidPathError(archive_name, "archive_name must not be empty")
    if scope_unit is not None and not _SCOPE_UNIT_RE.fullmatch(scope_unit):
        raise InvalidPathError(scope_unit, "scope_unit must be a plain systemd unit name")

    command = [binary_path, "map", snapshot, archive_name, "--repository", repository]
    if scope_unit is not None:
        command = [
            _resolve_binary(_SYSTEMD_RUN_BINARY),
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={scope_unit}",
            *command,
        ]
    if namespace:
        command += ["--ns", namespace]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, token=token)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)

    # The "mapped on ..." line lands on stderr in real PBS client builds
    # tested here (confirmed live 2026-09-21), not stdout -- search
    # stdout first for forward-compatibility with a version that moves
    # it back, then fall back to stderr.
    match = _MAP_OUTPUT_PATTERN.search(result.stdout) or _MAP_OUTPUT_PATTERN.search(result.stderr)
    if not match:
        combined = f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        raise PbsOutputParseError(command, combined, ValueError("no device path found in map output"))
    return MappedImage(device_path=match.group(1))


def unmap(device_path_or_name: str, *, token: str | None = None, binary: str = _DEFAULT_BINARY) -> None:
    """Unmap a previously-mapped image via `proxmox-backup-client unmap`
    -- confirmed live (2026-09-19): cleanly released a real `/dev/loop0`
    mapping, verified absent afterward via `losetup -a`. Accepts either
    the device path (`/dev/loop0`) or the archive name `map_snapshot`
    used, per the real CLI's own `unmap [<name>]` usage.
    """
    if not device_path_or_name:
        raise InvalidPathError(device_path_or_name, "device_path_or_name must not be empty")

    binary_path = _resolve_binary(binary)
    command = [binary_path, "unmap", device_path_or_name]
    result = _run(command, token=token)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)


def list_snapshot_archives(
    repository: str,
    snapshot: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> list[str]:
    """List the disk archive names present in a snapshot (e.g.
    `drive-scsi0.img.fidx`, `drive-scsi1.img.fidx` for a multi-disk VM)
    via `proxmox-backup-client snapshot files`, so `resurgam-lab-agent`
    never has to guess/hardcode an archive name before mapping it.

    No `--port` flag -- see `map_snapshot`'s docstring: PBS's CLI
    rejects `--repository` combined with `--port`, confirmed live
    (2026-09-21) against a real Recovery Lab run. Embed the port in
    `repository` itself instead.

    Live-verified (2026-09-21) against a real multi-disk-capable VM
    snapshot -- the earlier "not yet live-verified" caveat is resolved.

    `keyfile` -- see `map_snapshot`'s docstring; `snapshot files` just
    lists filenames/sizes/`crypt-mode`, not archive content, so whether
    this specific call actually needs a key is unconfirmed, but passing
    it costs nothing extra and keeps every call in this module consistent.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "snapshot", "files", snapshot, "--repository", repository, "--output-format", "json"]
    if namespace:
        command += ["--ns", namespace]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, token=token)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)
    try:
        raw = json.loads(result.stdout)
        return [
            entry["filename"]
            for entry in raw
            if isinstance(entry, dict) and str(entry.get("filename", "")).endswith(".img.fidx")
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PbsOutputParseError(command, result.stdout, exc) from exc


def fetch_original_vm_config(
    repository: str,
    snapshot: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> dict[str, str]:
    """Every PBS VM backup carries its own real `qm config` as
    `qemu-server.conf.blob` -- fetched here via
    `proxmox-backup-client restore <snapshot> qemu-server.conf.blob -`
    (streamed to stdout). A real, decisive gap found live (2026-09-22):
    the first working Recovery Lab boot used a synthetic from-scratch VM
    config (`pve_client.qm.create_vm`'s original shape -- memory/cores/
    net0/scsi0 only) and happened to work for that one guest, but a real
    Windows restore fell straight into Windows's repair/recovery mode --
    the synthetic config silently dropped `machine` type, the real disk
    *controller* (this guest used `sata0`, not `scsi0` -- `create_vm`'s
    hardcoded `scsi0` assumption was simply wrong), NIC model, `ostype`,
    real socket/core/memory counts, and the `smbios1` UUID (Windows
    activation ties to this). Windows is far more sensitive to this kind
    of boot-environment mismatch than the Linux guest the synthetic
    config happened to boot successfully. `resurgam_worker.lab_agent.daemon`
    now reconstructs the lab VM from this real config wholesale, only
    remapping the disk slot (to the overlay) and `net0`'s bridge (to the
    Lab VNet) -- see `daemon.py::_build_member_vm_config`.

    Parses PVE's plain `key: value`-per-line config format, skipping
    blank lines, comment lines (`#...`), and the trailing
    `#qmdump#map:...` line `qmrestore` itself uses (not needed here --
    the target disk slot is derived from the archive filename instead,
    see `daemon.py`).

    `keyfile` -- see `map_snapshot`'s docstring.
    """
    binary_path = _resolve_binary(binary)
    if not snapshot:
        raise InvalidPathError(snapshot, "snapshot must not be empty")

    command = [binary_path, "restore", snapshot, "qemu-server.conf.blob", "-", "--repository", repository]
    if namespace:
        command += ["--ns", namespace]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, token=token)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)

    config: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        config[key.strip()] = value.strip()
    if not config:
        raise PbsOutputParseError(
            command, result.stdout, ValueError("no config keys parsed from qemu-server.conf.blob")
        )
    return config


# `proxmox-backup-client unmap` without a name lists the current mappings --
# PBS source (proxmox-backup-client/src/mount.rs, `unmap`, checked
# 2026-10-08): one `log::info!("{}:\t{}", loopdev, name)` line per mapping
# ("(unmapped)" for a stale one), or "Nothing mapped."; `name` is
# `{repo}:{snapshot path}/{archive without .fidx}` (same file, `mount_do`).
# Not live-verified on PBS 4.x yet. Written to stderr like `map`'s own
# "mapped on" line, so both streams are parsed.
_MAPPING_LINE = re.compile(r"^(/dev/loop\d+):\s+(\S.*)$")


def list_mappings(*, binary: str = _DEFAULT_BINARY) -> dict[str, str]:
    """`{"/dev/loop3": "<repo>:vm/116/2026-...Z/drive-scsi0.img", ...}` -- what is
    mapped right now, so a teardown only unmaps a device still bound to *its*
    snapshot (a loop number can be reused by another mapping after a reboot)."""
    binary_path = _resolve_binary(binary)
    command = [binary_path, "unmap"]
    result = _run(command)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)
    mappings: dict[str, str] = {}
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        match = _MAPPING_LINE.match(line.strip())
        if match:
            mappings[match.group(1)] = match.group(2).strip()
    return mappings


def restore_image(
    repository: str,
    snapshot: str,
    archive_name: str,
    target_path: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> None:
    """Restores one disk image archive (`drive-efidisk0.img.fidx`,
    `drive-tpmstate0-backup.img.fidx`) as a raw file via
    `proxmox-backup-client restore <snapshot> <archive> <target_path>`.

    Instant Recovery (ADR 0030 §3) needs EFI vars and TPM state as real files:
    `qm disk import` refuses anything that is not a regular file ("non-existent
    or non-regular file", qemu-server `qm.pm` `importdisk`), so a mapped loop
    device can't be imported, and PVE won't move TPM state while the VM runs.
    PVE's own live-restore restores exactly these two drives before the start
    too (`QemuServer.pm`, `restore_proxmox_backup_archive`)."""
    binary_path = _resolve_binary(binary)
    if not snapshot:
        raise InvalidPathError(snapshot, "snapshot must not be empty")
    if not archive_name.endswith(".img.fidx"):
        raise InvalidPathError(archive_name, "archive_name must be a disk image archive (*.img.fidx)")
    if not target_path.startswith("/"):
        raise InvalidPathError(target_path, "target_path must be an absolute path")

    command = [binary_path, "restore", snapshot, archive_name, target_path, "--repository", repository]
    if namespace:
        command += ["--ns", namespace]
    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, token=token, timeout=_IMAGE_RESTORE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)
