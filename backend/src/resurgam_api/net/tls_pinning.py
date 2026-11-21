import hashlib
import socket
import ssl


class TlsFingerprintMismatchError(Exception):
    def __init__(self, host: str, port: int, expected: str, actual: str) -> None:
        self.host = host
        self.port = port
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"TLS certificate fingerprint mismatch for {host}:{port}: expected {expected}, observed {actual}"
        )


def _unverified_client_context() -> ssl.SSLContext:
    """A context that completes the TLS handshake without chain/hostname
    validation, because we're about to validate the peer certificate
    ourselves via a pinned fingerprint instead (manifest §6.1/§6.2:
    self-signed certs must be supported via fingerprint pinning).
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def fetch_peer_certificate_sha256(host: str, port: int, timeout: float = 5.0) -> str:
    """Open a TLS connection to host:port and return the SHA-256 fingerprint
    (lowercase hex) of the peer's leaf certificate, without validating it
    against any trust chain.
    """
    context = _unverified_client_context()
    with (
        socket.create_connection((host, port), timeout=timeout) as sock,
        context.wrap_socket(sock, server_hostname=host) as tls_sock,
    ):
        der_cert = tls_sock.getpeercert(binary_form=True)
    if der_cert is None:
        raise ssl.SSLError(f"no peer certificate presented by {host}:{port}")
    return hashlib.sha256(der_cert).hexdigest()


def verify_pinned_fingerprint(host: str, port: int, expected_fingerprint_sha256: str, timeout: float = 5.0) -> None:
    """Raise TlsFingerprintMismatchError unless the peer's current
    certificate fingerprint matches exactly. Callers must not send any
    application data before this returns successfully.
    """
    actual = fetch_peer_certificate_sha256(host, port, timeout=timeout)
    if actual.lower() != expected_fingerprint_sha256.lower():
        raise TlsFingerprintMismatchError(host, port, expected_fingerprint_sha256, actual)
