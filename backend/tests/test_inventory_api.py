from datetime import UTC

from resurgam_api.connectivity.models import (
    RemoteSnapshot,
    RemoteTask,
    SnapshotListResult,
    TaskListResult,
)


def _create_pbs_instance(
    client, headers, *, name="PBS", host="pbs1.internal.example", token="test@pbs!token:secretvalue"
) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": name,
            "host": host,
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": token,
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _import_datastore(client, headers, instance_id, monkeypatch, name="store1") -> str:
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore

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


def _create_customer_scoped_viewer(db_session, test_settings, customer_id) -> dict:
    """See tests/test_datastores_api.py for why this doesn't reuse the
    break-glass login: it always resolves to the same fixed identity.
    """
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email=f"viewer-{customer_id}@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()

    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def test_sync_endpoint_creates_inventory_and_tasks(client, platform_admin_headers, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)

    from resurgam_api.collector import sync as collector_sync_module

    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(
            reachable=True,
            snapshots=[RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")],
        ),
    )
    monkeypatch.setattr(
        collector_sync_module,
        "list_tasks",
        lambda *a, **kw: TaskListResult(
            reachable=True,
            tasks=[
                RemoteTask(
                    upid="UPID:pbs:1:backup:100:root@pam:",
                    task_type="backup",
                    worker_id="100",
                    status="OK",
                    start_time=1_700_000_000,
                    end_time=1_700_000_100,
                )
            ],
        ),
    )

    response = client.post(f"/api/v1/pbs-instances/{instance_id}/sync", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["tasks"]["tasks_created"] == 1
    assert body["datastores"][0]["datastore_id"] == datastore_id
    assert body["datastores"][0]["groups_created"] == 1
    assert body["datastores"][0]["snapshots_created"] == 1

    groups_response = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=platform_admin_headers)
    assert groups_response.status_code == 200
    groups = groups_response.json()
    assert len(groups) == 1
    group_id = groups[0]["id"]
    assert groups[0]["backup_id"] == "100"

    snapshots_response = client.get(f"/api/v1/backup-groups/{group_id}/snapshots", headers=platform_admin_headers)
    assert snapshots_response.status_code == 200
    assert len(snapshots_response.json()) == 1

    tasks_response = client.get(f"/api/v1/pbs-instances/{instance_id}/tasks", headers=platform_admin_headers)
    assert tasks_response.status_code == 200
    assert len(tasks_response.json()) == 1


def test_sync_requires_platform_admin(client):
    response = client.post("/api/v1/pbs-instances/00000000-0000-0000-0000-000000000000/sync")
    assert response.status_code == 401


def test_backup_groups_respect_customer_scope(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    own_datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="mine")
    other_datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="other")

    # sync_pbs_instance syncs every imported datastore in one pass, so the
    # mock must return different data per datastore name rather than being
    # called twice with a single fixed return value.
    from resurgam_api.collector import sync as collector_sync_module

    snapshots_by_datastore = {
        "mine": [RemoteSnapshot(backup_type="vm", backup_id="100", backup_time=1_700_000_000, verify_state="ok")],
        "other": [RemoteSnapshot(backup_type="vm", backup_id="200", backup_time=1_700_000_000, verify_state="ok")],
    }
    monkeypatch.setattr(collector_sync_module, "list_tasks", lambda *a, **kw: TaskListResult(reachable=True, tasks=[]))
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda host, port, trust, token, datastore, timeout=5.0: SnapshotListResult(
            reachable=True, snapshots=snapshots_by_datastore[datastore]
        ),
    )
    sync_response = client.post(f"/api/v1/pbs-instances/{instance_id}/sync", headers=platform_admin_headers)
    assert sync_response.status_code == 200

    customer_mine = _create_customer(db_session, "Mine Inc", "mine")
    client.post(
        f"/api/v1/datastores/{own_datastore_id}/assign-customer",
        json={"customer_id": str(customer_mine)},
        headers=platform_admin_headers,
    )

    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_mine)

    own_groups = client.get(f"/api/v1/datastores/{own_datastore_id}/backup-groups", headers=viewer_headers)
    assert own_groups.status_code == 200
    assert len(own_groups.json()) == 1

    other_groups = client.get(f"/api/v1/datastores/{other_datastore_id}/backup-groups", headers=viewer_headers)
    assert other_groups.status_code == 404  # not 403 — out of scope, must not confirm existence

    own_group_id = own_groups.json()[0]["id"]
    snapshots = client.get(f"/api/v1/backup-groups/{own_group_id}/snapshots", headers=viewer_headers)
    assert snapshots.status_code == 200


