class PbsClientError(Exception):
    """Base class for all resurgam_worker.pbs_client errors."""


class PbsBinaryNotFoundError(PbsClientError):
    def __init__(self, binary_name: str) -> None:
        self.binary_name = binary_name
        super().__init__(f"{binary_name} not found on PATH")


class PbsCommandFailedError(PbsClientError):
    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"command {command[0]!r} exited with code {returncode}: {stderr.strip() or '(no stderr)'}")


class PbsOutputParseError(PbsClientError):
    def __init__(self, command: list[str], raw_output: str, cause: Exception) -> None:
        self.command = command
        self.raw_output = raw_output
        self.cause = cause
        super().__init__(f"failed to parse output of {command[0]!r} as expected JSON shape: {cause}")


class InvalidPathError(PbsClientError):
    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"invalid path {path!r}: {reason}")


class PathNotMountableError(PbsClientError):
    """`proxmox-file-restore list` exits 0 but returns a `{"code": ...,
    "message": ...}` error object (instead of an array of entries) when a
    partition can't be mounted -- confirmed live against a real Windows VM
    backup's EFI/recovery partition ("all mounts failed or no supported
    file system"). Distinct from `PbsOutputParseError`: this is not
    malformed output, it's PBS's own documented way of saying "not
    browsable," which callers can legitimately skip rather than fail on.
    """

    def __init__(self, path: str, pbs_message: str) -> None:
        self.path = path
        self.pbs_message = pbs_message
        super().__init__(f"path {path!r} could not be mounted: {pbs_message}")


class SelectionTooLargeError(PbsClientError):
    """A multi-entry archive selection whose combined restore failed and that is too large to
    retry entry by entry (see `catalog.restore_selection`)."""

    def __init__(self, count: int, limit: int, stderr: str) -> None:
        self.count = count
        self.limit = limit
        self.stderr = stderr
        super().__init__(
            f"restoring the {count} selected entries failed, and a selection this large ({count} > {limit}) is not "
            f"retried entry by entry: {stderr.strip() or '(no stderr)'}"
        )
