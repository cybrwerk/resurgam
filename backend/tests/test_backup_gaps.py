"""ADR 0032: 3-2-1 gap detection -- unprotected PVE guests, offsite copy status,
audited exceptions."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError

from resurgam_api.connectivity.models import (
    DatastoreListResult,
    GuestListResult,
    JobIncludedGuestsResult,
    RemoteDatastore,
    RemoteGuest,
    UnprotectedGuest,
    UnprotectedGuestListResult,
    VzdumpJob,
    VzdumpJobListResult,
)
from resurgam_api.inventory.offsite import group_filter_allows, map_namespace

NOW = datetime.now(UTC).replace(microsecond=0)


# --- helpers -------------------------------------------------------------------


def _create_pbs_instance(client, headers, name, host) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": name,
            "host": host,
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "test@pbs!token:secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _import_datastore(client, headers, instance_id, monkeypatch, name) -> str:
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name=name)]),
    )
    response = client.post(f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": name}, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def _create_pve_cluster(client, headers) -> str:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "PVE",
            "host": "pve1.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _group(db_session, datastore_id, backup_id, snapshot_ages_hours=(), namespace="", **overrides):
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    group = BackupGroup(
        datastore_id=uuid.UUID(str(datastore_id)),
        backup_type="vm",
        backup_id=backup_id,
        namespace=namespace,
        resolved_name=f"vm-{backup_id}",
        **overrides,
    )
    db_session.add(group)
    db_session.flush()
    for age in snapshot_ages_hours:
        db_session.add(BackupSnapshot(backup_group_id=group.id, snapshot_time=NOW - timedelta(hours=age)))
    db_session.commit()
    return group.id


def _sync_job(db_session, instance_id, job_id, local_store, remote_store, **fields):
    from resurgam_api.models.sync import SyncJob

    db_session.add(
        SyncJob(
            pbs_instance_id=uuid.UUID(str(instance_id)),
            job_id=job_id,
            local_store_name=local_store,
            remote_store=remote_store,
            **fields,
        )
    )
    db_session.commit()


def _create_customer(db_session, slug):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(name=slug.title(), slug=slug, debitor_number=f"d-{slug}", match_code=slug[:3].upper())
    db_session.add(customer)
    db_session.commit()
    return customer.id


def _assign(db_session, datastore_id, customer_id, role="primary"):
    from resurgam_api.models.datastore import DatastoreCustomerAssignment

    db_session.add(
        DatastoreCustomerAssignment(datastore_id=uuid.UUID(str(datastore_id)), customer_id=customer_id, role=role)
    )
    db_session.commit()


def _viewer_headers(db_session, test_settings, customer_id) -> dict:
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email=f"viewer-{customer_id}@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def _offsite_by_id(body) -> dict[str, dict]:
    return {entry["backup_id"]: entry for entry in body["offsite"]}


@pytest.fixture
def two_sites(client, platform_admin_headers, db_session, monkeypatch):
    """PBS A (primary) and PBS B (offsite), B pulls store-a from A."""
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    b = _create_pbs_instance(client, platform_admin_headers, "Site B", "pbs-b.example")
    store_a = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-a")
    store_b = _import_datastore(client, platform_admin_headers, b, monkeypatch, "store-b")
    _sync_job(
        db_session,
        b,
        "pull-a",
        "store-b",
        "store-a",
        remote_name="site-a",
        remote_host="PBS-A.example",
        remote_port=8007,
    )
    return {"a": a, "b": b, "store_a": store_a, "store_b": store_b}


# --- pure functions --------------------------------------------------------------


def test_group_filter_follows_pbs_rules():
    assert group_filter_allows(None, "vm", "100")
    assert group_filter_allows(["type:vm"], "vm", "100")
    assert not group_filter_allows(["type:ct"], "vm", "100")
    assert group_filter_allows(["group:vm/100", "group:vm/101"], "vm", "101")
    assert not group_filter_allows(["exclude:group:vm/100"], "vm", "100")
    assert group_filter_allows(["exclude:group:vm/100"], "vm", "101")
    # both: included minus excluded
    assert not group_filter_allows(["type:vm", "exclude:regex:^vm/1\\d\\d$"], "vm", "150")
    assert group_filter_allows(["include:type:vm", "exclude:regex:^vm/1\\d\\d$"], "vm", "2000")
    # a regex Python can't read counts as matching, so a gap is never hidden
    assert group_filter_allows(["regex:(?<x"], "vm", "1")


def test_map_namespace_follows_source_target_and_depth():
    assert map_namespace("", None, None, None) == ""
    assert map_namespace("cust/a", None, "offsite", None) == "offsite/cust/a"
    assert map_namespace("cust/a", "cust", "copy", None) == "copy/a"
    assert map_namespace("other", "cust", "copy", None) is None
    assert map_namespace("cust/a/b", "cust", "", 1) is None
    assert map_namespace("cust/a", "cust", "", 1) == "a"
    assert map_namespace("cust", "cust", "", 0) == ""


# --- offsite status ---------------------------------------------------------------


def test_offsite_statuses_for_pulled_copies(client, platform_admin_headers, db_session, two_sites):
    s = two_sites
    _group(db_session, s["store_a"], "100", (30, 6))
    _group(db_session, s["store_b"], "100", (30, 6))  # the copy, complete
    _group(db_session, s["store_a"], "101", (100, 50, 3))
    _group(db_session, s["store_b"], "101", (100,))  # behind: oldest uncopied is 50 h old
    _group(db_session, s["store_a"], "102", (80,))  # never arrived
    _group(db_session, s["store_a"], "103", (2,))  # new, sync not yet due

    response = client.get("/api/v1/backup-gaps", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    offsite = _offsite_by_id(body)
    # The copy on B is not checked as a source of its own.
    assert len(body["offsite"]) == 4

    assert offsite["100"]["status"] == "ok"
    assert offsite["100"]["lag_hours"] == 0
    assert offsite["100"]["copy_location"] == "Site B / store-b"
    assert offsite["100"]["sync_jobs"] == ["Site B: sync job pull-a"]

    assert offsite["101"]["status"] == "lagging"
    assert offsite["101"]["severity"] == "warning"
    assert offsite["101"]["uncopied_count"] == 2
    assert offsite["101"]["lag_hours"] == pytest.approx(50, abs=0.1)

    assert offsite["102"]["status"] == "missing"
    assert offsite["102"]["severity"] == "critical"

    assert offsite["103"]["status"] == "ok"
    assert offsite["103"]["latest_copy_at"] is None
    assert offsite["103"]["uncopied_count"] == 1

    summary = body["summary"]
    assert summary["offsite_gaps"] == 2
    assert summary["offsite_critical"] == 1
    assert summary["offsite_max_lag_hours"] == pytest.approx(80, abs=0.1)
    assert body["thresholds"] == {"offsite_lag_warning_hours": 36, "offsite_lag_critical_hours": 72}


def test_offsite_lag_over_critical_threshold(client, platform_admin_headers, db_session, two_sites):
    s = two_sites
    _group(db_session, s["store_a"], "100", (200, 90))
    _group(db_session, s["store_b"], "100", (200,))
    offsite = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())
    assert offsite["100"]["status"] == "lagging"
    assert offsite["100"]["severity"] == "critical"

    response = client.put(
        "/api/v1/backup-gaps/thresholds",
        json={"offsite_lag_warning_hours": 48, "offsite_lag_critical_hours": 96},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    offsite = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())
    assert offsite["100"]["severity"] == "warning"


def test_thresholds_reject_critical_below_warning(client, platform_admin_headers):
    response = client.put(
        "/api/v1/backup-gaps/thresholds",
        json={"offsite_lag_warning_hours": 48, "offsite_lag_critical_hours": 24},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_group_filter_and_namespace_mapping_decide_coverage(client, platform_admin_headers, db_session, two_sites):
    from resurgam_api.models.sync import SyncJob

    s = two_sites
    job = db_session.query(SyncJob).filter_by(job_id="pull-a").one()
    job.group_filter = ["exclude:group:vm/101"]
    job.remote_ns = "cust"
    job.local_ns = "offsite"
    db_session.commit()

    _group(db_session, s["store_a"], "100", (5,), namespace="cust/a")
    _group(db_session, s["store_b"], "100", (5,), namespace="offsite/a")
    _group(db_session, s["store_a"], "101", (5,), namespace="cust")
    _group(db_session, s["store_a"], "102", (5,), namespace="")  # outside remote-ns

    offsite = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())
    assert offsite["100"]["status"] == "ok"
    assert offsite["101"]["status"] == "no_copy_job"
    assert offsite["102"]["status"] == "no_copy_job"


def test_push_job_to_unconnected_pbs_and_local_sync(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store_push = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-push")
    store_main = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-main")
    store_second = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-second")
    _sync_job(
        db_session,
        a,
        "push-cloud",
        "store-push",
        "cloud-store",
        remote_name="cloud",
        remote_host="backup.cloud.example",
        sync_direction="push",
    )
    # A local pull (no remote) from store-main into store-second, same PBS.
    _sync_job(db_session, a, "local", "store-second", "store-main")
    _group(db_session, store_push, "300", (5,))
    _group(db_session, store_main, "400", (5,))
    _group(db_session, store_second, "400", (5,))

    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    offsite = _offsite_by_id(body)
    assert offsite["300"]["status"] == "target_not_connected"
    assert offsite["300"]["copy_location"] == "remote cloud / cloud-store"
    assert offsite["400"]["status"] == "local_copy_only"
    assert offsite["400"]["severity"] == "warning"
    assert len(body["offsite"]) == 2  # the local copy in store-second is not a source


def test_push_job_to_connected_pbs(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    b = _create_pbs_instance(client, platform_admin_headers, "Site B", "pbs-b.example")
    store_a = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-a")
    store_b = _import_datastore(client, platform_admin_headers, b, monkeypatch, "store-b")
    _sync_job(
        db_session,
        a,
        "push-b",
        "store-a",
        "store-b",
        remote_name="site-b",
        remote_host="pbs-b.example",
        sync_direction="push",
    )
    _group(db_session, store_a, "100", (5,))
    _group(db_session, store_b, "100", (5,))
    offsite = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())
    assert list(offsite) == ["100"]
    assert offsite["100"]["status"] == "ok"
    assert offsite["100"]["copy_location"] == "Site B / store-b"


def test_offsite_role_datastore_is_not_a_source(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store = _import_datastore(client, platform_admin_headers, a, monkeypatch, "dr")
    customer = _create_customer(db_session, "acme")
    _assign(db_session, store, customer, role="offsite")
    _group(db_session, store, "100", (5,))
    assert client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()["offsite"] == []


# --- exceptions --------------------------------------------------------------------


def test_exception_accepts_and_revoke_restores_the_gap(client, platform_admin_headers, db_session, monkeypatch):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-a")
    _group(db_session, store, "200", (5,))

    offsite = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())
    assert offsite["200"]["status"] == "no_copy_job"

    response = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={
            "target_kind": "backup_group",
            "datastore_id": store,
            "backup_type": "vm",
            "backup_id": "200",
            "reason": "Copied to tape weekly, change 1234.",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["target_label"] == "Site A / store-a / vm/200"
    assert created["created_by_name"]

    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    entry = _offsite_by_id(body)["200"]
    assert entry["status"] == "excepted"
    assert entry["raw_status"] == "no_copy_job"
    assert entry["severity"] is None
    assert entry["exception"]["reason"] == "Copied to tape weekly, change 1234."
    assert body["summary"]["offsite_gaps"] == 0
    assert body["summary"]["excepted"] == 1

    response = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={
            "target_kind": "backup_group",
            "datastore_id": store,
            "backup_type": "vm",
            "backup_id": "200",
            "active": False,
            "reason": "Tape rotation ended.",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 201
    assert (
        _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())["200"]["status"]
        == "no_copy_job"
    )

    history = client.get("/api/v1/backup-gaps/exceptions", headers=platform_admin_headers).json()
    assert [row["active"] for row in history] == [False, True]

    from resurgam_api.models.audit import AuditEvent

    actions = {e.action for e in db_session.query(AuditEvent).filter(AuditEvent.target_type == "backup_gap_exception")}
    assert actions == {"backup_gap_exception_created", "backup_gap_exception_revoked"}


def test_datastore_exception_and_expiry(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.backup_gaps import BackupGapException

    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-a")
    _group(db_session, store, "200", (5,))
    response = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={"target_kind": "datastore", "datastore_id": store, "reason": "Lab datastore, no offsite copy."},
        headers=platform_admin_headers,
    )
    assert response.status_code == 201
    assert (
        _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())["200"]["status"]
        == "excepted"
    )

    # An exception that has run out no longer counts (inserted directly: the API refuses past dates).
    user_id = db_session.query(BackupGapException).first().created_by_user_id
    db_session.add(
        BackupGapException(
            target_kind="datastore",
            datastore_id=uuid.UUID(store),
            active=True,
            reason="Temporary, until the new site is up.",
            expires_at=NOW - timedelta(hours=1),
            created_by_user_id=user_id,
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    assert (
        _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())["200"]["status"]
        == "no_copy_job"
    )


def test_exception_validation(client, platform_admin_headers):
    short = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={"target_kind": "datastore", "datastore_id": str(uuid.uuid4()), "reason": "short"},
        headers=platform_admin_headers,
    )
    assert short.status_code == 422
    missing_target = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={"target_kind": "pve_guest", "pve_cluster_id": str(uuid.uuid4()), "reason": "No VMID given here."},
        headers=platform_admin_headers,
    )
    assert missing_target.status_code == 422
    unknown = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={"target_kind": "datastore", "datastore_id": str(uuid.uuid4()), "reason": "Datastore does not exist."},
        headers=platform_admin_headers,
    )
    assert unknown.status_code == 404
    past = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={
            "target_kind": "pve_cluster",
            "pve_cluster_id": str(uuid.uuid4()),
            "reason": "Expiry in the past is refused.",
            "expires_at": (NOW - timedelta(days=1)).isoformat(),
        },
        headers=platform_admin_headers,
    )
    assert past.status_code == 400


def test_exceptions_are_append_only(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.backup_gaps import BackupGapException

    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-a")
    client.post(
        "/api/v1/backup-gaps/exceptions",
        json={"target_kind": "datastore", "datastore_id": store, "reason": "Lab datastore, no offsite copy."},
        headers=platform_admin_headers,
    )
    row = db_session.query(BackupGapException).one()
    row.reason = "rewritten"
    with pytest.raises(DBAPIError):
        db_session.commit()
    db_session.rollback()


# --- tenancy, leg applicability ----------------------------------------------------


def test_tenant_sees_own_offsite_status_only_and_no_guests(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store_1 = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-1")
    store_2 = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-2")
    acme = _create_customer(db_session, "acme")
    other = _create_customer(db_session, "other")
    _assign(db_session, store_1, acme)
    _assign(db_session, store_2, other)
    _group(db_session, store_1, "100", (5,))
    _group(db_session, store_2, "200", (5,))

    headers = _viewer_headers(db_session, test_settings, acme)
    body = client.get("/api/v1/backup-gaps", headers=headers).json()
    assert list(_offsite_by_id(body)) == ["100"]
    assert body["unprotected_guests"] == []
    assert body["pve_clusters"] == []
    assert client.get("/api/v1/backup-gaps/exceptions", headers=headers).status_code == 403
    response = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={"target_kind": "datastore", "datastore_id": store_1, "reason": "Tenant tries an exception."},
        headers=headers,
    )
    assert response.status_code == 403


def test_offsite_leg_not_applicable(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting

    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-1")
    acme = _create_customer(db_session, "acme")
    _assign(db_session, store, acme)
    _group(db_session, store, "100", (5,))
    db_session.add(ThreeTwoOneOneZeroLegSetting(customer_id=acme, leg="offsite", applicable=False))
    db_session.commit()
    entry = _offsite_by_id(client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json())["100"]
    assert entry["status"] == "not_required"
    assert entry["severity"] is None


# --- alarms --------------------------------------------------------------------------


def test_alarms_group_offsite_gaps_per_datastore(client, platform_admin_headers, db_session, two_sites):
    s = two_sites
    _group(db_session, s["store_a"], "102", (80,))
    _group(db_session, s["store_a"], "104", (90,))
    _group(db_session, s["store_a"], "101", (100, 50))
    _group(db_session, s["store_b"], "101", (100,))

    alarms = client.get("/api/v1/alarms", headers=platform_admin_headers).json()
    by_type = {a["type"]: a for a in alarms}
    missing = by_type["offsite_copy_missing"]
    assert missing["severity"] == "critical"
    assert missing["entity_type"] == "datastore"
    assert missing["message"] == "store-a: 2 workloads with no offsite copy arrived: vm-102, vm-104"
    assert by_type["offsite_copy_lagging_warning"]["message"].startswith("store-a: 1 workload with offsite copy")


# --- PVE: unprotected guests -----------------------------------------------------------


def _patch_pve(monkeypatch, *, not_backed_up, jobs, included, guests=()):
    from resurgam_api.collector import backup_gaps as module

    monkeypatch.setattr(
        module,
        "list_not_backed_up_guests",
        lambda *a, **kw: UnprotectedGuestListResult(reachable=True, guests=list(not_backed_up)),
    )
    monkeypatch.setattr(module, "list_vzdump_jobs", lambda *a, **kw: VzdumpJobListResult(reachable=True, jobs=jobs))
    monkeypatch.setattr(
        module,
        "list_job_included_vmids",
        lambda *a, job_id, **kw: JobIncludedGuestsResult(reachable=True, vmids=frozenset(included[job_id])),
    )
    monkeypatch.setattr(module, "list_guests", lambda *a, **kw: GuestListResult(reachable=True, guests=list(guests)))


def test_cluster_check_finds_unprotected_and_disabled_only_guests(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.pve import PveCluster

    cluster_id = _create_pve_cluster(client, platform_admin_headers)
    cluster = db_session.get(PveCluster, uuid.UUID(cluster_id))
    cluster.lab_appliance_template_vmid = 9000
    db_session.commit()
    _patch_pve(
        monkeypatch,
        not_backed_up=[
            UnprotectedGuest(vmid=105, guest_type="qemu", name="web"),
            UnprotectedGuest(vmid=9000, guest_type="qemu", name="resurgam-lab-template"),
        ],
        jobs=[VzdumpJob(id="daily", enabled=True), VzdumpJob(id="old", enabled=False)],
        included={"daily": {100, 101}, "old": {101, 102}},
        guests=[RemoteGuest(vmid=102, node="n1", guest_type="lxc", name="db")],
    )

    response = client.post(f"/api/v1/backup-gaps/pve-clusters/{cluster_id}/check", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["error"] is None

    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    guests = {g["vmid"]: g for g in body["unprotected_guests"]}
    assert set(guests) == {105, 102}  # 9000 is RESURGAM's own template
    assert guests[105]["reason"] == "no_job"
    assert guests[102]["reason"] == "only_disabled_jobs"
    assert guests[102]["guest_type"] == "lxc"
    assert body["summary"]["unprotected_guests"] == 2
    first_seen_102 = guests[102]["first_seen_at"]

    alarm = next(
        a
        for a in client.get("/api/v1/alarms", headers=platform_admin_headers).json()
        if a["type"] == "guest_not_backed_up"
    )
    assert alarm["message"] == "PVE: 2 guests in no enabled backup job: 102 (db), 105 (web)"

    # Excepting the whole cluster silences the alarm; the guests stay listed.
    response = client.post(
        "/api/v1/backup-gaps/exceptions",
        json={"target_kind": "pve_cluster", "pve_cluster_id": cluster_id, "reason": "Lab-only cluster."},
        headers=platform_admin_headers,
    )
    assert response.status_code == 201
    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    assert body["summary"]["unprotected_guests"] == 0
    assert body["summary"]["excepted"] == 2
    assert all(
        a["type"] != "guest_not_backed_up" for a in client.get("/api/v1/alarms", headers=platform_admin_headers).json()
    )

    # Next check: 105 got a job, 102 still only in the disabled one.
    _patch_pve(
        monkeypatch,
        not_backed_up=[],
        jobs=[VzdumpJob(id="daily", enabled=True), VzdumpJob(id="old", enabled=False)],
        included={"daily": {100, 101, 105}, "old": {101, 102}},
        guests=[RemoteGuest(vmid=102, node="n1", guest_type="lxc", name="db")],
    )
    client.post(f"/api/v1/backup-gaps/pve-clusters/{cluster_id}/check", headers=platform_admin_headers)
    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    assert [g["vmid"] for g in body["unprotected_guests"]] == [102]
    assert body["unprotected_guests"][0]["first_seen_at"] == first_seen_102


def test_cluster_check_keeps_previous_result_on_error(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.collector import backup_gaps as module

    cluster_id = _create_pve_cluster(client, platform_admin_headers)
    _patch_pve(
        monkeypatch,
        not_backed_up=[UnprotectedGuest(vmid=105, guest_type="qemu", name="web")],
        jobs=[],
        included={},
    )
    client.post(f"/api/v1/backup-gaps/pve-clusters/{cluster_id}/check", headers=platform_admin_headers)
    monkeypatch.setattr(
        module,
        "list_not_backed_up_guests",
        lambda *a, **kw: UnprotectedGuestListResult(reachable=False, error="connection refused"),
    )
    response = client.post(f"/api/v1/backup-gaps/pve-clusters/{cluster_id}/check", headers=platform_admin_headers)
    assert response.json()["error"] == "connection refused"
    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    assert [g["vmid"] for g in body["unprotected_guests"]] == [105]
    assert body["pve_clusters"][0]["error"] == "connection refused"


# --- PBS sync job listing --------------------------------------------------------------


def test_list_sync_jobs_asks_for_all_directions_and_falls_back(monkeypatch):
    from resurgam_api.connectivity import pbs_api
    from resurgam_api.connectivity.models import TrustConfig
    from resurgam_api.models.pbs import TlsTrustMode

    calls = []
    entry = {
        "id": "push-b",
        "store": "store-a",
        "remote-store": "store-b",
        "remote": "site-b",
        "sync-direction": "push",
        "remote-ns": "copy",
        "max-depth": 2,
        "group-filter": ["type:vm"],
        "transfer-last": 3,
    }

    def fake_get(host, port, trust, token, path, timeout, params=None):
        calls.append(params)
        if params:  # an older PBS rejects the unknown parameter
            return None, "HTTP 400: parameter verification failed"
        return [entry], None

    monkeypatch.setattr(pbs_api, "_get_data", fake_get)
    trust = TrustConfig(trust_mode=TlsTrustMode.CA)
    result = pbs_api.list_sync_jobs("pbs", 8007, trust, "t", all_directions=True)
    assert calls == [{"sync-direction": "all"}, None]
    job = result.sync_jobs[0]
    assert (job.sync_direction, job.remote_ns, job.max_depth, job.group_filter, job.transfer_last) == (
        "push",
        "copy",
        2,
        ("type:vm",),
        3,
    )
    calls.clear()
    pbs_api.list_sync_jobs("pbs", 8007, trust, "t")
    assert calls == [None]


def test_no_sync_job_alarms_only_when_policy_requires_dr(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.inventory import ProtectionPolicy

    a = _create_pbs_instance(client, platform_admin_headers, "Site A", "pbs-a.example")
    store = _import_datastore(client, platform_admin_headers, a, monkeypatch, "store-a")
    acme = _create_customer(db_session, "acme")
    _assign(db_session, store, acme)
    plain = ProtectionPolicy(
        customer_id=acme, name="Daily", expected_interval_hours=24, max_time_since_last_backup_hours=48
    )
    dr = ProtectionPolicy(
        customer_id=acme,
        name="Daily + DR",
        expected_interval_hours=24,
        max_time_since_last_backup_hours=48,
        sync_dr_required=True,
    )
    db_session.add_all([plain, dr])
    db_session.commit()
    _group(db_session, store, "100", (1,), protection_policy_id=plain.id, last_backup_at=NOW)
    _group(db_session, store, "101", (1,), protection_policy_id=dr.id, last_backup_at=NOW)
    _group(db_session, store, "102", (1,))

    body = client.get("/api/v1/backup-gaps", headers=platform_admin_headers).json()
    offsite = _offsite_by_id(body)
    assert {k: (v["status"], v["severity"]) for k, v in offsite.items()} == {
        "100": ("no_copy_job", None),
        "101": ("no_copy_job", "warning"),
        "102": ("no_copy_job", None),
    }
    assert body["summary"]["offsite_gaps"] == 1
    assert body["summary"]["offsite_without_copy_job"] == 3

    gap_alarms = [
        a
        for a in client.get("/api/v1/alarms", headers=platform_admin_headers).json()
        if a["type"].startswith("offsite")
    ]
    assert [(a["type"], a["customer_id"]) for a in gap_alarms] == [("offsite_copy_gap", str(acme))]
    assert gap_alarms[0]["message"].startswith("store-a: 1 workload with no checked offsite copy")