def test_backup_groups_excludes_superseded_by_default_includes_with_flag(
    client, platform_admin_headers, db_session, monkeypatch
):
    from datetime import datetime

    from resurgam_api.models.inventory import BackupGroup

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)

    old_group = BackupGroup(
        datastore_id=datastore_id,
        backup_type="vm",
        backup_id="100",
        resolved_name="old-guest",
        superseded_at=datetime.now(UTC),
    )
    new_group = BackupGroup(datastore_id=datastore_id, backup_type="vm", backup_id="100", resolved_name="new-guest")
    db_session.add_all([old_group, new_group])
    db_session.commit()

    default_response = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=platform_admin_headers)
    assert default_response.status_code == 200
    assert [g["resolved_name"] for g in default_response.json()] == ["new-guest"]
    assert default_response.json()[0]["superseded_at"] is None

    history_response = client.get(
        f"/api/v1/datastores/{datastore_id}/backup-groups?include_superseded=true", headers=platform_admin_headers
    )
    assert history_response.status_code == 200
    names = {g["resolved_name"] for g in history_response.json()}
    assert names == {"old-guest", "new-guest"}
    old_entry = next(g for g in history_response.json() if g["resolved_name"] == "old-guest")
    assert old_entry["superseded_at"] is not None


def test_backup_groups_excludes_removed_by_default_includes_with_flag(
    client, platform_admin_headers, db_session, monkeypatch
):
    from datetime import datetime

    from resurgam_api.models.inventory import BackupGroup

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)

    removed_group = BackupGroup(
        datastore_id=datastore_id,
        backup_type="vm",
        backup_id="100",
        resolved_name="deleted-on-pbs",
        removed_from_pbs_at=datetime.now(UTC),
    )
    live_group = BackupGroup(datastore_id=datastore_id, backup_type="vm", backup_id="101", resolved_name="still-there")
    db_session.add_all([removed_group, live_group])
    db_session.commit()

    default_response = client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=platform_admin_headers)
    assert default_response.status_code == 200
    assert [g["resolved_name"] for g in default_response.json()] == ["still-there"]

    history_response = client.get(
        f"/api/v1/datastores/{datastore_id}/backup-groups?include_removed=true", headers=platform_admin_headers
    )
    assert history_response.status_code == 200
    names = {g["resolved_name"] for g in history_response.json()}
    assert names == {"deleted-on-pbs", "still-there"}
    removed_entry = next(g for g in history_response.json() if g["resolved_name"] == "deleted-on-pbs")
    assert removed_entry["removed_from_pbs_at"] is not None


def test_tasks_endpoint_requires_platform_admin(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)

    response = client.get("/api/v1/pbs-instances/00000000-0000-0000-0000-000000000000/tasks", headers=viewer_headers)
    assert response.status_code == 403


def test_sync_jobs_endpoint_lists_configured_jobs(client, platform_admin_headers, db_session):
    from resurgam_api.models.sync import SyncJob

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    job = SyncJob(
        pbs_instance_id=instance_id,
        job_id="job-1",
        remote_name="dr-site",
        remote_store="main",
        local_store_name="local-store",
        schedule="daily",
    )
    db_session.add(job)
    db_session.commit()

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/sync-jobs", headers=platform_admin_headers)
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "job-1"
    assert jobs[0]["remote_name"] == "dr-site"


def test_sync_jobs_endpoint_requires_platform_admin(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)

    response = client.get(
        "/api/v1/pbs-instances/00000000-0000-0000-0000-000000000000/sync-jobs", headers=viewer_headers
    )
    assert response.status_code == 403


