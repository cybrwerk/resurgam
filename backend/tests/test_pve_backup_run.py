"""Backup Jobs -> "Run now" and the live task viewer. PVE is mocked HTTP."""

from tests.proxmox_fakes import FakeProxmox, FakeResponse
from tests.test_lab_provisioning_api import BASE, _create_cluster

UPID1 = "UPID:pve1:0000A1B2:00C0FFEE:6708F0A1:vzdump::resurgam@pve!resurgam:"
UPID2 = "UPID:pve2:0000A1B3:00C0FFEF:6708F0A2:vzdump::resurgam@pve!resurgam:"
GUESTS = [
    {"vmid": 101, "node": "pve1", "type": "qemu", "name": "web"},
    {"vmid": 102, "node": "pve2", "type": "qemu", "name": "db"},
    {"vmid": 103, "node": "pve1", "type": "lxc", "name": "dns"},
]


def _pve(monkeypatch, job: dict, *, fail_node: str | None = None):
    def vzdump(url, kwargs):
        node = url.split("/nodes/")[1].split("/")[0]
        if node == fail_node:
            return FakeResponse({"errors": "permission denied"}, status_code=403, text="permission denied")
        return FakeResponse({"data": UPID1 if node == "pve1" else UPID2})

    return FakeProxmox(
        routes={
            ("GET", f"/api2/json/cluster/backup/{job['id']}"): FakeResponse({"data": job}),
            ("GET", "/api2/json/cluster/resources"): FakeResponse({"data": GUESTS}),
            ("POST", "/nodes/pve1/vzdump"): vzdump,
            ("POST", "/nodes/pve2/vzdump"): vzdump,
            ("GET", f"/tasks/{UPID1}/status"): FakeResponse({"data": {"status": "running"}}),
            ("GET", f"/tasks/{UPID1}/log"): FakeResponse(
                {
                    "data": [
                        {"n": 1, "t": "INFO: starting new backup job"},
                        {"n": 2, "t": "INFO: Starting Backup of VM 101"},
                    ]
                }
            ),
        }
    ).install(monkeypatch)


def _sent(fake, node):
    return [c[3] for c in fake.called("POST", f"/nodes/{node}/vzdump")]


def test_run_vmid_job_groups_guests_by_node(client, platform_admin_headers, db_session, monkeypatch):
    fake = _pve(
        monkeypatch,
        {
            "id": "backup-1",
            "enabled": 0,
            "vmid": "101,102,999",
            "storage": "pbs",
            "mode": "snapshot",
            "prune-backups": {"keep-daily": "7"},
        },
    )
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-1/run", headers=platform_admin_headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert [(t["node"], t["vmids"]) for t in body["tasks"]] == [("pve1", [101]), ("pve2", [102])]
    assert body["skipped"] == [{"vmid": 999, "reason": "missing"}]
    sent = _sent(fake, "pve1")[0]
    assert sent["vmid"] == "101" and sent["storage"] == "pbs" and sent["mode"] == "snapshot"
    # Like a scheduled run: the job's own retention is applied (write-side flat property string).
    assert sent["prune-backups"] == "keep-daily=7"


def test_run_job_without_retention_omits_prune_backups(client, platform_admin_headers, db_session, monkeypatch):
    fake = _pve(monkeypatch, {"id": "backup-6", "vmid": "101", "storage": "pbs"})
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    assert (
        client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-6/run", headers=platform_admin_headers).status_code == 202
    )
    assert "prune-backups" not in _sent(fake, "pve1")[0]


def test_run_node_bound_vmid_job_reports_guests_on_other_nodes(client, platform_admin_headers, db_session, monkeypatch):
    fake = _pve(monkeypatch, {"id": "backup-7", "vmid": "101,102", "node": "pve1", "storage": "pbs"})
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    body = client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-7/run", headers=platform_admin_headers).json()
    assert [(t["node"], t["vmids"]) for t in body["tasks"]] == [("pve1", [101])]
    assert body["skipped"] == [{"vmid": 102, "reason": "other_node"}]
    assert not _sent(fake, "pve2")

    _pve(monkeypatch, {"id": "backup-8", "vmid": "102,999", "node": "pve1", "storage": "pbs"})
    response = client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-8/run", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["details"]["skipped"] == [
        {"vmid": 102, "reason": "other_node"},
        {"vmid": 999, "reason": "missing"},
    ]


def test_run_all_job_runs_on_every_node_with_exclusions(client, platform_admin_headers, db_session, monkeypatch):
    fake = _pve(monkeypatch, {"id": "backup-2", "enabled": 1, "all": 1, "exclude": "103", "storage": "pbs"})
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    body = client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-2/run", headers=platform_admin_headers).json()
    assert [t["node"] for t in body["tasks"]] == ["pve1", "pve2"]
    assert _sent(fake, "pve2")[0]["all"] == 1 and _sent(fake, "pve2")[0]["exclude"] == "103"


def test_run_node_bound_job_and_partial_failure(client, platform_admin_headers, db_session, monkeypatch):
    _pve(monkeypatch, {"id": "backup-3", "all": 1, "node": "pve1", "storage": "pbs"})
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    body = client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-3/run", headers=platform_admin_headers).json()
    assert [t["node"] for t in body["tasks"]] == ["pve1"]

    _pve(monkeypatch, {"id": "backup-4", "all": 1, "storage": "pbs"}, fail_node="pve2")
    body = client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-4/run", headers=platform_admin_headers).json()
    assert [t["node"] for t in body["tasks"]] == ["pve1"]


