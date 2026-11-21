"""`connectivity._http.request_json` -- the shared request-and-unwrap
primitive `pbs_api.py`/`pve_api.py` both delegate to (see that module's
docstring for why).
"""

import httpx

from resurgam_api.connectivity._http import request_json
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.models.pbs import TlsTrustMode

_TRUST = TrustConfig(trust_mode=TlsTrustMode.CA)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_request_json_unwraps_data_field_by_default(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["params"] = kwargs.get("params")
        return _FakeResponse({"data": [1, 2, 3]})

    monkeypatch.setattr(httpx, "get", fake_get)

    data, error = request_json(
        "GET",
        "pve.example",
        8006,
        _TRUST,
        "secret",
        "/api2/json/nodes",
        5.0,
        auth_scheme="PVEAPIToken",
        params={"foo": "bar"},
    )

    assert error is None
    assert data == [1, 2, 3]
    assert captured["url"] == "https://pve.example:8006/api2/json/nodes"
    assert captured["headers"] == {"Authorization": "PVEAPIToken=secret"}
    assert captured["params"] == {"foo": "bar"}


def test_request_json_can_return_full_body_unwrapped(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": [], "total": 5}))

    body, error = request_json(
        "GET",
        "pbs.example",
        8007,
        _TRUST,
        "secret",
        "/api2/json/nodes/localhost/tasks",
        5.0,
        auth_scheme="PBSAPIToken",
        unwrap_data=False,
    )

    assert error is None
    assert body == {"data": [], "total": 5}


def test_request_json_reports_unexpected_status_as_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResponse({"message": "denied"}, status_code=403))

    data, error = request_json(
        "POST",
        "pve.example",
        8006,
        _TRUST,
        "secret",
        "/api2/json/nodes/pve1/qemu",
        5.0,
        auth_scheme="PVEAPIToken",
        data={"vmid": 100},
        ok_statuses=(200, 201),
    )

    assert data is None
    assert error is not None
    assert "HTTP 403" in error


def test_request_json_wraps_malformed_json_as_error(monkeypatch):
    class _Unparseable:
        status_code = 200
        text = "not json"

        def json(self):
            raise ValueError("boom")

    monkeypatch.setattr(httpx, "put", lambda *a, **kw: _Unparseable())

    data, error = request_json(
        "PUT",
        "pbs.example",
        8007,
        _TRUST,
        "secret",
        "/api2/json/config/datastore/store1",
        5.0,
        auth_scheme="PBSAPIToken",
        data={"maintenance-mode": "offline"},
        ok_statuses=(200, 201),
    )

    assert data is None
    assert "invalid JSON response" in error


def test_request_json_does_not_send_a_body_for_delete(monkeypatch):
    # DELETE never sends a `data` body -- PVE's own write endpoints take
    # options as query params instead (`destroy_vm`'s `purge`/`destroy-
    # unreferenced-disks` flags), which `request_json` does support for
    # DELETE (`params`, defaulting to None when the caller doesn't pass any).
    captured = {}

    def fake_delete(url, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "delete", fake_delete)

    request_json(
        "DELETE",
        "pve.example",
        8006,
        _TRUST,
        "secret",
        "/api2/json/cluster/sdn/vnets/lab1",
        5.0,
        auth_scheme="PVEAPIToken",
        ok_statuses=(200, 201),
    )

    assert "data" not in captured["kwargs"]
    assert captured["kwargs"].get("params") is None
