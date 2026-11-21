from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

from resurgam_worker._subprocess import run_subprocess
from resurgam_worker.pve_client.errors import (
    GuestAgentTimeoutError,
    GuestExecTimeoutError,
    PveBinaryNotFoundError,
    PveClientError,
    PveCommandFailedError,
    PveOutputParseError,
)

_QM_BINARY = "qm"
_QEMU_IMG_BINARY = "qemu-img"
# `qm create`/`set`/`start`/`stop`/`destroy` all return once the request is
# accepted (PVE runs the actual work as its own async task) -- this is a
# local command timeout, not a "wait for the guest to boot" timeout.
_COMMAND_TIMEOUT_SECONDS = 30


def _resolve_binary(binary: str) -> str:
    binary_path = shutil.which(binary)
    if binary_path is None:
        raise PveBinaryNotFoundError(binary)
    return binary_path


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return run_subprocess(command, timeout=_COMMAND_TIMEOUT_SECONDS, on_timeout=PveCommandFailedError)


def create_vm(vmid: int, config: dict[str, str], *, pool: str | None = None, binary: str = _QM_BINARY) -> None:
    """`qm create` from a full config dict -- confirmed live (2026-09-19)
    with the mapped/overlay device attached at whatever disk slot the
    config specifies, both pointing directly at a
    `proxmox-backup-client map`-produced `/dev/loopN` device (`ro=1`)
    and at a writable qcow2 COW overlay (see `create_cow_overlay`,
    referenced via PVE's storage-relative syntax
    `<storage-id>:<vmid>/vm-<vmid>-disk-0.qcow2` -- a bare filesystem
    path is rejected outright, "unable to associate path ... to any
    storage"; a bare `/dev/loopN` is accepted directly). Both booted
    successfully (`qm status` showed `running`, real QMP blockstat
    growth); the overlay run also confirmed writes land in the overlay
    file while the backing device stays untouched.

    `config` is meant to be the *original* guest's real `qm config`
    (`pbs_client.map.fetch_original_vm_config`), not a synthetic
    from-scratch shape -- a real, decisive gap found live (2026-09-22):
    the original synthetic config (hardcoded `memory`/`cores`/`net0`/
    `scsi0` only) happened to boot a Linux guest successfully, but a
    real Windows restore fell straight into Windows's repair/recovery
    mode. It had silently dropped `machine` type, the guest's *real*
    disk controller (this guest used `sata0`, not `scsi0` -- the
    hardcoded slot assumption was simply wrong for any guest not using
    virtio-scsi), NIC model, `ostype`, real socket/core/memory counts,
    and the `smbios1` UUID. `resurgam_worker.lab_agent.daemon` now
    builds `config` by taking the original wholesale and only remapping
    the disk slot (to the overlay) and `net0`'s bridge (to the Lab
    VNet) -- see `daemon.py::_build_member_vm_config`.

    `agent` is always forced to `"1"` in `config` by the caller -- a
    real gap found live (2026-09-21): without it, QEMU never exposes the
    virtio-serial guest-agent channel at all, regardless of whether the
    guest OS has `qemu-guest-agent` installed.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "create", str(vmid)]
    for key, value in config.items():
        command += [f"--{key}", value]
    if pool is not None:
        command += ["--pool", pool]

    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


def get_vm_config(vmid: int, *, binary: str = _QM_BINARY) -> dict[str, str]:
    """`qm config <vmid>` -- the already-running guest's real, current
    config (ostype, network, etc.), for `guest_exec_live` indexing (ADR
    0021 Sec.4), which needs to know the guest's OS family to pick an
    enumeration command but never boots anything itself -- unlike
    `guest_exec_ephemeral`, which gets the same shape from
    `pbs_client.map.fetch_original_vm_config`'s PBS-side read of
    `qemu-server.conf.blob`. Same plain `key: value`-per-line format
    both ways; parsed identically here for consistency.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "config", str(vmid)]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)

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
        raise PveOutputParseError(command, result.stdout, ValueError("no config keys parsed from qm config output"))
    return config


def start_vm(vmid: int, *, binary: str = _QM_BINARY) -> None:
    """`qm start` -- confirmed live (2026-09-19): the mapped-device VM
    from `create_vm` actually started and stayed running.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "start", str(vmid)]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


def stop_vm(vmid: int, *, binary: str = _QM_BINARY) -> None:
    """`qm stop` -- confirmed live (2026-09-19) as part of this feature's
    own test cleanup.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "stop", str(vmid)]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


