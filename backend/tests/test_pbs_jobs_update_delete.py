"""PBS job editing semantics (docs/adr/0023-backup-and-maintenance-job-administration):
omitted vs. explicitly-null fields on update (null -> PBS `delete`
parameter), non-clearable fields, sync-job delete removing the
Collector's cached row, the GC capability check and blank-GC-schedule
clearing. All PBS traffic is mocked at the `httpx` level -- no real PBS.

PBS's `delete` parameter shape (an array of property names, per-endpoint
enum) is taken from pbs.proxmox.com/docs/api-viewer/apidoc.js.
"""

import httpx
import pytest


def _create_pbs_instance(client, headers) -> str:
    payload = {
        "display_name": "Linked PBS",
        "host": "pbs1.internal.example",
        "port": 8007,
        "tls_trust_mode": "ca",
        "api_token": "test@pbs!token:secretvalue",
    }
    response = client.post("/api/v1/pbs-instances", json=payload, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def fake_pbs(monkeypatch):
    """Records every PUT/DELETE sent to PBS and answers GETs of a job's
    config path with `fake_pbs.job` (a PBS wire-format dict)."""

    class _Recorder:
        def __init__(self):
            self.job: dict = {}
            self.puts: list = []
            self.deletes: list = []

    recorder = _Recorder()

    def _put(url, **kwargs):
        recorder.puts.append((url, kwargs.get("data")))
        return _FakeResponse({"data": None})

    def _get(url, **kwargs):
        return _FakeResponse({"data": recorder.job})

    def _delete(url, **kwargs):
        recorder.deletes.append(url)
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", _put)
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "delete", _delete)
    return recorder


# --- Prune --------------------------------------------------------------------


def test_update_prune_job_null_fields_become_pbs_delete(client, platform_admin_headers, db_session, fake_pbs):
    from resurgam_api.models.audit import AuditEvent

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    fake_pbs.job = {"id": "p1", "store": "store1", "schedule": "21:00", "keep-daily": 14}

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1",
        json={"keep_daily": 14, "keep_weekly": None, "comment": ""},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    assert len(fake_pbs.puts) == 1
    url, data = fake_pbs.puts[0]
    assert url.endswith("/api2/json/config/prune/p1")
    assert data == {"keep-daily": 14, "delete": ["comment", "keep-weekly"]}
    assert response.json()["keep_weekly"] is None

    event = db_session.query(AuditEvent).filter_by(action="pbs_prune_job_updated").one()
    assert event.detail == {
        "job_id": "p1",
        "changed_fields": ["comment", "keep-daily", "keep-weekly"],
        "cleared_fields": ["comment", "keep-weekly"],
    }


