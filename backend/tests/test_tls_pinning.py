import pytest

from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pbs_api import probe_pbs
from resurgam_api.models.pbs import TlsTrustMode
from resurgam_api.net.tls_pinning import (
    TlsFingerprintMismatchError,
    fetch_peer_certificate_sha256,
    verify_pinned_fingerprint,
)


def test_fetch_peer_certificate_matches_expected(local_tls_server):
    host, port, expected_fingerprint = local_tls_server
    assert fetch_peer_certificate_sha256(host, port) == expected_fingerprint


def test_verify_pinned_fingerprint_accepts_match(local_tls_server):
    host, port, expected_fingerprint = local_tls_server
    verify_pinned_fingerprint(host, port, expected_fingerprint)  # must not raise


def test_verify_pinned_fingerprint_rejects_mismatch(local_tls_server):
    host, port, _ = local_tls_server
    with pytest.raises(TlsFingerprintMismatchError):
        verify_pinned_fingerprint(host, port, "0" * 64)


def test_probe_pbs_succeeds_with_correct_pinned_fingerprint(local_tls_server):
    host, port, fingerprint = local_tls_server
    trust = TrustConfig(trust_mode=TlsTrustMode.FINGERPRINT, fingerprint_sha256=fingerprint)

    result = probe_pbs(host, port, trust, token="test@pbs!token:secret")

    assert result.reachable is True
    assert result.version == "4.2.1"
    assert result.error is None


def test_probe_pbs_fails_before_sending_request_with_wrong_pinned_fingerprint(local_tls_server):
    host, port, _ = local_tls_server
    trust = TrustConfig(trust_mode=TlsTrustMode.FINGERPRINT, fingerprint_sha256="0" * 64)

    result = probe_pbs(host, port, trust, token="test@pbs!token:secret")

    assert result.reachable is False
    assert "mismatch" in result.error.lower()


def test_probe_pbs_requires_fingerprint_in_fingerprint_mode(local_tls_server):
    host, port, _ = local_tls_server
    trust = TrustConfig(trust_mode=TlsTrustMode.FINGERPRINT, fingerprint_sha256=None)

    result = probe_pbs(host, port, trust, token="test@pbs!token:secret")

    assert result.reachable is False
    assert "requires a fingerprint" in result.error