def destroy_vm(vmid: int, *, purge: bool = True, binary: str = _QM_BINARY) -> None:
    """`qm destroy` -- confirmed live (2026-09-19) as part of this
    feature's own test cleanup, including `--purge` (removes the VM from
    related configuration, e.g. pool membership) both times a test VM
    was torn down.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "destroy", str(vmid)]
    if purge:
        command += ["--purge"]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


def create_cow_overlay(
    backing_device: str, overlay_path: str, *, backing_format: str = "raw", binary: str = _QEMU_IMG_BINARY
) -> None:
    """`qemu-img create -f qcow2 -b <backing_device> -F <backing_format>
    <overlay_path>` -- a local, writable COW layer on top of a
    `proxmox-backup-client map`-produced read-only device, so a booting
    guest's writes (journal replay, swap, temp files) never touch the
    real PBS-served image. **Confirmed live (2026-09-19)** against a
    real PVE 9.2 node: a VM booted from this overlay (attached as
    `scsi0` under a PVE-registered "dir" storage -- see `create_vm`)
    ran normally, and `qemu-img info` on the overlay file showed its
    size growing (196KiB -> 388KiB) while the mapped backing device was
    confirmed untouched -- the real writable-restore-test mechanism
    this project uses (ADR 0019), not assumed correct.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "create", "-f", "qcow2", "-b", backing_device, "-F", backing_format, overlay_path]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


# Guest-exec File Indexing (ADR 0021). `qm guest exec`/`qm guest cmd` are
# PVE's own CLI convenience wrappers around QEMU Guest Agent's
# guest-exec/guest-exec-status/guest-file-* QMP commands -- confirmed
# live (2026-09-21/22, Recovery Lab Phase B and this feature's own
# design pass) that both already decode captured output to plain JSON
# on stdout themselves (no manual base64 decoding needed for `qm guest
# exec`'s own out-data/err-data). `guest-exec-status`'s captured output
# has an undocumented real size limit (an `out-truncated`/`err-truncated`
# flag exists for a reason) -- never relied on for a potentially large
# directory listing; the guest-side command instead writes its listing
# to a local temp file, retrieved via `guest_file_read_all` below.

# Local CLI-call timeout for the launch step only (`--synchronous 0`
# returns almost immediately, just carrying the pid) -- not how long we
# wait for the guest-side command itself, which `guest_exec`'s own
# `timeout_seconds`/`exec-status` polling loop controls separately.
_GUEST_EXEC_LAUNCH_TIMEOUT_SECONDS = 30
# How long `guest_exec`'s own polling loop waits for the guest-side
# command to finish -- generous on purpose: a real full-drive
# enumeration on a real production VM has been observed live to take
# several minutes even after exclude-pattern pruning.
_GUEST_EXEC_WAIT_TIMEOUT_SECONDS = 1800.0
_GUEST_EXEC_POLL_INTERVAL_SECONDS = 3.0
_PVESH_BINARY = "pvesh"


def guest_exec_launch(vmid: int, command_args: list[str], *, binary: str = _QM_BINARY) -> int:
    """Launches `command_args` via `qm guest exec <vmid> --synchronous 0
    -- <command_args...>` and returns just the `pid` -- fire-and-forget,
    no waiting for the guest-side command to finish at all. Extracted
    out of `guest_exec` (below) for callers that have their own,
    better-suited way to detect completion than polling `exec-status`
    (see `lab_agent.daemon._wait_for_manifest`'s docstring for why: a
    real, decisive gap found live 2026-09-25, `exec-status` itself
    proved unreliable for a long-running Windows guest command).
    """
    binary_path = _resolve_binary(binary)
    launch_command = [binary_path, "guest", "exec", str(vmid), "--synchronous", "0", "--"] + command_args
    launch_result = run_subprocess(
        launch_command, timeout=_GUEST_EXEC_LAUNCH_TIMEOUT_SECONDS, on_timeout=PveCommandFailedError
    )
    if launch_result.returncode != 0:
        raise PveCommandFailedError(launch_command, launch_result.returncode, launch_result.stderr)
    try:
        launch = json.loads(launch_result.stdout)
    except json.JSONDecodeError as exc:
        raise PveOutputParseError(launch_command, launch_result.stdout, exc) from exc
    pid = launch.get("pid")
    if not isinstance(pid, int):
        raise PveOutputParseError(
            launch_command, launch_result.stdout, ValueError("no 'pid' in guest-exec launch response")
        )
    return pid


