"""One-time SSH sessions to a lab PVE node (guided agent install and
template build).

Security rules, all enforced here:

- **No auto-accept.** `fetch_host_key` connects *without authenticating*
  and returns the server's host key fingerprint (OpenSSH format,
  `SHA256:<base64, no padding>` -- what `ssh-keygen -lf` prints). The admin
  compares it with the node and confirms it. `connect` then requires that
  confirmed fingerprint and checks the presented host key against it
  *before* any credential is sent; a mismatch aborts.
- **Credentials are one-time.** `SshCredentials` lives only in memory for
  one job; nothing here logs, stores or returns it (`repr` hides every
  secret field).
- **Root or `sudo -n`.** `open_remote_shell` detects whether the login is
  root, or may run `sudo` without a password; otherwise it fails with a
  clear `privilege_missing` error instead of hanging on a password prompt.

The paramiko-based backend is the only part that touches the network;
tests replace `ssh_backend` with a fake.
"""

import base64
import hashlib
import hmac
import io
import shlex
import socket
import time
from dataclasses import dataclass, field
from typing import Protocol

# Upper bound for one command's captured stdout/stderr (the tail is kept) --
# apt's output can be long, and the step log is shown in the UI.
MAX_CAPTURED_OUTPUT = 64 * 1024


class SshError(Exception):
    """A failed SSH step. `code` is machine-readable (the UI translates it),
    `message` says what happened and how to fix it."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class HostKeyInfo:
    key_type: str
    fingerprint_sha256: str


@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def combined(self) -> str:
        return "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int = 22


@dataclass(frozen=True)
class SshCredentials:
    """One-time login data. Never persisted; `repr=False` keeps the secrets
    out of tracebacks and debug output."""

    username: str
    password: str | None = field(default=None, repr=False)
    private_key: str | None = field(default=None, repr=False)
    passphrase: str | None = field(default=None, repr=False)

    def secrets(self) -> list[str]:
        return [s for s in (self.password, self.private_key, self.passphrase) if s]


def fingerprint_sha256(key_blob: bytes) -> str:
    """OpenSSH's SHA256 fingerprint of a public key blob."""
    digest = hashlib.sha256(key_blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def fingerprints_match(presented: str, confirmed: str) -> bool:
    return hmac.compare_digest(presented.strip(), confirmed.strip())


class SshSession(Protocol):
    def run(self, command: str, *, timeout: float) -> CommandResult: ...

    def put_bytes(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None: ...

    def put_file(self, local_path: str, remote_path: str, *, mode: int = 0o644) -> None: ...

    def close(self) -> None: ...


class SshBackend(Protocol):
    def fetch_host_key(self, target: SshTarget, *, timeout: float) -> HostKeyInfo: ...

    def connect(
        self, target: SshTarget, credentials: SshCredentials, *, expected_fingerprint: str, timeout: float
    ) -> SshSession: ...


def _tail(text: str) -> str:
    return text[-MAX_CAPTURED_OUTPUT:]


class _ParamikoSession:
    def __init__(self, transport):
        self._transport = transport
        self._sftp = None

    def run(self, command: str, *, timeout: float) -> CommandResult:
        channel = self._transport.open_session(timeout=30)
        try:
            channel.settimeout(5.0)
            channel.exec_command(command)
            out, err = bytearray(), bytearray()
            deadline = time.monotonic() + timeout
            while True:
                progressed = False
                while channel.recv_ready():
                    out.extend(channel.recv(32768))
                    progressed = True
                while channel.recv_stderr_ready():
                    err.extend(channel.recv_stderr(32768))
                    progressed = True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() > deadline:
                    raise SshError(
                        "command_timeout",
                        f"A command on the node did not finish within {int(timeout)} s and was abandoned.",
                    )
                if not progressed:
                    time.sleep(0.1)
                if len(out) > 4 * MAX_CAPTURED_OUTPUT:
                    del out[: len(out) - MAX_CAPTURED_OUTPUT]
                if len(err) > 4 * MAX_CAPTURED_OUTPUT:
                    del err[: len(err) - MAX_CAPTURED_OUTPUT]
            status = channel.recv_exit_status()
            return CommandResult(
                exit_status=status,
                stdout=_tail(out.decode("utf-8", errors="replace")),
                stderr=_tail(err.decode("utf-8", errors="replace")),
            )
        finally:
            channel.close()

    def _sftp_client(self):
        import paramiko

        if self._sftp is None:
            self._sftp = paramiko.SFTPClient.from_transport(self._transport)
        return self._sftp

    def put_bytes(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None:
        sftp = self._sftp_client()
        # The mode is set before any content is written, so a secret file is
        # never readable by others, not even briefly.
        with sftp.open(remote_path, "wb") as handle:
            handle.chmod(mode)
            handle.write(data)

    def put_file(self, local_path: str, remote_path: str, *, mode: int = 0o644) -> None:
        sftp = self._sftp_client()
        sftp.put(local_path, remote_path)
        sftp.chmod(remote_path, mode)

    def close(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self._transport.close()


def load_private_key(text: str, passphrase: str | None):
    """An OpenSSH/PEM private key (ed25519, ECDSA or RSA) from its text."""
    import paramiko

    last_error: Exception | None = None
    for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_class.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            raise SshError(
                "private_key_passphrase_required",
                "The private key is encrypted: enter its passphrase.",
            ) from exc
        except (paramiko.SSHException, ValueError, IndexError, TypeError) as exc:
            last_error = exc
    raise SshError(
        "private_key_invalid",
        "The private key could not be read (or the passphrase is wrong). Paste the whole key including the "
        "'-----BEGIN ... PRIVATE KEY-----' lines; supported types: ed25519, ECDSA, RSA.",
    ) from last_error


class ParamikoBackend:
    """The real SSH backend (paramiko). Host keys are never looked up in or
    written to any known_hosts file: the confirmed fingerprint is the only
    trust anchor."""

    def _transport(self, target: SshTarget, timeout: float):
        import paramiko

        try:
            sock = socket.create_connection((target.host, target.port), timeout=timeout)
        except OSError as exc:
            raise SshError(
                "connect_failed",
                f"Could not reach {target.host}:{target.port} over SSH ({exc}). Check the address, the port and "
                "that the RESURGAM appliance may reach the node on that port.",
            ) from exc
        transport = paramiko.Transport(sock)
        transport.banner_timeout = timeout
        try:
            transport.start_client(timeout=timeout)
        except (paramiko.SSHException, OSError, EOFError) as exc:
            transport.close()
            raise SshError(
                "connect_failed",
                f"{target.host}:{target.port} did not complete an SSH handshake ({exc}). Is an SSH server listening "
                "there?",
            ) from exc
        return transport

    def fetch_host_key(self, target: SshTarget, *, timeout: float) -> HostKeyInfo:
        transport = self._transport(target, timeout)
        try:
            key = transport.get_remote_server_key()
            return HostKeyInfo(key_type=key.get_name(), fingerprint_sha256=fingerprint_sha256(key.asbytes()))
        finally:
            transport.close()

    def connect(
        self, target: SshTarget, credentials: SshCredentials, *, expected_fingerprint: str, timeout: float
    ) -> SshSession:
        import paramiko

        transport = self._transport(target, timeout)
        try:
            key = transport.get_remote_server_key()
            presented = fingerprint_sha256(key.asbytes())
            if not fingerprints_match(presented, expected_fingerprint):
                raise SshError(
                    "host_key_mismatch",
                    f"The node's SSH host key changed: it now presents {presented} ({key.get_name()}), not the "
                    f"confirmed {expected_fingerprint}. Nothing was sent. Check the node (ssh-keygen -lf "
                    "/etc/ssh/ssh_host_*_key.pub) and confirm the fingerprint again.",
                )
            try:
                if credentials.private_key:
                    pkey = load_private_key(credentials.private_key, credentials.passphrase)
                    transport.auth_publickey(credentials.username, pkey)
                elif credentials.password is not None:
                    transport.auth_password(credentials.username, credentials.password)
                else:
                    raise SshError("credentials_missing", "Enter a password or a private key.")
            except paramiko.BadAuthenticationType as exc:
                raise SshError(
                    "auth_method_not_allowed",
                    f"The node does not accept this login method (allowed: {', '.join(exc.allowed_types)}).",
                ) from exc
            except paramiko.AuthenticationException as exc:
                raise SshError(
                    "auth_failed",
                    f"The node rejected the login for user '{credentials.username}'. Check user name and password/key.",
                ) from exc
            if not transport.is_authenticated():
                raise SshError("auth_failed", "The node did not accept the login.")
            return _ParamikoSession(transport)
        except Exception:
            transport.close()
            raise


# Replaced by a fake in tests -- the only object here that opens sockets.
ssh_backend: SshBackend = ParamikoBackend()


class RemoteShell:
    """Runs commands on the node, as root directly or through `sudo -n`."""

    def __init__(self, session: SshSession, privilege_mode: str):
        self.session = session
        self.privilege_mode = privilege_mode  # "root" | "sudo"

    def wrap(self, command: str, *, privileged: bool) -> str:
        if not privileged or self.privilege_mode == "root":
            return command
        return f"sudo -n sh -c {shlex.quote(command)}"

    def run(self, command: str, *, privileged: bool = True, timeout: float = 120.0) -> CommandResult:
        return self.session.run(self.wrap(command, privileged=privileged), timeout=timeout)


def open_remote_shell(session: SshSession, username: str) -> RemoteShell:
    """Detects root vs. passwordless sudo (`sudo -n` never prompts)."""
    whoami = session.run("id -u", timeout=30)
    if whoami.ok and whoami.stdout.strip() == "0":
        return RemoteShell(session, "root")
    sudo = session.run("sudo -n true", timeout=30)
    if sudo.ok:
        return RemoteShell(session, "sudo")
    raise SshError(
        "privilege_missing",
        f"User '{username}' is not root and may not run sudo without a password (`sudo -n true` failed: "
        f"{sudo.combined() or 'no output'}). Log in as root, or allow this user passwordless sudo on the node "
        "(e.g. a file in /etc/sudoers.d), then try again.",
    )
