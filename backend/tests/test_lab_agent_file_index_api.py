"""Endpoint tests for `resurgam-lab-agent`'s Guest-exec File Indexing
surface (ADR 0021, `api/v1/lab_agent.py`): the shared claim/entries/status
job trio both opt-in methods share, plus `guest_exec_live`'s
hookscript-specific `backup-completed` endpoint (the hookscript itself is
a node-level vzdump `script:` hook, not per-VM PVE config -- confirmed
live 2026-09-24, see ADR 0021's addendum -- so there is no longer a
per-agent reconcile-target endpoint to test here).
Mirrors `test_restore_test_plans_api.py`'s own lab-agent auth conventions
(`_agent_headers` via `POST /pve-clusters/{id}/lab-agent-token`).
"""

import uuid
from datetime import UTC, datetime

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


def _create_pbs_instance(client, headers) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "resurgam@pbs!dev:secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _import_datastore(client, headers, instance_id, monkeypatch, name="store1") -> str:
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name=name)]),
    )
    response = client.post(f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": name}, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _create_group(db_session, datastore_id, *, backup_type="vm", backup_id="100", file_index_enabled=True):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(
        datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id, file_index_enabled=file_index_enabled
    )
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _add_snapshot(db_session, group_id, snapshot_time=None):
    from resurgam_api.models.inventory import BackupSnapshot

    snapshot = BackupSnapshot(
        backup_group_id=group_id, snapshot_time=snapshot_time or datetime(2023, 9, 20, 0, 0, 3, tzinfo=UTC)
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.commit()
    return snapshot.id


def _enable_indexing(db_session, customer_id, **kwargs) -> None:
    from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings

    defaults = {"enabled": True, "retention_days": 30}
    defaults.update(kwargs)
    db_session.add(CustomerFileIndexSettings(customer_id=customer_id, **defaults))
    db_session.commit()


def _create_cluster(client, headers, *, name="Prod PVE", host="pve-prod.internal.example") -> str:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": name,
            "host": host,
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _agent_headers(client, headers, cluster_id):
    token = client.post(f"/api/v1/pve-clusters/{cluster_id}/lab-agent-token", headers=headers).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _set_guest_exec_live(client, headers, group_id, cluster_id):
    response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-method",
        headers=headers,
        json={"method": "guest_exec_live", "guest_exec_live_pve_cluster_id": cluster_id},
    )
    assert response.status_code == 200


def _setup_vm_group(client, platform_admin_headers, db_session, monkeypatch, *, backup_id="100"):
    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    group_id = _create_group(db_session, datastore_id, backup_id=backup_id)
    _enable_indexing(db_session, customer_id)
    return customer_id, datastore_id, group_id


# --- POST /backup-completed --------------------------------------------------


def test_backup_completed_no_ops_for_unknown_vmid(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, _datastore_id, _group_id = _setup_vm_group(client, platform_admin_headers, db_session, monkeypatch)
    cluster_id = _create_cluster(client, platform_admin_headers)
    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)

    response = client.post(
        "/api/v1/lab-agent/backup-completed",
        json={"vmid": 9999, "node": "pve1", "at": "2026-09-22T10:00:00Z"},
        headers=agent_headers,
    )
    assert response.status_code == 204


def test_backup_completed_no_ops_when_indexing_disabled(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import lab_agent as lab_agent_module

    customer_id, _datastore_id, group_id = _setup_vm_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_id="200"
    )
    cluster_id = _create_cluster(client, platform_admin_headers)
    _set_guest_exec_live(client, platform_admin_headers, group_id, cluster_id)

    from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings

    settings_row = db_session.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one()
    settings_row.enabled = False
    db_session.commit()

    monkeypatch.setattr(
        lab_agent_module,
        "sync_datastore_inventory",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not sync")),
    )
    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    response = client.post(
        "/api/v1/lab-agent/backup-completed",
        json={"vmid": 200, "node": "pve1", "at": "2026-09-22T10:00:00Z"},
        headers=agent_headers,
    )
    assert response.status_code == 204


