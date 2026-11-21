from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel


class ProbeTarget(BaseModel):
    """Mirrors `backend`'s `schemas.restore_test.ProbeTargetOut` -- kept
    as a plain duplicate rather than a shared import, same reasoning as
    `lab_agent.client.LabAgentJob` (ADR 0019: this package has no
    dependency on `backend`).
    """

    member_id: str
    ip: str
    check_types: list[str]


class ProbeResult(BaseModel):
    check_type: str
    status: Literal["pass", "fail"]
    detail: str | None = None
    checked_at: str


class LabProberApiError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"RESURGAM backend returned HTTP {status_code}: {body[:200]}")


class LabProberClient:
    """Thin wrapper around this run's own `/lab-runs/{id}/probe-*`
    HTTPS backchannel -- authenticated by the per-run bearer token
    (`RestoreTestPlan.appliance_run_token_hash`), never a user session
    or the long-lived cluster-scoped `resurgam-lab-agent` token.
    """

    def __init__(self, backend_url: str, run_id: str, run_token: str, *, timeout: float = 30.0) -> None:
        self._run_id = run_id
        self._client = httpx.Client(
            base_url=backend_url,
            headers={"Authorization": f"Bearer {run_token}"},
            timeout=timeout,
        )

    def list_probe_targets(self) -> list[ProbeTarget]:
        response = self._client.get(f"/lab-runs/{self._run_id}/probe-targets")
        if response.status_code != 200:
            raise LabProberApiError(response.status_code, response.text)
        return [ProbeTarget.model_validate(t) for t in response.json()]

    def submit_probe_results(self, member_id: str, results: list[ProbeResult]) -> None:
        body = {"member_id": member_id, "results": [r.model_dump(mode="json") for r in results]}
        response = self._client.post(f"/lab-runs/{self._run_id}/probe-results", json=body)
        if response.status_code != 200:
            raise LabProberApiError(response.status_code, response.text)

    def close(self) -> None:
        self._client.close()
