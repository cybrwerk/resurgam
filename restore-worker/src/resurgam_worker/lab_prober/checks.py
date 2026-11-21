"""Level 2/3 of the Recovery Lab 3-tier probe hierarchy (ADR 0019, user
guidance 2026-09-19) -- `icmp` (Level 2) plus a small, bounded set of
built-in service checks (Level 3): `tcp_port`, `http`, `https`, `dns`,
`tls_handshake`, `ssh_banner`. Deliberately no arbitrary user-supplied
script execution (manifest §22.6). Level 1 (`guest_agent`) is always
resolved backend-side, never here -- see `backend`'s
`restore_test/execution.py::NETWORK_TIER_CHECK_TYPES`.

Fixed conventional default ports per type (user's explicit choice,
2026-09-21, over adding a port/path parameter to `check_types`):
`ssh_banner`/`tcp_port` -> 22, `http` -> 80, `https`/`tls_handshake` -> 443,
`dns` -> 53.
"""

from __future__ import annotations

import socket
import ssl
import subprocess
from datetime import UTC, datetime

DEFAULT_PORTS: dict[str, int] = {
    "tcp_port": 22,
    "ssh_banner": 22,
    "http": 80,
    "https": 443,
    "tls_handshake": 443,
    "dns": 53,
}

_CONNECT_TIMEOUT_SECONDS = 3.0
_PING_TIMEOUT_SECONDS = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _result(check_type: str, passed: bool, detail: str | None = None) -> dict:
    return {"check_type": check_type, "status": "pass" if passed else "fail", "detail": detail, "checked_at": _now()}


