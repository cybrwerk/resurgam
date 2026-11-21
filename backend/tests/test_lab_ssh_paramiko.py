"""The real paramiko SSH backend against an in-process SSH server on
127.0.0.1 (no external host): fingerprint format, host key check before
any credential is sent, password auth, command execution, key loading."""

import io
import shutil
import socket
import subprocess
import threading

import paramiko
import pytest

from resurgam_api.lab_setup.ssh import (
    ParamikoBackend,
    SshCredentials,
    SshError,
    SshTarget,
    fingerprint_sha256,
    load_private_key,
    open_remote_shell,
)

HOST_KEY = paramiko.RSAKey.generate(2048)
PASSWORD = "local-test-password"


class _Server(paramiko.ServerInterface):
    def __init__(self, events: list):
        self.events = events

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        self.events.append(("auth", username))
        return paramiko.AUTH_SUCCESSFUL if password == PASSWORD else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.events.append(("exec", command.decode()))

        def reply():
            if command == b"id -u":
                channel.sendall(b"0\n")
                status = 0
            else:
                channel.sendall(b"out:" + command)
                channel.sendall_stderr(b"err")
                status = 3
            channel.send_exit_status(status)
            channel.close()

        threading.Thread(target=reply, daemon=True).start()
        return True


@pytest.fixture
def ssh_server():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    events: list = []
    stop = threading.Event()

    def serve():
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(HOST_KEY)
            try:
                transport.start_server(server=_Server(events))
            except (paramiko.SSHException, EOFError, OSError):
                continue

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], events
    stop.set()
    listener.close()
    thread.join(timeout=5)


def test_fingerprint_matches_ssh_keygen(tmp_path):
    expected = fingerprint_sha256(HOST_KEY.asbytes())
    assert expected.startswith("SHA256:") and len(expected) == 7 + 43
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not installed")
    pub = tmp_path / "host.pub"
    pub.write_text(f"{HOST_KEY.get_name()} {HOST_KEY.get_base64()}\n")
    output = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, check=True).stdout
    assert expected in output


def test_host_key_then_strict_connect(ssh_server):
    port, events = ssh_server
    backend = ParamikoBackend()
    target = SshTarget("127.0.0.1", port)

    info = backend.fetch_host_key(target, timeout=10)
    assert info.fingerprint_sha256 == fingerprint_sha256(HOST_KEY.asbytes())
    assert info.key_type in ("ssh-rsa", "rsa-sha2-512", "rsa-sha2-256")
    assert not [e for e in events if e[0] == "auth"]

    with pytest.raises(SshError) as mismatch:
        backend.connect(
            target,
            SshCredentials(username="root", password=PASSWORD),
            expected_fingerprint="SHA256:" + "A" * 43,
            timeout=10,
        )
    assert mismatch.value.code == "host_key_mismatch"
    assert not [e for e in events if e[0] == "auth"], "credentials were sent to an unconfirmed host"

    with pytest.raises(SshError) as bad:
        backend.connect(
            target,
            SshCredentials(username="root", password="wrong"),
            expected_fingerprint=info.fingerprint_sha256,
            timeout=10,
        )
    assert bad.value.code == "auth_failed"

    session = backend.connect(
        target,
        SshCredentials(username="root", password=PASSWORD),
        expected_fingerprint=info.fingerprint_sha256,
        timeout=10,
    )
    try:
        shell = open_remote_shell(session, "root")
        assert shell.privilege_mode == "root"
        result = shell.run("echo hi", timeout=10)
        assert result.exit_status == 3
        assert result.stdout == "out:echo hi"
        assert result.stderr == "err"
    finally:
        session.close()


def test_connect_failure_is_explained():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(SshError) as exc:
        ParamikoBackend().fetch_host_key(SshTarget("127.0.0.1", port), timeout=2)
    assert exc.value.code == "connect_failed"


def test_load_private_key_plain_encrypted_and_invalid():
    key = paramiko.RSAKey.generate(2048)
    plain = io.StringIO()
    key.write_private_key(plain)
    assert load_private_key(plain.getvalue(), None).asbytes() == key.asbytes()

    encrypted = io.StringIO()
    key.write_private_key(encrypted, password="pass phrase")
    assert load_private_key(encrypted.getvalue(), "pass phrase").asbytes() == key.asbytes()
    with pytest.raises(SshError) as missing:
        load_private_key(encrypted.getvalue(), None)
    assert missing.value.code == "private_key_passphrase_required"
    with pytest.raises(SshError) as invalid:
        load_private_key("not a key", None)
    assert invalid.value.code == "private_key_invalid"


def test_credentials_repr_hides_secrets():
    creds = SshCredentials(username="root", password=PASSWORD, private_key="KEYDATA", passphrase="PHRASE")
    text = repr(creds)
    assert PASSWORD not in text and "KEYDATA" not in text and "PHRASE" not in text
