"""What a build needs from its caller: a shell on the node and a step log."""

from __future__ import annotations

from typing import Any, Protocol


class CommandResult(Protocol):
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool: ...

    def combined(self) -> str: ...


class Session(Protocol):
    def put_bytes(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None: ...

    def put_file(self, local_path: str, remote_path: str, *, mode: int = 0o644) -> None: ...


class Shell(Protocol):
    """Runs commands on the node: `RemoteShell` (SSH) or the agent's `LocalShell`."""

    session: Session
    privilege_mode: str  # "root" | "sudo"

    def run(self, command: str, *, privileged: bool = True, timeout: float = 120.0) -> CommandResult: ...


class BuildLog(Protocol):
    """The job's step log: the backend's `JobLog`, or the agent's reporter."""

    def start(self, key: str, message: str) -> Any: ...

    def finish(self, step: Any, status: str, message: str | None = None, output: str | None = None) -> None: ...

    def note(self, key: str, status: str, message: str, output: str | None = None) -> None: ...

    def set_result(self, **values: Any) -> None: ...


class PackageFile(Protocol):
    """A .deb shipped with RESURGAM (the backend's `LabArtifact`, or the
    agent's downloaded copy)."""

    filename: str
    version: str
    sha256: str
    path: Any