def guest_exec(
    vmid: int,
    command_args: list[str],
    *,
    timeout_seconds: float = _GUEST_EXEC_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _GUEST_EXEC_POLL_INTERVAL_SECONDS,
    binary: str = _QM_BINARY,
) -> dict:
    """Launches `command_args` (via `guest_exec_launch`) then polls `qm
    guest exec-status <vmid> <pid>` until it reports `exited`, returning
    PVE's own decoded `{"exitcode": int, "exited": int, "out-data": str,
    "err-data": str, ...}` shape. Only `exitcode` should ever be trusted
    for a large-output command (see module docstring above) -- callers
    needing the actual output should have the guest command write to a
    file and retrieve it via `guest_file_read_all` instead.

    Reliable for fast/normal commands (every diagnostic call made this
    session used it successfully). **Not** reliable for a genuinely
    long-running command on a real, busy Windows guest: a live,
    decisive case (2026-09-25) had `exec-status` still reporting
    `exited: 0` (or erroring) for 20+ minutes *after* the guest-side
    script had already finished and written its own completion marker
    (the file-index enumeration's manifest) -- QGA's own pid-exit
    tracking simply never caught up. `lab_agent.daemon` no longer uses
    this function for the enumeration dispatch for exactly this reason
    -- it uses `guest_exec_launch` plus its own `_wait_for_manifest`
    poll loop instead, which waits for proof of the *actual thing we
    care about* (the output existing) rather than trusting QGA's own,
    apparently unreliable, opinion about process exit state.

    A real, decisive gap found live (2026-09-25): the previous version
    of this function called plain `qm guest exec <vmid> -- ...` and
    trusted it to block until completion, on the assumption (stated in
    this ADR's own design notes) that `qm guest exec` is "PVE's own
    blocking wrapper around guest-exec+polling guest-exec-status." That
    assumption is only true for *fast* commands -- `qm help guest exec`
    reveals its real default is `--synchronous 1 --timeout 30`: it waits
    synchronously for at most 30 seconds, and past that returns just
    `{"pid": N}` while the guest-side command keeps running, completely
    unsupervised, in the background -- `guest_exec_launch`'s explicit
    `--synchronous 0` plus this function's own `exec-status` polling
    loop closes that gap for commands where polling *is* reliable.

    A separate, real gap found live in this same polling loop
    (2026-09-25): an individual `exec-status` poll can itself fail with
    a transient QGA-level error ("got timeout") -- treated the same as
    "not finished yet" (not raised) as long as the overall
    `timeout_seconds` deadline hasn't passed, matching
    `guest_file_read_all`'s own retry tolerance for the same class of
    hiccup; only a poll that fails *and* is past the deadline raises.
    """
    pid = guest_exec_launch(vmid, command_args, binary=binary)
    binary_path = _resolve_binary(binary)

    status_command = [binary_path, "guest", "exec-status", str(vmid), str(pid)]
    deadline = time.monotonic() + timeout_seconds
    last_poll_error: PveCommandFailedError | None = None
    while True:
        status_result = _run(status_command)
        if status_result.returncode == 0:
            try:
                status = json.loads(status_result.stdout)
            except json.JSONDecodeError as exc:
                raise PveOutputParseError(status_command, status_result.stdout, exc) from exc
            if status.get("exited"):
                return status
        else:
            last_poll_error = PveCommandFailedError(status_command, status_result.returncode, status_result.stderr)
        if time.monotonic() >= deadline:
            if last_poll_error is not None:
                raise last_poll_error
            raise GuestExecTimeoutError(vmid, pid, timeout_seconds)
        time.sleep(poll_interval_seconds)


