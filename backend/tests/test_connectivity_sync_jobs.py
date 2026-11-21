"""Tests for list_remotes()/list_sync_jobs() parsing. Unlike
test_connectivity_pbs_api.py, these fixtures are **not** captured from a
real PBS instance — config/remote and config/sync were both empty on the
only real instance available during this milestone (see
docs/milestone-1-pbs-pve-api-assumptions.md). These lock in the
documentation-based assumption only, not a verified shape.
"""

import httpx

from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pbs_api import list_remotes, list_sync_jobs
from resurgam_api.models.pbs import TlsTrustMode

_TRUST = TrustConfig(trust_mode=TlsTrustMode.CA)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_list_remotes_parses_assumed_shape(monkeypatch):
    payload = {"data": [{"name": "dr-site", "host": "pbs2.example.com", "port": 8007, "comment": "DR target"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_remotes("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert len(result.remotes) == 1
    assert result.remotes[0].name == "dr-site"
    assert result.remotes[0].host == "pbs2.example.com"
    assert result.remotes[0].port == 8007


def test_list_remotes_reports_unexpected_shape(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": [{"unexpected": "shape"}]}))

    result = list_remotes("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is False
    assert "unexpected response shape" in result.error


def test_list_sync_jobs_parses_assumed_shape(monkeypatch):
    payload = {
        "data": [
            {
                "id": "job-1",
                "remote": "dr-site",
                "remote-store": "main",
                "store": "local-store",
                "schedule": "daily",
                "remove-vanished": True,
                "comment": "nightly DR sync",
            }
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_sync_jobs("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert len(result.sync_jobs) == 1
    job = result.sync_jobs[0]
    assert job.job_id == "job-1"
    assert job.remote_name == "dr-site"
    assert job.remote_store == "main"
    assert job.local_store == "local-store"
    assert job.schedule == "daily"
    assert job.remove_vanished is True


def test_list_sync_jobs_reports_unexpected_shape(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": [{"id": "job-1"}]}))

    result = list_sync_jobs("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is False
    assert "unexpected response shape" in result.error