def _insert_task(db_session, instance_id, upid, task_type, started_at, status="ok"):
    from resurgam_api.models.inventory import TaskRun

    task = TaskRun(
        pbs_instance_id=instance_id,
        upid=upid,
        task_type=task_type,
        worker_id="vm/100",
        status=status,
        started_at=started_at.replace(tzinfo=UTC),
    )
    db_session.add(task)
    return task


def test_tasks_endpoint_limit_returns_most_recent_first(client, platform_admin_headers, db_session):
    from datetime import datetime

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    for i in range(5):
        _insert_task(
            db_session, instance_id, f"UPID:pbs:{i}:backup:root@pam:", "backup", datetime(2026, 1, i + 1, tzinfo=UTC)
        )
    db_session.commit()

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/tasks?limit=2", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    # most recent (2026-01-05) first
    assert body[0]["upid"] == "UPID:pbs:4:backup:root@pam:"
    assert body[1]["upid"] == "UPID:pbs:3:backup:root@pam:"


def test_tasks_endpoint_task_types_filters_before_limit(client, platform_admin_headers, db_session):
    """A less-frequent task type (verify) sharing the same global recency
    window as a more-frequent one (backup) must not be starved out by a
    limit applied before the type filter — see the endpoint's docstring.
    """
    from datetime import datetime

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    # three older backups, then three newer verify tasks
    for i in range(3):
        _insert_task(
            db_session, instance_id, f"UPID:pbs:b{i}:backup:root@pam:", "backup", datetime(2026, 1, i + 1, tzinfo=UTC)
        )
    for i in range(3):
        _insert_task(
            db_session, instance_id, f"UPID:pbs:v{i}:verify:root@pam:", "verify", datetime(2026, 2, i + 1, tzinfo=UTC)
        )
    db_session.commit()

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/tasks?task_types=backup&limit=2", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert all(t["task_type"] == "backup" for t in body)
    # the two most recent *backup* tasks, unaffected by the newer verify tasks
    assert body[0]["upid"] == "UPID:pbs:b2:backup:root@pam:"
    assert body[1]["upid"] == "UPID:pbs:b1:backup:root@pam:"