def _guest_cmd(vmid: int, qga_command: str, args: dict | None = None, *, binary: str = _QM_BINARY) -> object:
    """`qm guest cmd <vmid> <qga_command> [<json-args>]` -- PVE's generic
    pass-through for any QEMU Guest Agent command (confirmed live this
    session for `network-get-interfaces`, which returned its result as
    a bare JSON array, no `{"return": ...}` envelope). The exact wire
    shape is **not yet live-confirmed for every command this module
    calls** (`guest-file-open`/`-read`/`-close` specifically) -- callers
    handle both a bare value and a `{"return": ...}`-wrapped one
    defensively rather than assuming either.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "guest", "cmd", str(vmid), qga_command]
    if args:
        command.append(json.dumps(args))
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PveOutputParseError(command, result.stdout, exc) from exc


def _unwrap(response: object) -> object:
    if isinstance(response, dict) and "return" in response:
        return response["return"]
    return response


def guest_file_read_all(
    vmid: int,
    path: str,
    *,
    node: str,
    retries: int = 20,
    retry_delay_seconds: float = 5.0,
    binary: str = _PVESH_BINARY,
) -> str:
    """Reads an entire guest-local file via PVE's own REST API:
    `pvesh get /nodes/<node>/qemu/<vmid>/agent/file-read --file <path>`.

    A real, decisive correction found live (2026-09-24): `qm guest cmd`
    does NOT expose QEMU Guest Agent's raw `guest-file-open`/
    `guest-file-read`/`guest-file-close` at all -- confirmed against its
    own real command whitelist (`qm help guest cmd`): `fsfreeze-*`,
    `fstrim`, `get-*`, `info`, `network-get-interfaces`, `ping`,
    `shutdown`, `suspend-*`. A first version of this function built on
    that non-existent command family failed every real call ("400 too
    many arguments"). PVE's REST layer (unlike its own CLI's narrower
    `qm guest cmd` wrapper) exposes a higher-level `file-read` that
    internally handles the whole open/read-loop/close sequence in one
    call and hands back already-decoded `content` directly (confirmed
    live: a real Windows guest file read back correctly, no base64
    decoding needed on this side). `pvesh` is still a local CLI tool
    (talks to PVE's own API over its local socket), not a remote HTTP
    call -- consistent with this module's "local tools only" precedent.

    `node` is required here (unlike every other function in this
    module) because `pvesh`'s REST path is node-scoped, while `qm`'s own
    commands resolve a vmid cluster-wide on their own.

    A real, decisive ceiling confirmed live (2026-09-25): PVE's own
    `file-read` fails deterministically (`"got timeout"`, QGA-level) on a
    44MB file, independent of retry count -- 3, 8, and 40 retries all
    exhausted identically, and a clean re-run ruled out an unrelated
    network confound. `pvesh get /nodes/<node>/qemu/<vmid>/agent` was
    checked directly and carries no lower-level chunked-read primitive
    (no offset-based `file-open`/`file-read`/`file-close`) to fall back
    to -- this is a genuine, non-configurable, undocumented ceiling, not
    a transient lock that a bigger retry budget could wait out. The real
    fix is the caller never handing this function a large file in the
    first place: `lab_agent.file_index.build_enumeration_command` now
    writes many small chunk files instead of one big listing, and
    `lab_agent.daemon._read_and_submit_chunks` reads them one at a time.

    Retries on failure (`retries`/`retry_delay_seconds`) still matter for
    two real, separate, live-found cases: (2026-09-24) Windows
    Defender's real-time scan holding a just-written file locked briefly
    after `guest_exec`'s own command already exited, and (2026-09-25,
    after the chunking fix landed) an isolated small chunk (under 1MB,
    no larger than dozens of neighboring chunks that read successfully)
    still occasionally hitting the same `"got timeout"` QGA error on a
    real, actively-used production VM -- live-confirmed not a hard
    size-based ceiling (74 chunks up to ~1.1MB each were produced by one
    real run; the one that failed wasn't the largest), but real,
    occasional guest-side contention that a plain per-chunk retry rides
    out. The budget here (20 attempts/~100s) is a middle ground: lighter
    than the original single-big-file version (40 attempts/~200s, tuned
    for a since-disproven "hard ceiling" theory) since most chunks never
    need more than one attempt, but generous enough that one unlucky
    chunk out of dozens doesn't fail an otherwise-complete run.
    """
    binary_path = _resolve_binary(binary)
    command = [
        binary_path,
        "get",
        f"/nodes/{node}/qemu/{vmid}/agent/file-read",
        "--file",
        path,
        "--output-format",
        "json",
    ]

    last_error: PveCommandFailedError | None = None
    for attempt in range(retries):
        result = _run(command)
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise PveOutputParseError(command, result.stdout, exc) from exc
            content = data.get("content")
            if not isinstance(content, str):
                raise PveOutputParseError(
                    command, result.stdout, ValueError("no 'content' string in file-read response")
                )
            return content
        last_error = PveCommandFailedError(command, result.returncode, result.stderr)
        if attempt < retries - 1:
            time.sleep(retry_delay_seconds)
    assert last_error is not None
    raise last_error


def guest_ping(vmid: int, *, binary: str = _QM_BINARY) -> bool:
    """`qm guest cmd <vmid> ping` -- QGA's no-op liveness command: True when
    the guest agent answered. PVE names it `ping` (its `qm guest cmd`
    command list has no `guest-` prefix) and prints nothing on success, so
    only the exit status counts. Found live 2026-11-21: the former
    `qm guest cmd <vmid> guest-ping` is rejected by PVE ("value 'guest-ping'
    does not have a value in the enumeration") on every call, so every wait
    ran into its timeout and no Disk Mount helper ever became ready."""
    binary_path = _resolve_binary(binary)
    try:
        return _run([binary_path, "guest", "cmd", str(vmid), "ping"]).returncode == 0
    except PveClientError:  # e.g. the call itself timed out
        return False


def wait_for_guest_agent(vmid: int, *, timeout_seconds: float = 180.0, poll_interval_seconds: float = 2.0) -> None:
    """Polls `guest_ping` until the guest agent answers or `timeout_seconds`
    elapses -- needed only for a freshly booted clone (agent readiness
    unknown: ephemeral indexing, the Disk Mounts helper), never for an
    already-running production VM. A guest whose virtio-serial channel isn't
    attached yet fails the same way a guest that's still booting does; the
    timeout is the backstop either way."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if guest_ping(vmid):
            return
        if time.monotonic() >= deadline:
            raise GuestAgentTimeoutError(vmid, timeout_seconds)
        time.sleep(poll_interval_seconds)


# --- Disk Mounts helper VM (ADR 0025) ----------------------------------------
# qm(1) (pve.proxmox.com/pve-docs/qm.1.html, checked 2026-10-08):
# `qm clone <vmid> <newid> [--full] [--name] [--description] [--storage]`
# ("--storage: Target storage for full clone"; a template clones linked by
# default where the storage supports it), `qm set --delete <list>`,
# `qm guest exec ... --pass-stdin 1` ("read STDIN until EOF and forward to
# guest agent via input-data ... maximal 1 MiB").

_CLONE_TIMEOUT_SECONDS = 1800


def next_free_vmid(*, binary: str = _PVESH_BINARY) -> int:
    """`pvesh get /cluster/nextid` -- PVE's own next free VMID (the same call
    the backend's template build uses with `--vmid`)."""
    binary_path = _resolve_binary(binary)
    command = [binary_path, "get", "/cluster/nextid", "--output-format", "json"]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)
    try:
        return int(json.loads(result.stdout))
    except (ValueError, TypeError) as exc:
        raise PveOutputParseError(command, result.stdout, exc) from exc


def list_cluster_vmids(*, binary: str = _PVESH_BINARY) -> set[int]:
    """Every VMID in the cluster (`pvesh get /cluster/resources --type vm`), to
    tell "already gone" from "exists" without parsing localized error text."""
    binary_path = _resolve_binary(binary)
    command = [binary_path, "get", "/cluster/resources", "--type", "vm", "--output-format", "json"]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)
    try:
        return {int(entry["vmid"]) for entry in json.loads(result.stdout) if "vmid" in entry}
    except (ValueError, TypeError, KeyError) as exc:
        raise PveOutputParseError(command, result.stdout, exc) from exc