def test_backup_completed_creates_pending_job(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import lab_agent as lab_agent_module
    from resurgam_api.models.file_index_job import FileIndexJob

    _customer_id, _datastore_id, group_id = _setup_vm_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_id="300"
    )
    cluster_id = _create_cluster(client, platform_admin_headers)
    _set_guest_exec_live(client, platform_admin_headers, group_id, cluster_id)
    snapshot_id = _add_snapshot(db_session, group_id)

    monkeypatch.setattr(lab_agent_module, "sync_datastore_inventory", lambda *a, **kw: None)
    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    response = client.post(
        "/api/v1/lab-agent/backup-completed",
        json={"vmid": 300, "node": "pve1", "at": "2026-09-22T10:00:00Z"},
        headers=agent_headers,
    )
    assert response.status_code == 204

    job = db_session.query(FileIndexJob).filter_by(backup_group_id=group_id).one()
    assert job.status == "pending"
    assert job.trigger == "guest_exec_hook"
    assert job.file_index_method == "guest_exec_live"
    assert job.backup_snapshot_id == snapshot_id
    assert job.guest_exec_target_vmid == 300
    assert job.guest_exec_target_node == "pve1"

    # A second real backup firing while the first job is still pending
    # must not create a duplicate.
    response2 = client.post(
        "/api/v1/lab-agent/backup-completed",
        json={"vmid": 300, "node": "pve1", "at": "2026-09-22T11:00:00Z"},
        headers=agent_headers,
    )
    assert response2.status_code == 204
    assert db_session.query(FileIndexJob).filter_by(backup_group_id=group_id).count() == 1


# --- shared job trio: GET /file-index-jobs, POST .../claim, .../entries, .../status ---


def test_guest_exec_live_job_full_lifecycle_first_run_is_full(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.file_index_job import FileIndexJob

    _customer_id, _datastore_id, group_id = _setup_vm_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_id="400"
    )
    cluster_id = _create_cluster(client, platform_admin_headers)
    _set_guest_exec_live(client, platform_admin_headers, group_id, cluster_id)
    snapshot_id = _add_snapshot(db_session, group_id)

    job = FileIndexJob(
        status="pending",
        trigger="guest_exec_hook",
        file_index_method="guest_exec_live",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        customer_id=_customer_id,
        guest_exec_target_vmid=400,
        guest_exec_target_node="pve1",
    )
    db_session.add(job)
    db_session.commit()

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    listed = client.get("/api/v1/lab-agent/file-index-jobs", headers=agent_headers).json()
    assert [j["id"] for j in listed] == [str(job.id)]
    assert listed[0]["file_index_method"] == "guest_exec_live"
    assert listed[0]["target_vmid"] == 400

    claimed = client.post(f"/api/v1/lab-agent/file-index-jobs/{job.id}/claim", headers=agent_headers)
    assert claimed.status_code == 200
    assert claimed.json() == {"since": None, "is_full_reindex": True}

    # Already claimed -- a second claim attempt must conflict.
    assert client.post(f"/api/v1/lab-agent/file-index-jobs/{job.id}/claim", headers=agent_headers).status_code == 409

    entries_response = client.post(
        f"/api/v1/lab-agent/file-index-jobs/{job.id}/entries",
        json={
            "entries": [
                {
                    "path": "/etc/hosts",
                    "filename": "hosts",
                    "type": "file",
                    "size_bytes": 128,
                    "mtime": "2026-09-22T09:00:00Z",
                },
                {"path": "/etc", "filename": "etc", "type": "directory"},
            ]
        },
        headers=agent_headers,
    )
    assert entries_response.status_code == 204

    status_response = client.post(
        f"/api/v1/lab-agent/file-index-jobs/{job.id}/status", json={"status": "completed"}, headers=agent_headers
    )
    assert status_response.status_code == 204

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.entries_indexed == 2
    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    assert {e.path for e in entries} == {"/etc/hosts", "/etc"}

    # No longer listed once claimed/completed.
    assert client.get("/api/v1/lab-agent/file-index-jobs", headers=agent_headers).json() == []


