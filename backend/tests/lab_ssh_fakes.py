"""A fake SSH layer for the guided lab setup tests -- no socket is opened.

`FakeSshBackend` replaces `resurgam_api.lab_setup.ssh.ssh_backend`. It mimics
the real backend's contract: `fetch_host_key` never sees credentials;
`connect` compares the presented host key with the confirmed fingerprint
*before* looking at the credentials (`auth_attempts` records whether any
credential was ever "sent"). Commands are answered by `responder(command)`
(first matching regex wins) and recorded with every upload.
"""

import json
import re
from dataclasses import dataclass, field

from resurgam_api.lab_setup.ssh import CommandResult, HostKeyInfo, SshError

FINGERPRINT = "SHA256:" + "Q" * 43
OTHER_FINGERPRINT = "SHA256:" + "Z" * 43


@dataclass
class Upload:
    remote_path: str
    data: bytes
    mode: int


@dataclass
class FakeSession:
    backend: "FakeSshBackend"
    closed: bool = False

    def run(self, command: str, *, timeout: float) -> CommandResult:
        self.backend.commands.append(command)
        for pattern, result in self.backend.responses:
            if re.search(pattern, command):
                value = result(command) if callable(result) else result
                if isinstance(value, Exception):
                    raise value
                return value
        return CommandResult(0, "", "")

    def put_bytes(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None:
        self.backend.uploads.append(Upload(remote_path, data, mode))

    def put_file(self, local_path: str, remote_path: str, *, mode: int = 0o644) -> None:
        with open(local_path, "rb") as handle:
            self.backend.uploads.append(Upload(remote_path, handle.read(), mode))

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeSshBackend:
    fingerprint: str = FINGERPRINT
    key_type: str = "ssh-ed25519"
    accept_password: str | None = None
    accept_key: str | None = None
    responses: list = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    uploads: list[Upload] = field(default_factory=list)
    auth_attempts: list[str] = field(default_factory=list)
    host_key_calls: list = field(default_factory=list)
    sessions: list[FakeSession] = field(default_factory=list)

    def install(self, monkeypatch) -> "FakeSshBackend":
        from resurgam_api.lab_setup import ssh as ssh_module

        monkeypatch.setattr(ssh_module, "ssh_backend", self)
        return self

    def respond(self, pattern: str, result) -> "FakeSshBackend":
        self.responses.insert(0, (pattern, result))
        return self

    def fetch_host_key(self, target, *, timeout: float) -> HostKeyInfo:
        self.host_key_calls.append((target.host, target.port))
        return HostKeyInfo(key_type=self.key_type, fingerprint_sha256=self.fingerprint)

    def connect(self, target, credentials, *, expected_fingerprint: str, timeout: float) -> FakeSession:
        if expected_fingerprint != self.fingerprint:
            raise SshError("host_key_mismatch", f"The node's SSH host key changed: it now presents {self.fingerprint}.")
        if credentials.private_key:
            self.auth_attempts.append("private_key")
            if credentials.private_key != self.accept_key:
                raise SshError("auth_failed", "The node rejected the login.")
        else:
            self.auth_attempts.append("password")
            if credentials.password != self.accept_password:
                raise SshError("auth_failed", "The node rejected the login.")
        session = FakeSession(self)
        self.sessions.append(session)
        return session


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def fail(stderr: str = "error", status: int = 1) -> CommandResult:
    return CommandResult(status, "", stderr)


def guest_exec(payload: dict | None, exitcode: int = 0) -> CommandResult:
    """What `qm guest exec` prints for a finished command."""
    out = {"exitcode": exitcode, "exited": 1}
    if payload is not None:
        out["out-data"] = json.dumps(payload) + "\n"
    return ok(json.dumps(out, indent=3))