def clone_vm(
    source_vmid: int,
    target_vmid: int,
    *,
    name: str,
    description: str | None = None,
    full: bool = False,
    storage: str | None = None,
    binary: str = _QM_BINARY,
) -> None:
    """`qm clone`. Linked (default for a template) unless `full`; `storage` only
    applies to a full clone. Waits for the clone to finish (the CLI does)."""
    binary_path = _resolve_binary(binary)
    command = [binary_path, "clone", str(source_vmid), str(target_vmid), "--name", name]
    if description:
        command += ["--description", description]
    if full:
        command += ["--full", "1"]
        if storage:
            command += ["--storage", storage]
    result = run_subprocess(command, timeout=_CLONE_TIMEOUT_SECONDS, on_timeout=PveCommandFailedError)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


def set_vm_options(vmid: int, options: dict[str, str], *, binary: str = _QM_BINARY) -> None:
    """`qm set <vmid> --key value ...`."""
    binary_path = _resolve_binary(binary)
    command = [binary_path, "set", str(vmid)]
    for key, value in options.items():
        command += [f"--{key}", value]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


def delete_vm_options(vmid: int, keys: list[str], *, binary: str = _QM_BINARY) -> None:
    """`qm set <vmid> --delete a,b` -- used to detach the mapped `/dev/loopN`
    drives before `qm destroy`, so destroy never handles a path PVE does not own."""
    if not keys:
        return
    binary_path = _resolve_binary(binary)
    command = [binary_path, "set", str(vmid), "--delete", ",".join(keys)]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)