def test_tasks_endpoint_status_filter_composes_with_task_types(client, platform_admin_headers, db_session):
    """Recovery Evidence compliance reporting: `status` was added
    alongside the existing `task_types` filter, applied *before* `limit`
    the same way -- this locks in that it composes correctly with
    `task_types`, not just standing alone.
    """
    from datetime import datetime

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    _insert_task(
        db_session, instance_id, "UPID:pbs:b0:backup:root@pam:", "backup", datetime(2026, 1, 1, tzinfo=UTC), status="ok"
    )
    _insert_task(
        db_session,
        instance_id,
        "UPID:pbs:b1:backup:root@pam:",
        "backup",
        datetime(2026, 1, 2, tzinfo=UTC),
        status="error",
    )
    _insert_task(
        db_session,
        instance_id,
        "UPID:pbs:v0:verify:root@pam:",
        "verify",
        datetime(2026, 1, 3, tzinfo=UTC),
        status="error",
    )
    db_session.commit()

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/tasks?status=error", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert {t["upid"] for t in body} == {"UPID:pbs:b1:backup:root@pam:", "UPID:pbs:v0:verify:root@pam:"}

    response = client.get(
        f"/api/v1/pbs-instances/{instance_id}/tasks?status=error&task_types=backup", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert [t["upid"] for t in body] == ["UPID:pbs:b1:backup:root@pam:"]

    # Cross-instance endpoint gets the same filter.
    response = client.get(f"/api/v1/tasks?pbs_instance_ids={instance_id}&status=error", headers=platform_admin_headers)
    assert response.status_code == 200
    assert {t["upid"] for t in response.json()} == {"UPID:pbs:b1:backup:root@pam:", "UPID:pbs:v0:verify:root@pam:"}


def test_tasks_endpoint_rejects_out_of_range_limit(client, platform_admin_headers):
    instance_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/tasks?limit=0", headers=platform_admin_headers)
    assert response.status_code == 422

    response = client.get(f"/api/v1/pbs-instances/{instance_id}/tasks?limit=5001", headers=platform_admin_headers)
    assert response.status_code == 422


# --- cross-instance GET /tasks (Jobs page multi-instance selection) --------


def test_cross_instance_tasks_endpoint_unions_instances(client, platform_admin_headers, db_session):
    from datetime import datetime

    instance_a = _create_pbs_instance(client, platform_admin_headers)
    instance_b = _create_pbs_instance(client, platform_admin_headers, name="pbs-b", token="root@pam!b:secretvalue")
    _insert_task(db_session, instance_a, "UPID:a:1:backup:root@pam:", "backup", datetime(2026, 1, 1, tzinfo=UTC))
    _insert_task(db_session, instance_b, "UPID:b:1:backup:root@pam:", "backup", datetime(2026, 1, 2, tzinfo=UTC))
    db_session.commit()

    response = client.get(
        f"/api/v1/tasks?pbs_instance_ids={instance_a}&pbs_instance_ids={instance_b}",
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    upids = {t["upid"] for t in response.json()}
    assert upids == {"UPID:a:1:backup:root@pam:", "UPID:b:1:backup:root@pam:"}


def test_cross_instance_tasks_endpoint_only_returns_selected_instances(client, platform_admin_headers, db_session):
    from datetime import datetime

    instance_a = _create_pbs_instance(client, platform_admin_headers)
    instance_b = _create_pbs_instance(client, platform_admin_headers, name="pbs-b", token="root@pam!b:secretvalue")
    _insert_task(db_session, instance_a, "UPID:a:1:backup:root@pam:", "backup", datetime(2026, 1, 1, tzinfo=UTC))
    _insert_task(db_session, instance_b, "UPID:b:1:backup:root@pam:", "backup", datetime(2026, 1, 2, tzinfo=UTC))
    db_session.commit()

    response = client.get(f"/api/v1/tasks?pbs_instance_ids={instance_a}", headers=platform_admin_headers)
    assert response.status_code == 200
    upids = {t["upid"] for t in response.json()}
    assert upids == {"UPID:a:1:backup:root@pam:"}


def test_cross_instance_tasks_endpoint_requires_platform_admin(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    viewer_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_id)

    response = client.get("/api/v1/tasks?pbs_instance_ids=00000000-0000-0000-0000-000000000000", headers=viewer_headers)
    assert response.status_code == 403


def test_cross_instance_tasks_endpoint_rejects_empty_instance_ids(client, platform_admin_headers):
    response = client.get("/api/v1/tasks", headers=platform_admin_headers)
    assert response.status_code == 422


def test_cross_instance_tasks_endpoint_started_after_filters_before_limit(client, platform_admin_headers, db_session):
    from datetime import datetime

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    for i in range(3):
        _insert_task(
            db_session, instance_id, f"UPID:pbs:old{i}:backup:root@pam:", "backup", datetime(2026, 1, i + 1, tzinfo=UTC)
        )
    for i in range(3):
        _insert_task(
            db_session, instance_id, f"UPID:pbs:new{i}:backup:root@pam:", "backup", datetime(2026, 3, i + 1, tzinfo=UTC)
        )
    db_session.commit()

    response = client.get(
        f"/api/v1/tasks?pbs_instance_ids={instance_id}&started_after=2026-02-01T00:00:00Z&limit=2",
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert all(t["upid"].startswith("UPID:pbs:new") for t in body)


def test_cross_instance_tasks_endpoint_started_before_is_inclusive(client, platform_admin_headers, db_session):
    from datetime import datetime

    instance_id = _create_pbs_instance(client, platform_admin_headers)
    _insert_task(
        db_session,
        instance_id,
        "UPID:pbs:boundary:backup:root@pam:",
        "backup",
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/tasks?pbs_instance_ids={instance_id}&started_before=2026-01-01T12:00:00Z",
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
