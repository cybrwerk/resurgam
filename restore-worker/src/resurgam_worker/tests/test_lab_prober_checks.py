"""`checks.py`'s Level 2/3 probe implementations -- tested against real
local listeners (a genuine loopback TCP/HTTP/TLS/SSH-banner server),
not mocks, matching `restore-worker`'s existing "confirmed live where
practical" discipline for anything socket-shaped.
"""

import socket
import ssl
import subprocess
import threading

import pytest

from resurgam_worker.lab_prober import checks


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OneShotServer:
    """Accepts exactly one connection, sends `response`, then closes --
    enough for a single check call.
    """

    def __init__(self, response: bytes, *, tls: bool = False):
        self.port = _free_port()
        self._response = response
        self._tls = tls
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            raw_conn, _ = self._sock.accept()
        except OSError:
            return
        conn = raw_conn
        if self._tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*_SELF_SIGNED_CERT)
            try:
                conn = context.wrap_socket(raw_conn, server_side=True)
            except ssl.SSLError:
                raw_conn.close()
                return
        with conn:
            try:
                conn.settimeout(2)
                # Sends unprompted, matching real SSH server behavior
                # (banner comes first, unrequested) -- the HTTP/HTTPS
                # checks' own request write doesn't need to be read
                # server-side for this fixed-response fake to work.
                conn.sendall(self._response)
                if self._tls:
                    conn.unwrap()
            except OSError:
                pass

    def close(self):
        self._sock.close()


@pytest.fixture(scope="module")
def _self_signed_cert(tmp_path_factory):
    """Generated via the `openssl` CLI rather than a `cryptography`
    dependency -- `restore-worker` deliberately stays dependency-light
    (ADR 0019), and this is test-only.
    """
    import shutil
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not available")

    tmp_path = tmp_path_factory.mktemp("cert")
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    global _SELF_SIGNED_CERT
    _SELF_SIGNED_CERT = (str(cert_path), str(key_path))
    return _SELF_SIGNED_CERT


def test_check_icmp_passes_for_localhost():
    result = checks.check_icmp("127.0.0.1")
    assert result["check_type"] == "icmp"
    assert result["status"] == "pass"


def test_check_icmp_fails_for_unroutable_address():
    # TEST-NET-1 (RFC 5737), guaranteed unreachable.
    result = checks.check_icmp("192.0.2.1")
    assert result["status"] == "fail"


def test_check_tcp_port_passes_against_real_listener():
    server = _OneShotServer(b"ok")
    try:
        result = checks.check_tcp_port("127.0.0.1", server.port)
        assert result["status"] == "pass"
    finally:
        server.close()


def test_check_tcp_port_fails_when_nothing_listening():
    result = checks.check_tcp_port("127.0.0.1", _free_port())
    assert result["status"] == "fail"


def test_check_http_passes_on_any_status_code():
    server = _OneShotServer(b"HTTP/1.0 404 Not Found\r\n\r\n")
    try:
        result = checks.check_http("127.0.0.1", server.port)
        assert result["status"] == "pass"
    finally:
        server.close()


def test_check_http_fails_on_non_http_response():
    server = _OneShotServer(b"not http\r\n")
    try:
        result = checks.check_http("127.0.0.1", server.port)
        assert result["status"] == "fail"
    finally:
        server.close()


def test_check_ssh_banner_passes_on_real_banner():
    server = _OneShotServer(b"SSH-2.0-OpenSSH_9.2\r\n")
    try:
        result = checks.check_ssh_banner("127.0.0.1", server.port)
        assert result["status"] == "pass"
    finally:
        server.close()


def test_check_ssh_banner_fails_on_non_ssh_service():
    server = _OneShotServer(b"HTTP/1.0 200 OK\r\n\r\n")
    try:
        result = checks.check_ssh_banner("127.0.0.1", server.port)
        assert result["status"] == "fail"
    finally:
        server.close()


def test_check_https_and_tls_handshake_pass_against_real_tls_listener(_self_signed_cert):
    server = _OneShotServer(b"HTTP/1.0 200 OK\r\n\r\n", tls=True)
    try:
        https_result = checks.check_https("127.0.0.1", server.port)
        assert https_result["status"] == "pass"
    finally:
        server.close()

    server2 = _OneShotServer(b"", tls=True)
    try:
        tls_result = checks.check_tls_handshake("127.0.0.1", server2.port)
        assert tls_result["status"] == "pass"
    finally:
        server2.close()


def test_run_check_dispatches_by_type():
    result = checks.run_check("icmp", "127.0.0.1")
    assert result["check_type"] == "icmp"


def test_run_check_unknown_type_fails_closed():
    result = checks.run_check("bogus", "127.0.0.1")
    assert result["status"] == "fail"


# --- check_arp -- mocked, not real listeners: ARP inherently needs a
# real L2 neighbor, which isn't reproducible in a generic test
# environment (confirmed live against a real target instead, 2026-09-22).


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_check_arp_passes_on_reachable_state(monkeypatch):
    calls = []
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda command, **kw: (
            calls.append(command)
            or _fake_completed(
                stdout="10.99.0.110 dev eth0 lladdr da:ae:1f:b7:5f:ec REACHABLE\n" if command[0] == "ip" else ""
            )
        ),
    )
    result = checks.check_arp("10.99.0.110")
    assert result["status"] == "pass"
    assert calls[0][:2] == ["ping", "-c"]
    assert calls[1] == ["ip", "neigh", "show", "10.99.0.110"]


def test_check_arp_fails_when_no_entry():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(checks.subprocess, "run", lambda command, **kw: _fake_completed(stdout=""))
        result = checks.check_arp("10.99.0.110")
    assert result["status"] == "fail"


def test_check_arp_fails_on_incomplete_state():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            checks.subprocess,
            "run",
            lambda command, **kw: _fake_completed(stdout="10.99.0.110 dev eth0  INCOMPLETE\n"),
        )
        result = checks.check_arp("10.99.0.110")
    assert result["status"] == "fail"


# --- check_nmap_scan ----------------------------------------------------------


def test_check_nmap_scan_passes_when_ports_open(monkeypatch):
    nmap_output = (
        "Starting Nmap\n"
        "Nmap scan report for 10.99.0.110\n"
        "PORT     STATE SERVICE\n"
        "135/tcp  open  msrpc\n"
        "3389/tcp open  ms-wbt-server\n"
        "445/tcp  closed microsoft-ds\n"
    )
    monkeypatch.setattr(checks.subprocess, "run", lambda command, **kw: _fake_completed(stdout=nmap_output))

    result = checks.check_nmap_scan("10.99.0.110")

    assert result["status"] == "pass"
    assert "3389/tcp" in result["detail"]
    assert "445/tcp" not in result["detail"]


def test_check_nmap_scan_fails_when_nothing_open(monkeypatch):
    nmap_output = "Starting Nmap\nNmap scan report for 10.99.0.110\nPORT   STATE  SERVICE\n80/tcp closed http\n"
    monkeypatch.setattr(checks.subprocess, "run", lambda command, **kw: _fake_completed(stdout=nmap_output))

    result = checks.check_nmap_scan("10.99.0.110")

    assert result["status"] == "fail"


def test_check_nmap_scan_fails_on_command_error(monkeypatch):
    monkeypatch.setattr(
        checks.subprocess, "run", lambda command, **kw: (_ for _ in ()).throw(FileNotFoundError("nmap"))
    )

    result = checks.check_nmap_scan("10.99.0.110")

    assert result["status"] == "fail"