def test_guest_exec_live_job_listing_carries_group_exclude_patterns(
    client, platform_admin_headers, db_session, monkeypatch
):
    """A real gap found live (2026-09-24): the agent had no way to know
    a group's own exclude patterns at all, so guest-exec enumeration
    walked (and transferred back) everything unfiltered -- including
    paths already excluded by default for exactly this reason. Sent in
    the job listing now so the agent can prune during enumeration.
    """
    from resurgam_api.models.file_index_job import FileIndexJob

    _customer_id, _datastore_id, group_id = _setup_vm_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_id="401"
    )
    cluster_id = _create_cluster(client, platform_admin_headers)
    _set_guest_exec_live(client, platform_admin_headers, group_id, cluster_id)
    patterns_response = client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-exclude-patterns",
        json={"patterns": [r"(^|/)Windows/WinSxS(/|$)"]},
        headers=platform_admin_headers,
    )
    assert patterns_response.status_code == 200
    snapshot_id = _add_snapshot(db_session, group_id)

    job = FileIndexJob(
        status="pending",
        trigger="guest_exec_hook",
        file_index_method="guest_exec_live",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        customer_id=_customer_id,
        guest_exec_target_vmid=401,
        guest_exec_target_node="pve1",
    )
    db_session.add(job)
    db_session.commit()

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    listed = client.get("/api/v1/lab-agent/file-index-jobs", headers=agent_headers).json()
    assert listed[0]["exclude_patterns"] == [r"(^|/)Windows/WinSxS(/|$)"]


def test_submit_entries_filters_out_excluded_paths_server_side(client, platform_admin_headers, db_session, monkeypatch):
    """Defense-in-depth (2026-09-24): even if a guest-exec run submits an
    excluded path (guest-side pruning gap, or a pattern that changed
    mid-run), it must not land in the index -- matching
    `worker.file_index_worker`'s own belt-and-suspenders filtering for
    `external_mount`.
    """
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.file_index_job import FileIndexJob

    _customer_id, _datastore_id, group_id = _setup_vm_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_id="402"
    )
    cluster_id = _create_cluster(client, platform_admin_headers)
    _set_guest_exec_live(client, platform_admin_headers, group_id, cluster_id)
    client.post(
        f"/api/v1/backup-groups/{group_id}/file-index-exclude-patterns",
        json={"patterns": [r"(^|/)Windows/WinSxS(/|$)"]},
        headers=platform_admin_headers,
    )
    snapshot_id = _add_snapshot(db_session, group_id)

    job = FileIndexJob(
        status="pending",
        trigger="guest_exec_hook",
        file_index_method="guest_exec_live",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        customer_id=_customer_id,
        guest_exec_target_vmid=402,
        guest_exec_target_node="pve1",
    )
    db_session.add(job)
    db_session.commit()

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    client.post(f"/api/v1/lab-agent/file-index-jobs/{job.id}/claim", headers=agent_headers)

    entries_response = client.post(
        f"/api/v1/lab-agent/file-index-jobs/{job.id}/entries",
        json={
            "entries": [
                {
                    "path": "C:/Windows/WinSxS/some-file.dll",
                    "filename": "some-file.dll",
                    "type": "file",
                    "size_bytes": 1,
                },
                {"path": "C:/Windows/System32/notepad.exe", "filename": "notepad.exe", "type": "file", "size_bytes": 2},
            ]
        },
        headers=agent_headers,
    )
    assert entries_response.status_code == 204

    db_session.refresh(job)
    assert job.entries_indexed == 1
    entries = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job.id).all()
    assert {e.path for e in entries} == {"C:/Windows/System32/notepad.exe"}