def check_icmp(ip: str) -> dict:
    """`ping -c1 -W2 <ip>` -- pass on exit code 0. Uses the system `ping`
    binary rather than a raw ICMP socket, which needs `CAP_NET_RAW`/root
    on most distros; `ping` itself is typically setuid/setcap already.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary + argument list, no shell
            ["ping", "-c", "1", "-W", str(_PING_TIMEOUT_SECONDS), ip],
            check=False,
            capture_output=True,
            text=True,
            timeout=_PING_TIMEOUT_SECONDS + 2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result("icmp", False, str(exc))
    return _result("icmp", result.returncode == 0, result.stdout[-200:] if result.returncode != 0 else None)


def check_tcp_port(ip: str, port: int | None = None) -> dict:
    port = port or DEFAULT_PORTS["tcp_port"]
    try:
        with socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT_SECONDS):
            return _result("tcp_port", True)
    except OSError as exc:
        return _result("tcp_port", False, str(exc))


def check_dns(ip: str, port: int | None = None) -> dict:
    """Reachability of the DNS service itself (TCP connect on 53) -- not
    a real resolution attempt, matching this check's scope as "is the
    service up," same as every other Level 3 check here.
    """
    port = port or DEFAULT_PORTS["dns"]
    try:
        with socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT_SECONDS):
            return _result("dns", True)
    except OSError as exc:
        return _result("dns", False, str(exc))


def check_http(ip: str, port: int | None = None) -> dict:
    """A raw HTTP GET -- passes on *any* response (even 4xx/5xx): the
    point is proving the service answered at all, not that it returned
    200.
    """
    port = port or DEFAULT_PORTS["http"]
    try:
        with socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT_SECONDS) as sock:
            sock.sendall(f"GET / HTTP/1.0\r\nHost: {ip}\r\n\r\n".encode())
            sock.settimeout(_CONNECT_TIMEOUT_SECONDS)
            response = sock.recv(64)
    except OSError as exc:
        return _result("http", False, str(exc))
    if not response.startswith(b"HTTP/"):
        return _result("http", False, f"unexpected response: {response[:32]!r}")
    return _result("http", True, response.split(b"\r\n", 1)[0].decode(errors="replace"))


def check_https(ip: str, port: int | None = None) -> dict:
    """A TLS handshake plus a raw HTTP GET over it -- cert verification
    disabled deliberately (self-signed is expected on a restored
    internal VM; the handshake *succeeding* and the service answering
    is what's being proven, not certificate trust).
    """
    port = port or DEFAULT_PORTS["https"]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT_SECONDS) as sock,
            context.wrap_socket(sock) as tls_sock,
        ):
            tls_sock.sendall(f"GET / HTTP/1.0\r\nHost: {ip}\r\n\r\n".encode())
            tls_sock.settimeout(_CONNECT_TIMEOUT_SECONDS)
            response = tls_sock.recv(64)
    except (OSError, ssl.SSLError) as exc:
        return _result("https", False, str(exc))
    if not response.startswith(b"HTTP/"):
        return _result("https", False, f"unexpected response: {response[:32]!r}")
    return _result("https", True, response.split(b"\r\n", 1)[0].decode(errors="replace"))


def check_tls_handshake(ip: str, port: int | None = None) -> dict:
    """A bare TLS handshake, no HTTP request -- distinct from `https`:
    proves the port speaks TLS at all (e.g. a non-HTTP TLS service),
    not that an HTTP response follows.
    """
    port = port or DEFAULT_PORTS["tls_handshake"]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT_SECONDS) as sock,
            context.wrap_socket(sock) as tls_sock,
        ):
            cipher = tls_sock.cipher()
    except (OSError, ssl.SSLError) as exc:
        return _result("tls_handshake", False, str(exc))
    return _result("tls_handshake", True, str(cipher))


def check_ssh_banner(ip: str, port: int | None = None) -> dict:
    """Connects and reads the first line, passes if it starts with
    `SSH-` -- confirms an actual SSH service, not just an open port.
    """
    port = port or DEFAULT_PORTS["ssh_banner"]
    try:
        with socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT_SECONDS) as sock:
            sock.settimeout(_CONNECT_TIMEOUT_SECONDS)
            banner = sock.recv(256)
    except OSError as exc:
        return _result("ssh_banner", False, str(exc))
    if not banner.startswith(b"SSH-"):
        return _result("ssh_banner", False, f"unexpected banner: {banner[:32]!r}")
    return _result("ssh_banner", True, banner.split(b"\r\n", 1)[0].decode(errors="replace"))


def check_arp(ip: str, port: int | None = None) -> dict:
    """Confirms the guest actually answers at L2 -- proven more reliable
    than `icmp` live (2026-09-22): a real Windows guest with its default
    firewall (blocks ICMP echo, allows RDP) failed `icmp` outright, but
    `ip neigh` showed a real, correctly-resolved ARP entry for it the
    whole time -- ARP resolution happens in the kernel below any
    userspace firewall, so it isn't subject to the same filtering.

    No `arping` binary needed (not part of this project's existing
    dependencies) -- `ping` already triggers ARP resolution as a
    kernel-level side effect regardless of whether the ICMP reply itself
    ever comes back, confirmed live the same way (flushing the ARP
    cache, pinging -- which still reports 100% loss -- then finding a
    fresh, correctly-resolved neighbor entry immediately after).
    """
    subprocess.run(  # noqa: S603 - fixed binary + argument list, no shell
        ["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=3, check=False
    )
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary + argument list, no shell
            ["ip", "neigh", "show", ip], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result("arp", False, str(exc))
    # Real states confirmed live: REACHABLE/STALE/DELAY/PROBE/PERMANENT
    # all mean a real reply was (or recently was) received; FAILED,
    # INCOMPLETE, or no entry at all mean it never answered.
    if not result.stdout.strip():
        return _result("arp", False, "no ARP entry -- host never answered")
    state = result.stdout.split()[-1]
    if state in ("FAILED", "INCOMPLETE"):
        return _result("arp", False, result.stdout.strip())
    return _result("arp", True, result.stdout.strip())


# A small, fixed set of common ports -- deliberately bounded (manifest
# §22.6's "no arbitrary scripts" philosophy extends to this check too:
# a fixed, predictable scan, not an operator-suppliable port range or
# nmap script).
_NMAP_TOP_PORTS = 100
_NMAP_TIMEOUT_SECONDS = 30


def check_nmap_scan(ip: str, port: int | None = None) -> dict:
    """A bounded `nmap` port scan (top 100 common ports, not all 65535)
    -- the richest Level 3 check here: proves which services are
    actually reachable on a restored guest, not just one fixed port.
    Passes if at least one port is found open; `detail` carries the
    scan's own open-ports summary as real evidence for the Lab report.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary + argument list, no shell
            ["nmap", "-Pn", "-T4", "--top-ports", str(_NMAP_TOP_PORTS), ip],
            check=False,
            capture_output=True,
            text=True,
            timeout=_NMAP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result("nmap_scan", False, str(exc))
    open_lines = [line.strip() for line in result.stdout.splitlines() if "/tcp" in line or "/udp" in line]
    open_ports = [line for line in open_lines if line.split()[1] == "open"] if open_lines else []
    if not open_ports:
        return _result("nmap_scan", False, "no open ports found" if result.returncode == 0 else result.stderr.strip())
    return _result("nmap_scan", True, "; ".join(open_ports))


_CHECKS = {
    "icmp": check_icmp,
    "arp": check_arp,
    "tcp_port": check_tcp_port,
    "dns": check_dns,
    "http": check_http,
    "https": check_https,
    "tls_handshake": check_tls_handshake,
    "ssh_banner": check_ssh_banner,
    "nmap_scan": check_nmap_scan,
}


def run_check(check_type: str, ip: str) -> dict:
    """Dispatches to the right check function -- unknown check types
    (should never happen, the backend only ever sends network-tier
    types) fail closed rather than raising, so one bad entry can't take
    down an entire poll cycle.
    """
    fn = _CHECKS.get(check_type)
    if fn is None:
        return _result(check_type, False, f"unknown check type {check_type!r}")
    return fn(ip)