def test_update_prune_job_omitted_fields_are_not_sent(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    fake_pbs.job = {"id": "p1", "store": "store1", "schedule": "22:00"}

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1",
        json={"schedule": "22:00", "disable": False},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert fake_pbs.puts[0][1] == {"schedule": "22:00", "disable": 0}


def test_update_prune_job_only_clears_is_a_valid_change(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    fake_pbs.job = {"id": "p1", "store": "store1", "schedule": "22:00"}

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1", json={"ns": None}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert fake_pbs.puts[0][1] == {"delete": ["ns"]}


def test_update_prune_job_clearing_schedule_is_rejected(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1", json={"schedule": None}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "field_not_clearable"
    assert "schedule" in response.json()["message"]
    assert fake_pbs.puts == []


def test_update_prune_job_rejects_keep_zero(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/prune-jobs/p1", json={"keep_daily": 0}, headers=platform_admin_headers
    )
    assert response.status_code == 422
    assert fake_pbs.puts == []


# --- Verify -------------------------------------------------------------------


def test_update_verify_job_can_clear_schedule(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    fake_pbs.job = {"id": "v1", "store": "store1"}

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/verify-jobs/v1",
        json={"schedule": None, "outdated_after": None, "ignore_verified": False},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    url, data = fake_pbs.puts[0]
    assert url.endswith("/api2/json/config/verify/v1")
    assert data == {"ignore-verified": 0, "delete": ["outdated-after", "schedule"]}


# --- Sync ---------------------------------------------------------------------


def test_update_sync_job_clears_schedule_and_owner(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    fake_pbs.job = {"id": "s1", "store": "store1", "remote-store": "main", "remote": "dr"}

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1",
        json={"schedule": None, "owner": "  ", "remove_vanished": True, "remote_store": "main"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    url, data = fake_pbs.puts[0]
    assert url.endswith("/api2/json/config/sync/s1")
    assert data == {"remote-store": "main", "remove-vanished": 1, "delete": ["owner", "schedule"]}
    assert response.json()["schedule"] is None


def test_update_sync_job_clearing_remote_store_is_rejected(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1",
        json={"remote_store": None},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "field_not_clearable"
    assert fake_pbs.puts == []


def _cached_sync_job(instance_id, job_id):
    from resurgam_api.models.sync import SyncJob

    return SyncJob(
        pbs_instance_id=instance_id,
        job_id=job_id,
        remote_name="dr-site",
        remote_store="main",
        local_store_name="store1",
        schedule="daily",
    )


def test_delete_sync_job_removes_cached_collector_row(client, platform_admin_headers, db_session, fake_pbs):
    from resurgam_api.models.sync import SyncJob

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    db_session.add_all([_cached_sync_job(instance_id, "s1"), _cached_sync_job(instance_id, "s2")])
    db_session.commit()

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1", headers=platform_admin_headers)
    assert response.status_code == 204
    assert fake_pbs.deletes[0].endswith("/api2/json/config/sync/s1")

    db_session.expire_all()
    remaining = [j.job_id for j in db_session.query(SyncJob).filter_by(pbs_instance_id=instance_id)]
    assert remaining == ["s2"]

    listed = client.get(f"/api/v1/pbs-instances/{instance_id}/sync-jobs", headers=platform_admin_headers).json()
    assert [j["job_id"] for j in listed] == ["s2"]


def test_delete_sync_job_failure_keeps_cached_row(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.sync import SyncJob

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    db_session.add(_cached_sync_job(instance_id, "s1"))
    db_session.commit()
    monkeypatch.setattr(httpx, "delete", lambda *a, **kw: _FakeResponse({"errors": "permission check failed"}, 403))

    response = client.delete(f"/api/v1/pbs-instances/{instance_id}/sync-jobs/s1", headers=platform_admin_headers)
    assert response.status_code == 502
    assert "403" in response.json()["message"]

    db_session.expire_all()
    assert db_session.query(SyncJob).filter_by(pbs_instance_id=instance_id, job_id="s1").count() == 1


# --- GC -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("privileges", "expected"),
    [
        (
            {"/": {"Datastore.Audit": 1, "Datastore.Modify": 1}},
            {"can_read": True, "can_write": True, "missing_write_privileges": []},
        ),
        (
            {"/": {"Datastore.Audit": 1}},
            {"can_read": True, "can_write": False, "missing_write_privileges": ["Datastore.Modify"]},
        ),
    ],
)
def test_gc_schedule_capabilities(client, platform_admin_headers, monkeypatch, privileges, expected):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": privileges}))
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/gc-schedule/capabilities", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json() == expected


def test_gc_schedule_blank_string_clears_via_pbs_delete(client, platform_admin_headers, fake_pbs):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.put(
        f"/api/v1/pbs-instances/{instance_id}/datastores/store1/gc-schedule",
        json={"gc_schedule": "   "},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"gc_schedule": None}
    url, data = fake_pbs.puts[0]
    assert url.endswith("/api2/json/config/datastore/store1")
    assert data == {"delete": "gc-schedule"}


# --- Connectivity helper ------------------------------------------------------


def test_update_sync_job_helper_sends_delete_list(monkeypatch):
    from resurgam_api.connectivity.models import TrustConfig
    from resurgam_api.connectivity.pbs_api import update_sync_job
    from resurgam_api.models.pbs import TlsTrustMode

    sent = {}

    def _put(url, **kwargs):
        sent["url"] = url
        sent["data"] = kwargs["data"]
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", _put)
    update_sync_job(
        "pbs.example",
        8007,
        TrustConfig(trust_mode=TlsTrustMode.CA),
        "token",
        5.0,
        job_id="s1",
        changes={"comment": "x"},
        delete=["schedule", "owner"],
    )
    assert sent["url"] == "https://pbs.example:8007/api2/json/config/sync/s1"
    assert sent["data"] == {"comment": "x", "delete": ["owner", "schedule"]}