def test_guest_exec_live_second_incremental_run_carries_forward_untouched_paths(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.file_index_entry import FileIndexEntry
    from resurgam_api.models.file_index_job import FileIndexJob

    _customer_id, _datastore_id, group_id = _setup_vm_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_id="500"
    )
    cluster_id = _create_cluster(client, platform_admin_headers)
    _set_guest_exec_live(client, platform_admin_headers, group_id, cluster_id)
    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)

    # First (full) run: two files.
    snapshot_1 = _add_snapshot(db_session, group_id, snapshot_time=datetime(2026, 9, 20, 0, 0, tzinfo=UTC))
    job_1 = FileIndexJob(
        status="pending",
        trigger="guest_exec_hook",
        file_index_method="guest_exec_live",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_1,
        customer_id=_customer_id,
        guest_exec_target_vmid=500,
        guest_exec_target_node="pve1",
    )
    db_session.add(job_1)
    db_session.commit()
    client.post(f"/api/v1/lab-agent/file-index-jobs/{job_1.id}/claim", headers=agent_headers)
    client.post(
        f"/api/v1/lab-agent/file-index-jobs/{job_1.id}/entries",
        json={
            "entries": [
                {
                    "path": "/data/a.txt",
                    "filename": "a.txt",
                    "type": "file",
                    "size_bytes": 10,
                    "mtime": "2026-09-19T00:00:00Z",
                },
                {
                    "path": "/data/b.txt",
                    "filename": "b.txt",
                    "type": "file",
                    "size_bytes": 20,
                    "mtime": "2026-09-19T00:00:00Z",
                },
            ]
        },
        headers=agent_headers,
    )
    client.post(
        f"/api/v1/lab-agent/file-index-jobs/{job_1.id}/status", json={"status": "completed"}, headers=agent_headers
    )

    # Second (incremental) run: only a.txt changed.
    snapshot_2 = _add_snapshot(db_session, group_id, snapshot_time=datetime(2026, 9, 21, 0, 0, tzinfo=UTC))
    job_2 = FileIndexJob(
        status="pending",
        trigger="guest_exec_hook",
        file_index_method="guest_exec_live",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_2,
        customer_id=_customer_id,
        guest_exec_target_vmid=500,
        guest_exec_target_node="pve1",
    )
    db_session.add(job_2)
    db_session.commit()

    claim_2 = client.post(f"/api/v1/lab-agent/file-index-jobs/{job_2.id}/claim", headers=agent_headers).json()
    assert claim_2["is_full_reindex"] is False
    assert claim_2["since"] is not None

    client.post(
        f"/api/v1/lab-agent/file-index-jobs/{job_2.id}/entries",
        json={
            "entries": [
                {
                    "path": "/data/a.txt",
                    "filename": "a.txt",
                    "type": "file",
                    "size_bytes": 15,
                    "mtime": "2026-09-21T00:00:00Z",
                },
            ]
        },
        headers=agent_headers,
    )
    client.post(
        f"/api/v1/lab-agent/file-index-jobs/{job_2.id}/status", json={"status": "completed"}, headers=agent_headers
    )

    db_session.refresh(job_2)
    entries_2 = db_session.query(FileIndexEntry).filter_by(file_index_job_id=job_2.id).all()
    by_path = {e.path: e for e in entries_2}
    assert set(by_path) == {"/data/a.txt", "/data/b.txt"}
    # a.txt was freshly transferred this run.
    assert by_path["/data/a.txt"].size_bytes == 15
    assert by_path["/data/a.txt"].linked_from_entry_id is None
    # b.txt was carried forward -- linked, not re-verified.
    assert by_path["/data/b.txt"].size_bytes is None
    assert by_path["/data/b.txt"].linked_from_entry_id is not None


# --- guest_exec_ephemeral listing --------------------------------------------


def test_guest_exec_ephemeral_job_listing_carries_boot_coordinates(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
    from resurgam_api.models.file_index_job import FileIndexJob

    customer_id, _datastore_id, group_id = _setup_vm_group(
        client, platform_admin_headers, db_session, monkeypatch, backup_id="600"
    )
    snapshot_id = _add_snapshot(db_session, group_id)
    lab_cluster_id = _create_cluster(client, platform_admin_headers, name="Lab PVE", host="pve-lab.internal.example")

    settings_row = db_session.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one()
    settings_row.guest_exec_pve_cluster_id = uuid.UUID(lab_cluster_id)
    settings_row.guest_exec_target_node = "labnode"
    settings_row.guest_exec_target_storage = "local-lvm"
    db_session.commit()

    job = FileIndexJob(
        status="pending",
        trigger="manual",
        file_index_method="guest_exec_ephemeral",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        customer_id=customer_id,
        guest_exec_target_vmid=777,
        guest_exec_target_node="labnode",
    )
    db_session.add(job)
    db_session.commit()

    agent_headers = _agent_headers(client, platform_admin_headers, lab_cluster_id)
    listed = client.get("/api/v1/lab-agent/file-index-jobs", headers=agent_headers).json()
    assert len(listed) == 1
    entry = listed[0]
    assert entry["file_index_method"] == "guest_exec_ephemeral"
    assert entry["target_vmid"] == 777
    assert entry["target_storage"] == "local-lvm"
    assert entry["pbs_datastore"] == "store1"
    assert entry["backup_id"] == "600"
    assert entry["pool"]

    # A different cluster's agent never sees this ephemeral job.
    other_cluster_id = _create_cluster(
        client, platform_admin_headers, name="Unrelated PVE", host="pve-unrelated.internal.example"
    )
    other_agent_headers = _agent_headers(client, platform_admin_headers, other_cluster_id)
    assert client.get("/api/v1/lab-agent/file-index-jobs", headers=other_agent_headers).json() == []