def guest_exec_with_stdin(
    vmid: int, command_args: list[str], stdin_text: str, *, timeout_seconds: int = 60, binary: str = _QM_BINARY
) -> dict:
    """`qm guest exec <vmid> --pass-stdin 1 --timeout N -- <args>` with `stdin_text`
    on stdin -- for a secret that must never be part of a command line (QEMU's
    guest agent logs the argv of every guest-exec, not its input-data; seen in
    the helper's journal in the local QEMU check, 2026-10-08). Returns PVE's
    decoded `{"exitcode": .., "exited": .., ...}`. Errors never include
    `stdin_text`."""
    binary_path = _resolve_binary(binary)
    command = [
        binary_path,
        "guest",
        "exec",
        str(vmid),
        "--pass-stdin",
        "1",
        "--timeout",
        str(timeout_seconds),
        "--",
        *command_args,
    ]
    result = run_subprocess(
        command, timeout=timeout_seconds + 30, on_timeout=PveCommandFailedError, input_text=stdin_text
    )
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PveOutputParseError(command, "(output withheld)", exc) from exc
    if not isinstance(payload, dict):
        raise PveOutputParseError(command, "(output withheld)", ValueError("not a JSON object"))
    return payload


# `qm disk import` copies the whole source; EFI vars and TPM state are a few
# MiB, but a slow storage still gets a generous budget.
_IMPORT_TIMEOUT_SECONDS = 15 * 60


def import_disk(
    vmid: int,
    source_path: str,
    storage: str,
    *,
    target_disk: str | None = None,
    disk_format: str | None = None,
    binary: str = _QM_BINARY,
) -> str:
    """`qm disk import <vmid> <source> <storage> [--target-disk <slot>]` -- copies a
    regular file into a new volume on `storage` (qemu-server `qm.pm` `importdisk`:
    the source must be a regular file). With `target_disk` the volume is attached
    at that slot and the slot's new config value is returned
    (`local-lvm:vm-123-disk-0,size=4M`); without it the volume lands as the next
    `unusedN` and that entry's value (the bare volume id) is returned.

    Instant Recovery (ADR 0030 §3) imports `efidisk0` and `tpmstate0` without a
    target slot and attaches them afterwards with the original options in one
    `qm set`. Confirmed live 2026-11-20 (PVE 9.2): importing straight into
    `tpmstate0` attaches it as TPM v1.2 (the default), and the following
    `qm set --tpmstate0 <same volume>,version=v2.0` is refused ("cannot change
    TPM state version after creation", `Qemu.pm` `prohibit_tpm_version_change`,
    which only fires when the slot already holds that same volume). Attaching
    an `unusedN` volume is a new drive for PVE, and PVE drops the `unusedN`
    entry itself once the volume is in use (`QemuServer.pm` write_config)."""
    binary_path = _resolve_binary(binary)
    command = [binary_path, "disk", "import", str(vmid), source_path, storage]
    if target_disk:
        command += ["--target-disk", target_disk]
    if disk_format:
        command += ["--format", disk_format]
    before = {} if target_disk else get_vm_config(vmid, binary=binary)
    result = run_subprocess(command, timeout=_IMPORT_TIMEOUT_SECONDS, on_timeout=PveCommandFailedError)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)
    after = get_vm_config(vmid, binary=binary)
    if target_disk:
        value = after.get(target_disk)
        if not value:
            raise PveOutputParseError(command, result.stdout, ValueError(f"{target_disk} is not set after the import"))
        return value
    new_unused = [
        value for key, value in after.items() if re.fullmatch(r"unused\d+", key) and value not in before.values()
    ]
    if len(new_unused) != 1:
        raise PveOutputParseError(
            command, result.stdout, ValueError(f"expected one new unusedN entry after the import, found {new_unused}")
        )
    return new_unused[0]


def vm_status(vmid: int, *, binary: str = _QM_BINARY) -> str | None:
    """`qm status <vmid>` -> `running` / `stopped` / ..., or None when the VM
    does not exist (`qm` exits non-zero with "does not exist")."""
    binary_path = _resolve_binary(binary)
    command = [binary_path, "status", str(vmid)]
    result = _run(command)
    if result.returncode != 0:
        if "does not exist" in (result.stderr or ""):
            return None
        raise PveCommandFailedError(command, result.returncode, result.stderr)
    _, _, status = result.stdout.strip().partition(":")
    return status.strip() or None