def test_run_nothing_selected_and_unknown_job(client, platform_admin_headers, db_session, monkeypatch):
    _pve(monkeypatch, {"id": "backup-5", "vmid": "999", "storage": "pbs"})
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-5/run", headers=platform_admin_headers)
    assert response.status_code == 409 and response.json()["code"] == "backup_job_selects_nothing"
    assert (
        client.post(f"{BASE}/{cluster_id}/backup-jobs/missing/run", headers=platform_admin_headers).status_code == 404
    )


def test_task_progress_returns_status_and_new_lines(client, platform_admin_headers, db_session, monkeypatch):
    fake = _pve(monkeypatch, {"id": "backup-1", "vmid": "101", "storage": "pbs"})
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    response = client.get(f"{BASE}/{cluster_id}/tasks/pve1/{UPID1}?start=0", headers=platform_admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    # PVE's `n` is 1-based and `start` is the number of lines to skip: after n=1,2 the next start is 2.
    assert body["status"] == "running" and body["next_start"] == 2 and body["more"] is False
    assert [line["t"] for line in body["lines"]][-1] == "INFO: Starting Backup of VM 101"
    log_call = fake.called("GET", f"/tasks/{UPID1}/log")
    assert log_call
    assert client.get(f"{BASE}/{cluster_id}/tasks/pve2/{UPID1}", headers=platform_admin_headers).status_code == 400
    assert client.get(f"{BASE}/{cluster_id}/tasks/pve1/not-a-upid", headers=platform_admin_headers).status_code == 422


def test_run_now_requires_platform_admin(client, db_session, platform_admin_headers):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    assert client.post(f"{BASE}/{cluster_id}/backup-jobs/backup-1/run").status_code == 401


def _dump_logfile(log: list[str], start: int, limit: int) -> list[dict]:
    """PVE's `PVE::Tools::dump_logfile`, transcribed: skips `start` lines,
    numbers lines from 1, and returns a fake `no content` line for an empty
    file."""
    out, count = [], 0
    for line in log:
        count += 1
        if count <= start:
            continue
        if limit <= 0:
            continue
        limit -= 1
        out.append({"n": count, "t": line})
    if count == 0:
        out.append({"n": 1, "t": "no content"})
    return out


def test_task_progress_follows_a_growing_log_without_losing_lines(
    client, platform_admin_headers, db_session, monkeypatch
):
    log: list[str] = []

    def task_log(url, kwargs):
        params = kwargs.get("params") or {}
        return FakeResponse({"data": _dump_logfile(log, int(params["start"]), int(params["limit"]))})

    fake = _pve(monkeypatch, {"id": "backup-1", "vmid": "101", "storage": "pbs"})
    fake.routes[("GET", f"/tasks/{UPID1}/log")] = task_log
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)

    seen: list[str] = []
    start = 0

    def poll():
        nonlocal start
        body = client.get(
            f"{BASE}/{cluster_id}/tasks/pve1/{UPID1}?start={start}", headers=platform_admin_headers
        ).json()
        seen.extend(line["t"] for line in body["lines"])
        start = body["next_start"]
        return body

    body = poll()  # empty log: PVE's placeholder is hidden and the cursor does not move
    assert body["lines"] == [] and body["next_start"] == 0
    log.extend(["line 1", "line 2"])
    poll()
    log.append("line 3")
    poll()
    poll()  # nothing new
    log.extend(f"bulk {i}" for i in range(600))
    body = poll()
    assert body["more"] is True and len(body["lines"]) == 500
    body = poll()
    assert body["more"] is False
    assert seen == ["line 1", "line 2", "line 3", *[f"bulk {i}" for i in range(600)]]
    assert start == 603


def test_task_progress_node_check_is_exact_and_upid_is_quoted(client, platform_admin_headers, db_session, monkeypatch):
    upid = "UPID:pve1:0000A1B2:00C0FFEE:6708F0A1:vzdump:a?b#c:resurgam@pve!resurgam:"
    fake = _pve(monkeypatch, {"id": "backup-1", "vmid": "101", "storage": "pbs"})
    fake.routes[("GET", "/status")] = FakeResponse({"data": {"status": "stopped", "exitstatus": "OK"}})
    fake.routes[("GET", "/log")] = FakeResponse({"data": []})
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    quoted = "UPID:pve1:0000A1B2:00C0FFEE:6708F0A1:vzdump:a%3Fb%23c:resurgam@pve!resurgam:"
    response = client.get(f"{BASE}/{cluster_id}/tasks/pve1/{quoted}", headers=platform_admin_headers)
    assert response.status_code == 200, response.text
    assert fake.called("GET", f"/nodes/pve1/tasks/{quoted}/status")
    assert response.json()["upid"] == upid
    # `pve` is a substring-free mismatch of `pve1`: the node field must match exactly.
    assert client.get(f"{BASE}/{cluster_id}/tasks/pve/{UPID1}", headers=platform_admin_headers).status_code == 400
