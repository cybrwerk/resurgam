"""Regression tests locking in response shapes captured from a real PBS 4.2
instance on 2026-09-13 (token/host anonymized) — see
docs/milestone-1-pbs-pve-api-assumptions.md for what these confirmed or
corrected versus the original fixture-only assumptions.
"""

import httpx

from resurgam_api.connectivity.models import PbsPruneJob, PbsVerifyJob, RemoteSyncJob, TrustConfig
from resurgam_api.connectivity.pbs_api import (
    PbsWriteError,
    create_prune_job,
    create_remote,
    create_sync_job,
    create_verify_job,
    delete_prune_job,
    delete_remote,
    delete_sync_job,
    get_datastore_gc_schedule,
    get_datastore_maintenance_mode,
    get_group_notes,
    get_own_permissions,
    get_prune_job,
    get_server_timezone,
    get_sync_job,
    get_task_log,
    list_acl_entries,
    list_datastore_usage,
    list_datastores,
    list_namespaces,
    list_prune_jobs,
    list_remotes,
    list_snapshots,
    list_sync_jobs,
    list_tasks,
    list_verify_jobs,
    probe_pbs,
    set_datastore_gc_schedule,
    set_datastore_maintenance_mode,
    update_prune_job,
    update_remote,
    update_sync_job,
)
from resurgam_api.models.pbs import TlsTrustMode

