"""Timed automatic re-enable of disabled PVE backup jobs ("disable for N
hours", ADR 0018 addendum 2026-11-11): the API side (disable with a
duration, timer set/cancel, enable/delete clearing it) and the scheduler
pass (`pve_job_reenable.run_due_reenables`). All PVE calls are mocked."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from resurgam_api import pve_job_reenable
from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
from resurgam_api.connectivity.models import VzdumpJob, VzdumpJobListResult
from resurgam_api.connectivity.pve_api import PveWriteError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.pve import PveBackupJobReenable

JOB_ID = "backup-abc123"


def _create_cluster(client, headers) -> str:
    payload = {
        "display_name": "Primary PVE",
        "host": "pve1.internal.example",
        "port": 8006,
        "tls_trust_mode": "ca",
        "api_token": "root@pam!token=secretvalue",
    }
    response = client.post("/api/v1/pve-clusters", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _listing(*, enabled: bool, job_id: str = JOB_ID) -> VzdumpJobListResult:
    return VzdumpJobListResult(
        reachable=True, jobs=[VzdumpJob(id=job_id, enabled=enabled, schedule="02:30", storage="pbs-main")]
    )


def _mock_api_pve(monkeypatch, *, enabled_after: bool):
    """The API module's PVE calls: record writes, report the job in the
    state it has after the write."""
    calls = []
    monkeypatch.setattr(
        pve_clusters_module,
        "set_vzdump_job_enabled",
        lambda *a, job_id, enabled, **kw: calls.append((job_id, enabled)),
    )
    monkeypatch.setattr(pve_clusters_module, "list_vzdump_jobs", lambda *a, **kw: _listing(enabled=enabled_after))
    monkeypatch.setattr(
        pve_clusters_module,
        "get_vzdump_job",
        lambda *a, **kw: VzdumpJob(id=JOB_ID, enabled=enabled_after, schedule="02:30", storage="pbs-main"),
    )
    return calls


def _row(db_session, cluster_id) -> PveBackupJobReenable | None:
    return (
        db_session.query(PveBackupJobReenable)
        .filter_by(pve_cluster_id=uuid.UUID(cluster_id), job_id=JOB_ID)
        .one_or_none()
    )


def _url(cluster_id: str, suffix: str) -> str:
    return f"/api/v1/pve-clusters/{cluster_id}/backup-jobs/{JOB_ID}/{suffix}"


# --- API ---------------------------------------------------------------------


def test_disable_with_duration_schedules_reenable(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    calls = _mock_api_pve(monkeypatch, enabled_after=False)

    before = datetime.now(UTC)
    response = client.post(
        _url(cluster_id, "disable"), json={"reenable_after_hours": 4}, headers=platform_admin_headers
    )
    assert response.status_code == 200, response.text
    assert calls == [(JOB_ID, False)]

    body = response.json()
    reenable_at = datetime.fromisoformat(body["reenable_at"])
    assert before + timedelta(hours=4) <= reenable_at <= datetime.now(UTC) + timedelta(hours=4)
    assert body["reenable_last_error"] is None

    row = _row(db_session, cluster_id)
    assert row is not None and row.reenable_at == reenable_at

    event = db_session.query(AuditEvent).filter_by(action="pve_vzdump_job_disabled").one()
    assert event.detail["reenable_at"] == reenable_at.isoformat()


def test_disable_without_body_is_indefinite_and_replaces_a_timer(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=False)
    client.post(_url(cluster_id, "disable"), json={"reenable_after_hours": 2}, headers=platform_admin_headers)
    assert _row(db_session, cluster_id) is not None

    response = client.post(_url(cluster_id, "disable"), headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["reenable_at"] is None
    assert _row(db_session, cluster_id) is None


@pytest.mark.parametrize("hours", [0, 169, -1])
def test_disable_rejects_out_of_range_duration(client, platform_admin_headers, monkeypatch, hours):
    cluster_id = _create_cluster(client, platform_admin_headers)
    calls = _mock_api_pve(monkeypatch, enabled_after=False)
    response = client.post(
        _url(cluster_id, "disable"), json={"reenable_after_hours": hours}, headers=platform_admin_headers
    )
    assert response.status_code == 422
    assert calls == []


def test_manual_enable_cancels_pending_timer(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=False)
    client.post(_url(cluster_id, "disable"), json={"reenable_after_hours": 8}, headers=platform_admin_headers)

    _mock_api_pve(monkeypatch, enabled_after=True)
    response = client.post(_url(cluster_id, "enable"), headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["reenable_at"] is None
    assert _row(db_session, cluster_id) is None


def test_set_timer_on_disabled_job(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    calls = _mock_api_pve(monkeypatch, enabled_after=False)

    response = client.put(
        _url(cluster_id, "reenable-schedule"), json={"reenable_after_hours": 12}, headers=platform_admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["reenable_at"] is not None
    assert calls == []  # the job itself is not touched in PVE
    assert _row(db_session, cluster_id) is not None
    assert db_session.query(AuditEvent).filter_by(action="pve_vzdump_job_reenable_scheduled").count() == 1


def test_set_timer_on_enabled_job_is_rejected(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=True)
    response = client.put(
        _url(cluster_id, "reenable-schedule"), json={"reenable_after_hours": 1}, headers=platform_admin_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "pve_vzdump_job_not_disabled"
    assert _row(db_session, cluster_id) is None


def test_cancel_timer(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=False)
    client.post(_url(cluster_id, "disable"), json={"reenable_after_hours": 3}, headers=platform_admin_headers)

    response = client.delete(_url(cluster_id, "reenable-schedule"), headers=platform_admin_headers)
    assert response.status_code == 204
    assert _row(db_session, cluster_id) is None
    assert db_session.query(AuditEvent).filter_by(action="pve_vzdump_job_reenable_cancelled").count() == 1

    again = client.delete(_url(cluster_id, "reenable-schedule"), headers=platform_admin_headers)
    assert again.status_code == 404
    assert again.json()["code"] == "reenable_schedule_not_found"


def test_deleting_the_job_drops_its_timer(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=False)
    client.post(_url(cluster_id, "disable"), json={"reenable_after_hours": 3}, headers=platform_admin_headers)
    monkeypatch.setattr(pve_clusters_module, "delete_vzdump_job", lambda *a, **kw: None)

    response = client.delete(f"/api/v1/pve-clusters/{cluster_id}/backup-jobs/{JOB_ID}", headers=platform_admin_headers)
    assert response.status_code == 204
    assert _row(db_session, cluster_id) is None


def test_job_list_shows_pending_timer(client, platform_admin_headers, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=False)
    client.post(_url(cluster_id, "disable"), json={"reenable_after_hours": 6}, headers=platform_admin_headers)

    jobs = client.get(f"/api/v1/pve-clusters/{cluster_id}/backup-jobs", headers=platform_admin_headers).json()
    assert jobs[0]["id"] == JOB_ID
    assert jobs[0]["reenable_at"] is not None


def test_reenable_endpoints_require_platform_admin(client):
    fake = uuid.uuid4()
    base = f"/api/v1/pve-clusters/{fake}/backup-jobs/{JOB_ID}/reenable-schedule"
    assert client.put(base, json={"reenable_after_hours": 1}).status_code in (401, 403)
    assert client.delete(base).status_code in (401, 403)


# --- scheduler pass ----------------------------------------------------------


@pytest.fixture
def due_row(client, platform_admin_headers, db_session, monkeypatch):
    """A disabled job whose timer ran out a minute ago."""
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=False)
    client.post(_url(cluster_id, "disable"), json={"reenable_after_hours": 1}, headers=platform_admin_headers)
    row = _row(db_session, cluster_id)
    row.reenable_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    return cluster_id


def _mock_scheduler_pve(monkeypatch, *, listing: VzdumpJobListResult, write_error: str | None = None):
    calls = []

    def _set(*a, job_id, enabled, **kw):
        if write_error:
            raise PveWriteError(write_error)
        calls.append((job_id, enabled))

    monkeypatch.setattr(pve_job_reenable, "list_vzdump_jobs", lambda *a, **kw: listing)
    monkeypatch.setattr(pve_job_reenable, "set_vzdump_job_enabled", _set)
    return calls


def test_scheduler_enables_due_job(due_row, db_session, test_settings, monkeypatch):
    calls = _mock_scheduler_pve(monkeypatch, listing=_listing(enabled=False))

    assert pve_job_reenable.run_due_reenables(db_session, test_settings) == 1
    assert calls == [(JOB_ID, True)]
    assert _row(db_session, due_row) is None
    event = db_session.query(AuditEvent).filter_by(action="pve_vzdump_job_auto_reenabled").one()
    assert event.actor_label == pve_job_reenable.AUTO_REENABLE_ACTOR_LABEL
    assert event.detail["job_id"] == JOB_ID


def test_scheduler_leaves_rows_that_are_not_due(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers)
    _mock_api_pve(monkeypatch, enabled_after=False)
    client.post(_url(cluster_id, "disable"), json={"reenable_after_hours": 1}, headers=platform_admin_headers)
    calls = _mock_scheduler_pve(monkeypatch, listing=_listing(enabled=False))

    assert pve_job_reenable.run_due_reenables(db_session, test_settings) == 0
    assert calls == []
    assert _row(db_session, cluster_id) is not None


def test_scheduler_keeps_row_and_records_error_when_pve_unreachable(due_row, db_session, test_settings, monkeypatch):
    calls = _mock_scheduler_pve(monkeypatch, listing=VzdumpJobListResult(reachable=False, error="connection refused"))
    pve_job_reenable.run_due_reenables(db_session, test_settings)

    assert calls == []
    row = _row(db_session, due_row)
    assert row is not None
    assert row.attempts == 1
    assert "connection refused" in row.last_error


def test_scheduler_keeps_row_and_records_error_when_write_fails(due_row, db_session, test_settings, monkeypatch):
    _mock_scheduler_pve(monkeypatch, listing=_listing(enabled=False), write_error="HTTP 403: Forbidden")
    pve_job_reenable.run_due_reenables(db_session, test_settings)

    row = _row(db_session, due_row)
    assert row is not None and row.attempts == 1 and row.last_error == "HTTP 403: Forbidden"
    assert db_session.query(AuditEvent).filter_by(action="pve_vzdump_job_auto_reenabled").count() == 0


def test_failed_attempt_is_shown_in_job_list(
    due_row, client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    _mock_scheduler_pve(monkeypatch, listing=_listing(enabled=False), write_error="HTTP 403: Forbidden")
    pve_job_reenable.run_due_reenables(db_session, test_settings)

    jobs = client.get(f"/api/v1/pve-clusters/{due_row}/backup-jobs", headers=platform_admin_headers).json()
    assert jobs[0]["reenable_last_error"] == "HTTP 403: Forbidden"


@pytest.mark.parametrize(
    ("listing", "reason"),
    [
        (_listing(enabled=False, job_id="some-other-job"), "job_not_found"),
        (_listing(enabled=True), "already_enabled"),
    ],
)
def test_scheduler_drops_row_without_writing(due_row, db_session, test_settings, monkeypatch, listing, reason):
    calls = _mock_scheduler_pve(monkeypatch, listing=listing)
    pve_job_reenable.run_due_reenables(db_session, test_settings)

    assert calls == []
    assert _row(db_session, due_row) is None
    event = db_session.query(AuditEvent).filter_by(action="pve_vzdump_job_auto_reenable_dropped").one()
    assert event.detail["reason"] == reason
