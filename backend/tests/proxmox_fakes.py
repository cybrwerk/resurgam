"""Mocked-HTTP fakes for PBS/PVE onboarding tests (health check, token
provisioning). Nothing here opens a socket: DNS (`socket.getaddrinfo` as
seen by `connectivity.health`) and every `httpx` verb are monkeypatched.
"""

import socket
from dataclasses import dataclass, field

import httpx

from resurgam_api.connectivity import health as health_module
from resurgam_api.connectivity.required_privileges import PBS_REQUIRED_PRIVILEGES, PVE_REQUIRED_PRIVILEGES


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def all_privileges(product: str) -> dict:
    required = PBS_REQUIRED_PRIVILEGES if product == "pbs" else PVE_REQUIRED_PRIVILEGES
    return {"/": {p.name: True for p in required}}


@dataclass
class FakeProxmox:
    """Routes `(METHOD, path-suffix)` to a response. Records every call
    (method, url, headers, data) so tests can assert on them."""

    routes: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    resolve_to: str | None = "192.0.2.10"
    raise_on: dict = field(default_factory=dict)

    def install(self, monkeypatch) -> "FakeProxmox":
        def fake_getaddrinfo(host, port, *args, **kwargs):
            if self.resolve_to is None:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.resolve_to, port))]

        monkeypatch.setattr(health_module.socket, "getaddrinfo", fake_getaddrinfo)
        for verb in ("get", "post", "put", "delete"):
            monkeypatch.setattr(httpx, verb, self._handler(verb.upper()))
        return self

    def _handler(self, method):
        def handle(url, **kwargs):
            self.calls.append((method, url, kwargs.get("headers") or {}, kwargs.get("data")))
            for (m, suffix), exc in self.raise_on.items():
                if m == method and url.endswith(suffix):
                    raise exc
            # longest matching suffix wins
            matches = [
                (len(suffix), resp) for (m, suffix), resp in self.routes.items() if m == method and url.endswith(suffix)
            ]
            if not matches:
                return FakeResponse({"errors": "not found"}, status_code=404, text="not found")
            resp = max(matches, key=lambda item: item[0])[1]
            return resp(url, kwargs) if callable(resp) else resp

        return handle

    def called(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]