_TRUST = TrustConfig(trust_mode=TlsTrustMode.CA)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_probe_pbs_parses_real_version_shape(monkeypatch):
    payload = {"data": {"release": "5", "repoid": "5f890fcb6dc07148c22568ad03bf9c079bcad249", "version": "4.2"}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = probe_pbs("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert result.version == "4.2"


def test_list_datastores_parses_real_shape_with_no_comment(monkeypatch):
    # Real PBS omits "comment" entirely when unset, rather than sending null.
    payload = {
        "data": [
            {
                "name": "store1",
                "path": "/mnt/datastore/store1",
                "gc-schedule": "07:45",
                "notification-mode": "legacy-sendmail",
                "notify": "gc=always,prune=error,sync=error,verify=error",
            }
        ],
        "digest": "15ffef9e04cc2eeeaf5687e3329de4c52f4aecd6083e089d79f2dae3bcc58ad0",
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_datastores("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert len(result.datastores) == 1
    assert result.datastores[0].name == "store1"
    assert result.datastores[0].path == "/mnt/datastore/store1"
    assert result.datastores[0].comment is None


def test_list_tasks_parses_real_worker_type_and_worker_id_fields(monkeypatch):
    # Real PBS uses `worker_type`/`worker_id`, not `type`/`id` as originally
    # assumed — the original field names would KeyError on every real call.
    payload = {
        "total": 2,
        "data": [
            {
                "upid": "UPID:pbs:00015F37:045D70E1:00000C96:6AA6E4A4:backup:store:vm-100:user@pbs:",
                "node": "localhost",
                "pid": 89911,
                "pstart": 73232609,
                "starttime": 1789322404,
                "worker_type": "backup",
                "worker_id": "store:vm/100",
                "user": "user@pbs",
                "endtime": 1789322410,
                "status": "OK",
            },
            {
                "upid": "UPID:pbs:00015F37:045D70E1:00000C8C:6AA68230:prunejob:store:root@pam:",
                "node": "localhost",
                "pid": 89911,
                "pstart": 73232609,
                "starttime": 1789297200,
                "worker_type": "prunejob",
                "worker_id": "store",
                "user": "root@pam",
                "endtime": 1789297201,
                "status": "OK",
            },
        ],
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_tasks("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert len(result.tasks) == 2
    assert result.tasks[0].task_type == "backup"
    assert result.tasks[0].worker_id == "store:vm/100"
    assert result.tasks[1].task_type == "prunejob"
    assert result.tasks[1].worker_id == "store"


def test_list_tasks_sends_limit_param(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        captured["params"] = params
        return _FakeResponse({"total": 0, "data": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    list_tasks("pbs.example", 8007, _TRUST, "token", limit=500)

    assert captured["params"] == {"limit": 500}


def test_get_group_notes_parses_real_empty_string_shape(monkeypatch):
    # PBS returns {"data": ""} for a group with no note set — not null,
    # not a 404 — confirmed against a real instance.
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": ""}))

    result = get_group_notes("pbs.example", 8007, _TRUST, "token", "store", "vm", "100")

    assert result.reachable is True
    assert result.notes == ""


def test_get_group_notes_sends_backup_type_and_id_params(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        captured["params"] = params
        return _FakeResponse({"data": "web-server-01"})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = get_group_notes("pbs.example", 8007, _TRUST, "token", "store", "vm", "100", namespace="team-a")

    assert result.notes == "web-server-01"
    assert captured["params"] == {"backup-type": "vm", "backup-id": "100", "ns": "team-a"}


def test_list_datastore_usage_parses_real_shape(monkeypatch):
    # Trimmed real payload from /api2/json/status/datastore-usage — the
    # `history`/`gc-status`/`estimated-full-date` fields are real but
    # intentionally unused by list_datastore_usage (see its docstring).
    payload = {
        "data": [
            {
                "store": "backup-store",
                "total": 4293392338944,
                "used": 2178934128640,
                "avail": 2114458210304,
                "backend-type": "filesystem",
                "mount-status": "nonremovable",
                "history-delta": 1800,
                "history-start": 1786740486,
                "history": [0.48, 0.49],
                "gc-status": {"disk-bytes": 2081435634409},
            }
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_datastore_usage("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert len(result.usage) == 1
    assert result.usage[0].name == "backup-store"
    assert result.usage[0].used_bytes == 2178934128640
    assert result.usage[0].available_bytes == 2114458210304
    assert result.usage[0].total_bytes == 4293392338944


def test_list_namespaces_parses_real_shape(monkeypatch):
    # Real payload from a datastore with namespaced backups (2026-09-14).
    payload = {"data": [{"ns": ""}, {"ns": "site-b"}, {"ns": "site-b/tenant-b"}, {"ns": "tenant-a"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_namespaces("pbs.example", 8007, _TRUST, "token", "store1")

    assert result.reachable is True
    assert result.namespaces == ["", "site-b", "site-b/tenant-b", "tenant-a"]


def test_list_snapshots_aggregates_across_namespaces(monkeypatch):
    # Confirmed live (2026-09-14): a datastore's root namespace can be
    # completely empty while real snapshots exist under other namespaces
    # — /snapshots is scoped to exactly one namespace per call, and a
    # namespaced entry doesn't carry its own backup-namespace field back.
    namespaces_payload = {"data": [{"ns": ""}, {"ns": "tenant-a"}]}
    tenant_a_snapshots_payload = {
        "data": [
            {
                "backup-type": "ct",
                "backup-id": "100",
                "backup-time": 1695168003,
                "comment": "log.example.com",
                "verification": {"state": "ok"},
                "size": 2103844100,
                "owner": "tenant-a@pbs",
            }
        ]
    }

    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        if url.endswith("/namespace"):
            return _FakeResponse(namespaces_payload)
        if params and params.get("ns") == "tenant-a":
            return _FakeResponse(tenant_a_snapshots_payload)
        return _FakeResponse({"data": []})  # root namespace: empty, as observed live

    monkeypatch.setattr(httpx, "get", fake_get)

    result = list_snapshots("pbs.example", 8007, _TRUST, "token", "store1")

    assert result.reachable is True
    assert len(result.snapshots) == 1
    assert result.snapshots[0].namespace == "tenant-a"
    assert result.snapshots[0].backup_id == "100"
    assert result.snapshots[0].verify_state == "ok"
    assert result.snapshots[0].size == 2103844100
    assert result.snapshots[0].owner == "tenant-a@pbs"
    assert result.snapshots[0].comment == "log.example.com"


def test_list_snapshots_falls_back_to_root_when_namespace_listing_fails(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        if url.endswith("/namespace"):
            return _FakeResponse({}, status_code=500)
        return _FakeResponse({"data": [{"backup-type": "vm", "backup-id": "100", "backup-time": 1_700_000_000}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = list_snapshots("pbs.example", 8007, _TRUST, "token", "store1")

    assert result.reachable is True
    assert len(result.snapshots) == 1
    assert result.snapshots[0].namespace == ""


def test_list_snapshots_fails_if_a_namespace_query_fails(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        if url.endswith("/namespace"):
            return _FakeResponse({"data": [{"ns": ""}, {"ns": "broken"}]})
        if params and params.get("ns") == "broken":
            return _FakeResponse({}, status_code=500)
        return _FakeResponse({"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = list_snapshots("pbs.example", 8007, _TRUST, "token", "store1")

    assert result.reachable is False


# --- get_task_log: real shape confirmed live 2026-09-17 against a real
# PBS 4.2 instance -- {"data": [{"n": <int>, "t": "<str>"}, ...], "success":
# 1, "total": <int>}, `total` outside `data`; `start` is a 1-indexed
# absolute line number, `start=0` a special-cased alias for "from the
# beginning" -- see get_task_log's own docstring and ADR/assumptions doc.


def test_get_task_log_fetches_the_tail_using_the_real_total(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        calls.append(dict(params))
        if params["limit"] == 1:
            return _FakeResponse({"data": [{"n": 1, "t": "first"}], "success": 1, "total": 21})
        return _FakeResponse(
            {
                "data": [
                    {"n": 19, "t": "successfully finished backup"},
                    {"n": 20, "t": "backup finished successfully"},
                    {"n": 21, "t": "TASK OK"},
                ],
                "success": 1,
                "total": 21,
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    result = get_task_log("pbs.example", 8007, _TRUST, "token", upid="UPID:pbs:1:backup:root@pam:", tail_lines=3)

    assert result.reachable is True
    assert result.total == 21
    assert [line.n for line in result.lines] == [19, 20, 21]
    assert result.lines[-1].text == "TASK OK"
    assert result.truncated is True
    # second call must request the correctly-computed 1-indexed tail offset
    assert calls[1]["start"] == 19  # max(1, 21 - 3 + 1)


def test_get_task_log_returns_everything_when_shorter_than_tail_lines(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        if params["limit"] == 1:
            return _FakeResponse({"data": [{"n": 1, "t": "first"}], "success": 1, "total": 2})
        return _FakeResponse({"data": [{"n": 1, "t": "first"}, {"n": 2, "t": "TASK OK"}], "success": 1, "total": 2})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = get_task_log("pbs.example", 8007, _TRUST, "token", upid="UPID:pbs:1:backup:root@pam:", tail_lines=5000)

    assert result.reachable is True
    assert result.truncated is False
    assert len(result.lines) == 2


def test_get_task_log_falls_back_to_head_when_no_total_available(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        if params["limit"] == 1:
            return _FakeResponse({"data": [{"n": 1, "t": "first"}], "success": 1})  # no "total" key at all
        return _FakeResponse({"data": [{"n": 1, "t": "first"}], "success": 1})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = get_task_log("pbs.example", 8007, _TRUST, "token", upid="UPID:pbs:1:backup:root@pam:", tail_lines=5000)

    assert result.reachable is True
    assert result.total is None
    assert result.truncated is False
    assert len(result.lines) == 1


def test_get_task_log_reachable_false_on_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({}, status_code=500))

    result = get_task_log("pbs.example", 8007, _TRUST, "token", upid="UPID:pbs:1:backup:root@pam:")

    assert result.reachable is False
    assert result.error is not None


def test_get_datastore_maintenance_mode_parses_real_field_shape(monkeypatch):
    # Confirmed live 2026-09-19 against the real instance, after two wrong
    # guesses: the wire format is a PBS property string with `mode` bare/
    # positional (no `type=` prefix) and only `message` explicitly keyed --
    # `<mode>[,message=<text>]`. `type[,message]` free-text 400's live
    # ("parameter verification failed... 'type': duplicate key"); an
    # explicit `type=<mode>,message=<text>` PUT is *accepted* but GET then
    # echoes it back without the `type=` prefix, proving that's not the
    # real stored/round-tripped shape either.
    payload = {"data": {"name": "backup-store", "maintenance-mode": "read-only,message=planned work"}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = get_datastore_maintenance_mode("pbs.example", 8007, _TRUST, "token", 5.0, name="backup-store")

    assert result.reachable is True
    assert result.mode == "read-only"
    assert result.message == "planned work"


def test_get_datastore_maintenance_mode_without_message(monkeypatch):
    payload = {"data": {"name": "backup-store", "maintenance-mode": "offline"}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = get_datastore_maintenance_mode("pbs.example", 8007, _TRUST, "token", 5.0, name="backup-store")

    assert result.reachable is True
    assert result.mode == "offline"
    assert result.message is None


def test_get_datastore_maintenance_mode_absent_when_unset(monkeypatch):
    payload = {"data": {"name": "backup-store"}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = get_datastore_maintenance_mode("pbs.example", 8007, _TRUST, "token", 5.0, name="backup-store")

    assert result.reachable is True
    assert result.mode is None
    assert result.message is None


def test_set_datastore_maintenance_mode_sends_mode_and_message(monkeypatch):
    captured = {}

    def fake_put(url, headers=None, data=None, timeout=None, verify=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", fake_put)

    set_datastore_maintenance_mode(
        "pbs.example", 8007, _TRUST, "token", 5.0, name="backup-store", mode="read-only", message="planned work"
    )

    assert captured["url"].endswith("/config/datastore/backup-store")
    # Quoted, so a comma inside the message stays part of it (live 2026-11-21).
    assert captured["data"] == {"maintenance-mode": 'read-only,message="planned work"'}


def test_set_datastore_maintenance_mode_clears_with_delete_param(monkeypatch):
    captured = {}

    def fake_put(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", fake_put)

    set_datastore_maintenance_mode(
        "pbs.example", 8007, _TRUST, "token", 5.0, name="backup-store", mode=None, message=None
    )

    assert captured["data"] == {"delete": "maintenance-mode"}


def test_set_datastore_maintenance_mode_raises_on_malformed_json_response(monkeypatch):
    """Regression test: a 200-status response with an unparseable body
    (a real PBS bug or a proxy mangling the response) must surface as a
    `PbsWriteError`, not be silently treated as a successful write with
    no error -- `_put_data`'s JSON-decode-failure handling previously
    diverged from `pve_api.py`'s (returned `(None, None)` instead of an
    error message), now normalized via the shared `connectivity._http`
    helper both modules use.
    """

    class _UnparseableResponse:
        status_code = 200
        text = "not json"

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(httpx, "put", lambda *a, **kw: _UnparseableResponse())

    try:
        set_datastore_maintenance_mode(
            "pbs.example", 8007, _TRUST, "token", 5.0, name="backup-store", mode="offline", message=None
        )
        assert False, "expected PbsWriteError"
    except PbsWriteError as exc:
        assert "invalid JSON response" in str(exc)


def test_set_datastore_maintenance_mode_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        httpx, "put", lambda *a, **kw: _FakeResponse({"message": "Permission check failed"}, status_code=403)
    )

    try:
        set_datastore_maintenance_mode(
            "pbs.example", 8007, _TRUST, "token", 5.0, name="backup-store", mode="offline", message=None
        )
        assert False, "expected PbsWriteError"
    except PbsWriteError:
        pass


def test_list_acl_entries(monkeypatch):
    # Real shape captured live (2026-09-20): `propagate` is already a
    # JSON boolean on the wire, not the 0/1 int some other PBS endpoints
    # use (e.g. list_vzdump_jobs's `enabled`).
    payload = {
        "data": [
            {"path": "/", "propagate": True, "roleid": "Admin", "ugid": "root@pam", "ugid_type": "user"},
            {
                "path": "/datastore/backup-store",
                "propagate": True,
                "roleid": "Admin",
                "ugid": "tenant-a@pbs",
                "ugid_type": "user",
            },
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_acl_entries("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert len(result.entries) == 2
    assert result.entries[1].path == "/datastore/backup-store"
    assert result.entries[1].role == "Admin"
    assert result.entries[1].subject == "tenant-a@pbs"
    assert result.entries[1].subject_type == "user"
    assert result.entries[1].propagate is True


# --- Prune/Verify/Sync/Remote job CRUD + capability gating (docs/adr/0023-backup-and-maintenance-job-administration) ------
#
# Field shapes below are confirmed live (2026-09-26) against a real PBS
# 4.2 instance via full create/get/delete cycles -- not guessed from docs
# (unlike the original `list_remotes`/`list_sync_jobs` this project
# shipped with, whose own docstrings said as much at the time).


def test_list_prune_jobs_parses_real_shape(monkeypatch):
    payload = {
        "data": [
            {
                "id": "s-906b70a8-3c16",
                "store": "backup-store",
                "ns": "tenant-a",
                "schedule": "21:00",
                "keep-daily": 12,
            },
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_prune_jobs("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert result.jobs[0].keep_daily == 12
    assert result.jobs[0].ns == "tenant-a"


def test_get_prune_job_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"message": "no such job"}, status_code=404))

    assert get_prune_job("pbs.example", 8007, _TRUST, "token", 5.0, job_id="nope") is None


def test_create_prune_job_requires_explicit_id():
    job = PbsPruneJob(id="", store="store1", schedule="05:00")
    try:
        create_prune_job("pbs.example", 8007, _TRUST, "token", 5.0, job=job)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_create_prune_job_sends_id_and_keep_fields(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "post", fake_post)

    job = PbsPruneJob(id="resurgam-abc123", store="store1", schedule="05:00", keep_daily=7, keep_weekly=4)
    create_prune_job("pbs.example", 8007, _TRUST, "token", 5.0, job=job)

    assert captured["data"]["id"] == "resurgam-abc123"
    assert captured["data"]["keep-daily"] == 7
    assert captured["data"]["keep-weekly"] == 4


def test_update_prune_job_sends_only_given_changes(monkeypatch):
    captured = {}

    def fake_put(url, headers=None, data=None, timeout=None, verify=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", fake_put)

    update_prune_job("pbs.example", 8007, _TRUST, "token", 5.0, job_id="s-abc", changes={"keep-daily": 14})

    assert captured["url"].endswith("/config/prune/s-abc")
    assert captured["data"] == {"keep-daily": 14}


def test_delete_prune_job_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "delete", lambda *a, **kw: _FakeResponse({"message": "no such job"}, status_code=400))

    try:
        delete_prune_job("pbs.example", 8007, _TRUST, "token", 5.0, job_id="nope")
        assert False, "expected PbsWriteError"
    except PbsWriteError:
        pass


def test_list_verify_jobs_parses_real_shape(monkeypatch):
    payload = {"data": [{"id": "v-c11416b0-3304", "store": "backup-store", "ns": "tenant-a", "outdated-after": 30}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_verify_jobs("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert result.jobs[0].outdated_after == 30


def test_create_verify_job_requires_explicit_id():
    job = PbsVerifyJob(id="", store="store1")
    try:
        create_verify_job("pbs.example", 8007, _TRUST, "token", 5.0, job=job)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_create_verify_job_sends_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: (captured.update(data=kw.get("data")), _FakeResponse({"data": None}))[1]
    )

    job = PbsVerifyJob(id="resurgam-verify1", store="store1", outdated_after=15)
    create_verify_job("pbs.example", 8007, _TRUST, "token", 5.0, job=job)

    assert captured["data"]["id"] == "resurgam-verify1"
    assert captured["data"]["outdated-after"] == 15


def test_get_own_permissions_pbs_parses_real_shape(monkeypatch):
    # Real shape confirmed live 2026-09-26 against this backend's own
    # actual PBS write token.
    payload = {"data": {"/": {"Datastore.Modify": True, "Datastore.Verify": True, "Remote.Audit": True}}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = get_own_permissions("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert result.privileges["/"]["Datastore.Modify"] is True


def test_get_own_permissions_pbs_reports_unreachable_on_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"message": "unauthorized"}, status_code=401))

    result = get_own_permissions("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is False


def test_list_remotes_parses_real_shape_including_auth_id(monkeypatch):
    # Confirmed live 2026-09-26: `auth-id`/`fingerprint` are real fields
    # this project's original (unverified) RemoteRemote model was missing
    # entirely -- `password` is write-only, never returned.
    payload = {"data": [{"name": "local", "host": "pbs1.example.internal", "auth-id": "root@pam"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_remotes("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert result.remotes[0].auth_id == "root@pam"


def test_create_remote_sends_password_and_omits_optional_none_fields(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: (captured.update(data=kw.get("data")), _FakeResponse({"data": None}))[1]
    )

    create_remote(
        "pbs.example",
        8007,
        _TRUST,
        "token",
        5.0,
        name="offsite",
        remote_host="pbs2.example.com",
        auth_id="sync@pbs!token",
        password="secret-value",
    )

    assert captured["data"]["password"] == "secret-value"
    assert captured["data"]["auth-id"] == "sync@pbs!token"
    assert "fingerprint" not in captured["data"]


def test_delete_remote_calls_delete(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "delete", lambda url, **kw: (captured.update(url=url), _FakeResponse({"data": None}))[1])

    delete_remote("pbs.example", 8007, _TRUST, "token", 5.0, name="offsite")

    assert captured["url"].endswith("/config/remote/offsite")


def test_update_remote_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "put", lambda *a, **kw: _FakeResponse({"message": "no such remote"}, status_code=400))

    try:
        update_remote("pbs.example", 8007, _TRUST, "token", 5.0, name="nope", changes={"host": "new.example.com"})
        assert False, "expected PbsWriteError"
    except PbsWriteError:
        pass


def test_list_sync_jobs_parses_real_shape_remote_optional(monkeypatch):
    # Confirmed live 2026-09-26: `remote` is genuinely optional (a real
    # PBS 4.x feature -- omitting it means an intra-instance sync).
    payload = {"data": [{"id": "claude-test-sync", "remote": "local", "remote-store": "store1", "store": "store1"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(payload))

    result = list_sync_jobs("pbs.example", 8007, _TRUST, "token")

    assert result.reachable is True
    assert result.sync_jobs[0].remote_name == "local"
    assert result.sync_jobs[0].local_store == "store1"


def test_get_sync_job_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"message": "not found"}, status_code=404))

    assert get_sync_job("pbs.example", 8007, _TRUST, "token", 5.0, job_id="nope") is None


def test_create_sync_job_requires_explicit_id():
    job = RemoteSyncJob(job_id="", local_store="store1", remote_store="store1")
    try:
        create_sync_job("pbs.example", 8007, _TRUST, "token", 5.0, job=job)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_create_sync_job_sends_real_field_names(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: (captured.update(data=kw.get("data")), _FakeResponse({"data": None}))[1]
    )

    job = RemoteSyncJob(
        job_id="resurgam-sync1", local_store="store1", remote_store="store1", remote_name="offsite", schedule="04:00"
    )
    create_sync_job("pbs.example", 8007, _TRUST, "token", 5.0, job=job)

    assert captured["data"] == {
        "id": "resurgam-sync1",
        "store": "store1",
        "remote-store": "store1",
        "remote": "offsite",
        "schedule": "04:00",
    }


def test_update_sync_job_sends_only_given_changes(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        httpx,
        "put",
        lambda url, **kw: (captured.update(url=url, data=kw.get("data")), _FakeResponse({"data": None}))[1],
    )

    update_sync_job("pbs.example", 8007, _TRUST, "token", 5.0, job_id="s1", changes={"schedule": "06:00"})

    assert captured["url"].endswith("/config/sync/s1")
    assert captured["data"] == {"schedule": "06:00"}


def test_delete_sync_job_raises_on_failure(monkeypatch):
    monkeypatch.setattr(httpx, "delete", lambda *a, **kw: _FakeResponse({"message": "no such job"}, status_code=400))

    try:
        delete_sync_job("pbs.example", 8007, _TRUST, "token", 5.0, job_id="nope")
        assert False, "expected PbsWriteError"
    except PbsWriteError:
        pass


def test_get_datastore_gc_schedule_parses_field(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **kw: _FakeResponse({"data": {"name": "store1", "gc-schedule": "03:00"}})
    )

    reachable, schedule, error = get_datastore_gc_schedule("pbs.example", 8007, _TRUST, "token", 5.0, name="store1")

    assert reachable is True
    assert schedule == "03:00"
    assert error is None


def test_get_datastore_gc_schedule_absent_when_unset(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse({"data": {"name": "store1"}}))

    reachable, schedule, _error = get_datastore_gc_schedule("pbs.example", 8007, _TRUST, "token", 5.0, name="store1")

    assert reachable is True
    assert schedule is None


def test_set_datastore_gc_schedule_sends_field(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        httpx,
        "put",
        lambda url, **kw: (captured.update(url=url, data=kw.get("data")), _FakeResponse({"data": None}))[1],
    )

    set_datastore_gc_schedule("pbs.example", 8007, _TRUST, "token", 5.0, name="store1", schedule="04:30")

    assert captured["url"].endswith("/config/datastore/store1")
    assert captured["data"] == {"gc-schedule": "04:30"}


def test_clear_datastore_gc_schedule_sends_delete_field(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        httpx, "put", lambda url, **kw: (captured.update(data=kw.get("data")), _FakeResponse({"data": None}))[1]
    )

    set_datastore_gc_schedule("pbs.example", 8007, _TRUST, "token", 5.0, name="store1", schedule=None)

    assert captured["data"] == {"delete": "gc-schedule"}


def test_set_datastore_gc_schedule_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        httpx, "put", lambda *a, **kw: _FakeResponse({"message": "Permission check failed"}, status_code=403)
    )

    try:
        set_datastore_gc_schedule("pbs.example", 8007, _TRUST, "token", 5.0, name="store1", schedule="04:30")
        assert False, "expected PbsWriteError"
    except PbsWriteError:
        pass


def test_get_server_timezone_reads_the_timezone_field(monkeypatch):
    # Shape of /api2/json/nodes/localhost/time (src/api2/node/time.rs).
    captured = {}

    def fake_get(url, *a, **kw):
        captured["url"] = url
        return _FakeResponse({"data": {"timezone": "Europe/Zurich", "time": 1794632400, "localtime": 1794636000}})

    monkeypatch.setattr(httpx, "get", fake_get)

    assert get_server_timezone("pbs.example", 8007, _TRUST, "token") == "Europe/Zurich"
    assert captured["url"].endswith("/api2/json/nodes/localhost/time")


def test_get_server_timezone_returns_none_for_a_missing_or_odd_value(monkeypatch):
    for payload in ({"data": {"time": 1}}, {"data": {"timezone": ""}}, {"data": {"timezone": "x" * 65}}, {"data": []}):
        monkeypatch.setattr(httpx, "get", lambda *a, _p=payload, **kw: _FakeResponse(_p))
        assert get_server_timezone("pbs.example", 8007, _TRUST, "token") is None


def test_maintenance_message_with_comma_and_quotes_round_trips(monkeypatch):
    """Live finding 2026-11-21: an unquoted message containing a comma was
    rejected by PBS ("multiple values without a key in property string").
    The stored shape below is what PBS 4 returned for the quoted value."""
    captured = {}

    def fake_put(url, headers=None, data=None, timeout=None, verify=None):
        captured["data"] = data
        return _FakeResponse({"data": None})

    monkeypatch.setattr(httpx, "put", fake_put)
    message = 'Storage upgrade, back at 14:00 "soon"'
    set_datastore_maintenance_mode("pbs.example", 8007, _TRUST, "t", 5.0, name="ds", mode="read-only", message=message)
    assert captured["data"] == {"maintenance-mode": 'read-only,message="Storage upgrade, back at 14:00 \\"soon\\""'}

    stored = {"data": {"name": "ds", "maintenance-mode": captured["data"]["maintenance-mode"]}}
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResponse(stored))
    result = get_datastore_maintenance_mode("pbs.example", 8007, _TRUST, "t", 5.0, name="ds")
    assert (result.mode, result.message) == ("read-only", message)
