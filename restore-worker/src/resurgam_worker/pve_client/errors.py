class PveClientError(Exception):
    """Base class for all resurgam_worker.pve_client errors."""


class PveBinaryNotFoundError(PveClientError):
    def __init__(self, binary_name: str) -> None:
        self.binary_name = binary_name
        super().__init__(f"{binary_name} not found on PATH")


class PveCommandFailedError(PveClientError):
    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"command {command[0]!r} exited with code {returncode}: {stderr.strip() or '(no stderr)'}")


class PveOutputParseError(PveClientError):
    def __init__(self, command: list[str], output: str, cause: Exception) -> None:
        self.command = command
        self.output = output
        self.cause = cause
        super().__init__(f"failed to parse output of {command[0]!r} as expected JSON shape: {cause}")


class GuestAgentTimeoutError(PveClientError):
    def __init__(self, vmid: int, timeout_seconds: float) -> None:
        self.vmid = vmid
        self.timeout_seconds = timeout_seconds
        super().__init__(f"guest agent on VM {vmid} did not become reachable within {timeout_seconds:.0f}s")


class GuestExecTimeoutError(PveClientError):
    """A real, decisive gap found live (2026-09-25): `qm guest exec`'s
    own default (`--synchronous 1 --timeout 30`) only blocks for 30s --
    past that it returns just `{"pid": N}` while the guest-side command
    keeps running *unsupervised* in the background. `guest_exec` now
    polls `exec-status` explicitly instead of trusting `qm`'s own wait,
    and raises this if the guest command still hasn't finished within
    its own, much longer budget -- at which point the guest-side
    process is still running, just no longer being waited on by us; the
    caller has no more (or less) control over it than before.
    """

    def __init__(self, vmid: int, pid: int, timeout_seconds: float) -> None:
        self.vmid = vmid
        self.pid = pid
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"guest-exec pid {pid} on VM {vmid} did not finish within {timeout_seconds:.0f}s "
            "(the guest-side process may still be running)"
        )
