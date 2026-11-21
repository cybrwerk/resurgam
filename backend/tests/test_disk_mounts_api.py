"""Disk Mounts (ADR 0025): user API (permissions, four-eyes approval, extend,
stop), the agent backchannel (claim/progress/status/teardown), the expiry and
orphan sweep, and that the generated share password never appears anywhere
but the create response and the agent's claim. PBS/PVE are mocked; nothing
real is contacted."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from resurgam_api.api.v1 import disk_mounts as disk_mounts_module
from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore
from resurgam_api.disk_mounts import lifecycle
from resurgam_api.disk_mounts.snapshot_disks import SnapshotDisk
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.disk_mount import DiskMount
from resurgam_api.models.pve import LabUplinkIpAllocation, PveCluster

BASE = "/api/v1/disk-mounts"
AGENT = "/api/v1/lab-agent/disk-mount-jobs"


# --- setup -----------------------------------------------------------------------------------


def _customer(db_session, slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=slug.title(), slug=slug, debitor_number=f"d-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.commit()
    return customer.id


def _scoped_user(db_session, test_settings, customer_id, role_key="restore_operator"):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email=f"{role_key}-{uuid.uuid4().hex[:8]}@example.com", display_name="Operator")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey(role_key).value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}, user.id


def _pbs_and_datastore(client, headers, monkeypatch, customer_id):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    instance = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "resurgam@pbs!dev:secretvalue",
        },
        headers=headers,
    ).json()["id"]
    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name="store1")]),
    )
    datastore = client.post(
        f"/api/v1/pbs-instances/{instance}/datastores", json={"name": "store1"}, headers=headers
    ).json()["id"]
    client.post(
        f"/api/v1/datastores/{datastore}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=headers,
    )
    return datastore


def _group_and_snapshot(db_session, datastore_id, backup_type="vm", backup_id="116"):
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    group = BackupGroup(datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id, namespace="Kunde")
    db_session.add(group)
    db_session.flush()
    snapshot = BackupSnapshot(backup_group_id=group.id, snapshot_time=datetime(2026, 9, 14, 0, 48, 57, tzinfo=UTC))
    db_session.add(snapshot)
    db_session.commit()
    return group.id, snapshot.id


def _lab_cluster(client, headers, db_session, *, host="pve-lab.internal.example", **overrides):
    cluster_id = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": f"Lab {host[:6]}",
            "host": host,
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "is_lab_compute": True,
            "lab_snippet_storage_id": "local",
            "lab_appliance_uplink_bridge": "vmbr0",
        },
        headers=headers,
    ).json()["id"]
    token = client.post(f"/api/v1/pve-clusters/{cluster_id}/lab-agent-token", headers=headers).json()["token"]
    cluster = db_session.get(PveCluster, cluster_id)
    cluster.lab_agent_last_seen_at = datetime.now(UTC)
    cluster.disk_mount_helper_template_vmid = 9100
    for key, value in overrides.items():
        setattr(cluster, key, value)
    db_session.commit()
    return cluster_id, {"Authorization": f"Bearer {token}"}


@pytest.fixture
def disks(monkeypatch):
    available = [
        SnapshotDisk("drive-scsi0.img.fidx", "scsi0", 32 * 2**30, "none"),
        SnapshotDisk("drive-scsi1.img.fidx", "scsi1", 100 * 2**30, "none"),
    ]
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        return available

    monkeypatch.setattr(disk_mounts_module, "list_snapshot_disks", fake)
    return calls


@pytest.fixture
def env(client, platform_admin_headers, db_session, test_settings, monkeypatch, disks):
    customer_id = _customer(db_session)
    datastore_id = _pbs_and_datastore(client, platform_admin_headers, monkeypatch, customer_id)
    group_id, snapshot_id = _group_and_snapshot(db_session, datastore_id)
    cluster_id, agent = _lab_cluster(client, platform_admin_headers, db_session)
    operator, operator_id = _scoped_user(db_session, test_settings, customer_id)
    return {
        "customer_id": customer_id,
        "datastore_id": datastore_id,
        "group_id": group_id,
        "snapshot_id": snapshot_id,
        "cluster_id": cluster_id,
        "agent": agent,
        "operator": operator,
        "operator_id": operator_id,
    }


def _body(env, **overrides):
    body = {
        "backup_snapshot_id": str(env["snapshot_id"]),
        "disk_archives": ["drive-scsi0.img.fidx", "drive-scsi1.img.fidx"],
        "pve_cluster_id": env["cluster_id"],
        "target_node": "pve1",
        "bridge": "vmbr0",
        "vlan_tag": 20,
        "ip_mode": "static",
        "ip_address_cidr": "192.168.20.50/24",
        "gateway": "192.168.20.1",
        "protocols": ["smb", "nfs"],
        "allowed_clients": ["192.168.20.0/24", "10.1.2.3"],
    }
    body.update(overrides)
    return body


def _create(client, env, headers=None, **overrides):
    return client.post(BASE, json=_body(env, **overrides), headers=headers or env["operator"])


def _audit_text(db_session) -> str:
    return json.dumps([[e.action, e.detail] for e in db_session.query(AuditEvent).all()], default=str)


def _snippet_volume(mount_id) -> str:
    return f"local:snippets/resurgam-disk-mount-{uuid.UUID(str(mount_id)).hex[:12]}.yaml"


def _provision(client, env, mount_id, *, ready=True):
    claim = client.post(f"{AGENT}/{mount_id}/claim", headers=env["agent"])
    assert claim.status_code == 200, claim.text
    for body in (
        {"mapped_device": {"archive": "drive-scsi0.img.fidx", "device": "/dev/loop3"}},
        {"mapped_device": {"archive": "drive-scsi1.img.fidx", "device": "/dev/loop4"}},
        {"helper_vmid": 4242},
        {"snippet_volume": _snippet_volume(mount_id)},
    ):
        assert client.post(f"{AGENT}/{mount_id}/progress", json=body, headers=env["agent"]).status_code == 204
    if ready:
        status = client.post(
            f"{AGENT}/{mount_id}/status",
            json={
                "status": "ready",
                "addresses": ["192.168.20.50"],
                "volumes": [
                    {
                        "disk_index": 1,
                        "archive": "drive-scsi0.img.fidx",
                        "partition": "part2",
                        "fstype": "ntfs",
                        "size_bytes": 1000,
                        "name": "disk1-part2-ntfs",
                        "mounted": True,
                        "reason": None,
                        "detail": None,
                        "unexpected": "dropped",
                    },
                    {
                        "disk_index": 2,
                        "archive": "drive-scsi1.img.fidx",
                        "partition": "part1",
                        "fstype": "BitLocker",
                        "mounted": False,
                        "reason": "encrypted",
                    },
                ],
            },
            headers=env["agent"],
        )
        assert status.status_code == 204, status.text
    return claim.json()


# --- create / permissions ---------------------------------------------------------------------


def test_create_returns_the_password_once_and_stores_only_ciphertext(client, env, db_session, caplog, disks):
    caplog.set_level(logging.DEBUG)
    response = _create(client, env)
    assert response.status_code == 201, response.text
    created = response.json()
    password = created["share_password"]
    mount = created["mount"]
    assert len(password) == 24
    assert mount["status"] == "queued"
    assert mount["smb_username"].startswith("rsgm-")
    assert mount["protocols"] == ["smb", "nfs"]
    assert mount["allowed_clients"] == ["192.168.20.0/24", "10.1.2.3/32"]
    assert mount["connection"]["host"] is None  # not ready yet
    assert disks[0]["namespace"] == "Kunde" and disks[0]["backup_type"] == "vm"

    row = db_session.get(DiskMount, uuid.UUID(mount["id"]))
    assert row.share_password_ciphertext is not None
    assert password.encode() not in row.share_password_ciphertext
    assert (
        lifecycle.decrypt_password("PIjcEQaYJrbfZSCc5nPrYO-qID45lH3qxeKsKt_ln44=", row.share_password_ciphertext)
        == password
    )

    detail = client.get(f"{BASE}/{mount['id']}", headers=env["operator"]).text
    listing = client.get(BASE, headers=env["operator"]).text
    audit = _audit_text(db_session)
    assert "disk_mount_created" in audit
    for text in (detail, listing, audit, caplog.text):
        assert password not in text


def test_nfs_only_mount_has_no_credentials(client, env):
    created = _create(client, env, protocols=["nfs"]).json()
    assert created["share_password"] is None
    assert created["mount"]["smb_username"] is None


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"protocols": ["nfs"], "allowed_clients": []}, "NFS has no password"),
        ({"allowed_clients": ["10.0.0.0/4"]}, "too broad"),
        ({"allowed_clients": ["not-an-ip"]}, "not an IPv4"),
        ({"ip_address_cidr": "192.168.20.0/24"}, "network or broadcast"),
        ({"gateway": "10.9.9.9"}, "inside"),
        ({"ip_mode": "dhcp"}, "only used with ip_mode static"),
        ({"disk_archives": ["root.pxar.didx"]}, "not a VM disk archive"),
        ({"lifetime_hours": 73}, "less than or equal to 72"),
        ({"ip_mode": "cluster_range", "ip_address_cidr": None, "gateway": None}, "leave vlan_tag"),
    ],
)
def test_create_validation_says_what_to_fix(client, env, overrides, fragment):
    response = _create(client, env, **overrides)
    assert response.status_code == 422, response.text
    assert fragment in response.text


def test_container_backups_are_refused_with_a_pointer_to_file_restore(client, env, db_session):
    _, ct_snapshot = _group_and_snapshot(db_session, env["datastore_id"], backup_type="ct", backup_id="200")
    response = _create(client, env, backup_snapshot_id=str(ct_snapshot))
    assert response.status_code == 400
    assert response.json()["code"] == "disk_mount_unsupported_backup_type"
    assert "Files" in response.json()["message"]


def test_unknown_disk_is_refused(client, env):
    response = _create(client, env, disk_archives=["drive-sata3.img.fidx"])
    assert response.status_code == 400
    assert response.json()["code"] == "disk_mount_unknown_disk"


def test_cluster_without_helper_template_is_not_ready(client, env, db_session):
    cluster = db_session.get(PveCluster, env["cluster_id"])
    cluster.disk_mount_helper_template_vmid = None
    db_session.commit()
    response = _create(client, env)
    assert response.status_code == 400
    assert response.json()["details"]["problems"] == ["helper_template_missing"]
    placement = client.get(f"{BASE}/placement", headers=env["operator"]).json()
    assert placement[0]["ready"] is False and placement[0]["problems"] == ["helper_template_missing"]


def test_other_tenants_and_viewers_cannot_see_or_create(client, env, db_session, test_settings):
    mount_id = _create(client, env).json()["mount"]["id"]
    other_customer = _customer(db_session, slug="other")
    other_operator, _ = _scoped_user(db_session, test_settings, other_customer)
    viewer, _ = _scoped_user(db_session, test_settings, env["customer_id"], role_key="viewer")
    for headers in (other_operator, viewer):
        assert client.get(f"{BASE}/{mount_id}", headers=headers).status_code == 404
        assert client.get(BASE, headers=headers).json() == []
        assert client.post(f"{BASE}/{mount_id}/stop", headers=headers).status_code == 404
        assert _create(client, env, headers=headers).status_code == 404


def test_static_address_already_in_use_is_refused(client, env):
    assert _create(client, env).status_code == 201
    response = _create(client, env, ip_address_cidr="192.168.20.50/24")
    assert response.status_code == 409
    assert response.json()["code"] == "disk_mount_address_in_use"


# --- approval ---------------------------------------------------------------------------------


def test_four_eyes_approval(client, env, db_session, test_settings):
    cluster = db_session.get(PveCluster, env["cluster_id"])
    cluster.require_restore_approval = True
    db_session.commit()
    created = _create(client, env).json()
    mount_id = created["mount"]["id"]
    assert created["mount"]["status"] == "pending_approval"
    assert created["share_password"]  # the requester still gets it now
    # The agent sees nothing yet.
    assert client.get(AGENT, headers=env["agent"]).json() == []

    assert client.post(f"{BASE}/{mount_id}/approve", headers=env["operator"]).status_code == 403
    approver, approver_id = _scoped_user(db_session, test_settings, env["customer_id"])
    approved = client.post(f"{BASE}/{mount_id}/approve", headers=approver)
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"
    assert approved.json()["approved_by_user_id"] == str(approver_id)
    assert client.post(f"{BASE}/{mount_id}/approve", headers=approver).status_code == 409
    assert [j["kind"] for j in client.get(AGENT, headers=env["agent"]).json()] == ["provision"]
    assert "disk_mount_approved" in _audit_text(db_session)


def test_reject_ends_the_mount_and_forgets_the_password(client, env, db_session, test_settings):
    cluster = db_session.get(PveCluster, env["cluster_id"])
    cluster.require_restore_approval = True
    db_session.commit()
    mount_id = _create(client, env).json()["mount"]["id"]
    approver, _ = _scoped_user(db_session, test_settings, env["customer_id"])
    rejected = client.post(f"{BASE}/{mount_id}/reject", json={"reason": "Not needed"}, headers=approver).json()
    assert rejected["status"] == "rejected" and rejected["approval_notes"] == "Not needed"
    db_session.expire_all()
    assert db_session.get(DiskMount, uuid.UUID(mount_id)).share_password_ciphertext is None


# --- agent backchannel + lifecycle -----------------------------------------------------------


def test_full_lifecycle(client, env, db_session, caplog):
    caplog.set_level(logging.DEBUG)
    created = _create(client, env).json()
    password = created["share_password"]
    mount_id = created["mount"]["id"]

    jobs = client.get(AGENT, headers=env["agent"]).json()
    assert jobs == [
        {
            "id": mount_id,
            "kind": "provision",
            "target_node": "pve1",
            "helper_name": f"rsgm-mount-{uuid.UUID(mount_id).hex[:8]}",
            "helper_vmid": None,
            "mapped_devices": [],
            "snippet_volume": None,
            "snapshot_path": "vm/116/2026-09-14T00:48:57Z",
        }
    ]
    assert password not in json.dumps(jobs)

    claim = _provision(client, env, mount_id, ready=False)
    assert claim["share_password"] == password
    assert claim["smb_username"] == created["mount"]["smb_username"]
    assert claim["pbs_namespace"] == "Kunde" and claim["pbs_datastore"] == "store1"
    assert claim["disks"] == [
        {"archive": "drive-scsi0.img.fidx", "serial": "rsgm1", "slot": "scsi1"},
        {"archive": "drive-scsi1.img.fidx", "serial": "rsgm2", "slot": "scsi2"},
    ]
    assert claim["net0"] == "model=virtio,bridge=vmbr0,tag=20"
    assert claim["ipconfig0"] == "ip=192.168.20.50/24,gw=192.168.20.1"
    assert claim["helper_template_vmid"] == 9100
    assert password not in claim["user_data"]
    db_session.expire_all()
    row = db_session.get(DiskMount, uuid.UUID(mount_id))
    assert row.share_password_ciphertext is None  # forgotten once handed over
    assert row.status == "provisioning" and row.helper_vmid == 4242
    assert [d["device"] for d in row.mapped_devices] == ["/dev/loop3", "/dev/loop4"]
    # Claimed only once.
    assert client.post(f"{AGENT}/{mount_id}/claim", headers=env["agent"]).status_code == 409

    _ = client.post(
        f"{AGENT}/{mount_id}/status",
        json={
            "status": "ready",
            "addresses": ["192.168.20.50"],
            "volumes": [
                {
                    "disk_index": 1,
                    "archive": "drive-scsi0.img.fidx",
                    "partition": "part2",
                    "fstype": "ntfs",
                    "name": "disk1-part2-ntfs",
                    "mounted": True,
                }
            ],
        },
        headers=env["agent"],
    )
    ready = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()
    assert ready["status"] == "ready"
    expires = datetime.fromisoformat(ready["expires_at"])
    assert expires - datetime.fromisoformat(ready["ready_at"]) == timedelta(hours=4)
    assert ready["connection"] == {
        "host": "192.168.20.50",
        "smb_unc": "\\\\192.168.20.50\\restore",
        "smb_url": "smb://192.168.20.50/restore",
        "smb_username": created["mount"]["smb_username"],
        "nfs_exports": ["192.168.20.50:/srv/resurgam/disk1-part2-ntfs"],
    }

    extended = client.post(f"{BASE}/{mount_id}/extend", json={"hours": 2}, headers=env["operator"]).json()
    assert datetime.fromisoformat(extended["expires_at"]) - expires == timedelta(hours=2)
    capped = client.post(f"{BASE}/{mount_id}/extend", json={"hours": 24}, headers=env["operator"])
    for _ in range(3):
        capped = client.post(f"{BASE}/{mount_id}/extend", json={"hours": 24}, headers=env["operator"])
    cap = datetime.fromisoformat(capped.json()["expires_at"])
    assert cap - datetime.now(UTC) <= timedelta(hours=72)
    assert client.post(f"{BASE}/{mount_id}/extend", json={"hours": 25}, headers=env["operator"]).status_code == 422

    stopping = client.post(f"{BASE}/{mount_id}/stop", headers=env["operator"]).json()
    assert stopping["status"] == "stopping" and stopping["teardown_reason"] == "stopped"
    assert stopping["connection"]["host"] is None
    # Stop again is a no-op.
    assert client.post(f"{BASE}/{mount_id}/stop", headers=env["operator"]).json()["status"] == "stopping"
    teardown = client.get(AGENT, headers=env["agent"]).json()
    assert teardown[0]["kind"] == "teardown"
    assert teardown[0]["helper_vmid"] == 4242
    assert teardown[0]["mapped_devices"] == [
        {"archive": "drive-scsi0.img.fidx", "device": "/dev/loop3"},
        {"archive": "drive-scsi1.img.fidx", "device": "/dev/loop4"},
    ]
    assert teardown[0]["snippet_volume"] == _snippet_volume(mount_id)

    done = client.post(f"{AGENT}/{mount_id}/teardown-status", json={"status": "done"}, headers=env["agent"])
    assert done.status_code == 204
    final = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()
    assert final["status"] == "stopped" and final["helper_vmid"] is None and final["ended_at"]
    assert client.get(AGENT, headers=env["agent"]).json() == []
    assert client.get(f"{BASE}?active_only=true", headers=env["operator"]).json() == []

    audit = _audit_text(db_session)
    for action in (
        "disk_mount_created",
        "disk_mount_provisioning_started",
        "disk_mount_ready",
        "disk_mount_extended",
        "disk_mount_stop_requested",
        "disk_mount_cleaned_up",
    ):
        assert action in audit, action
    assert password not in audit and password not in caplog.text


def test_ready_volume_report_is_sanitised(client, env):
    mount_id = _create(client, env).json()["mount"]["id"]
    _provision(client, env, mount_id)
    mount = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()
    assert [v["reason"] for v in mount["volumes"]] == [None, "encrypted"]
    assert "unexpected" not in json.dumps(mount["volumes"])


def test_agent_of_another_cluster_sees_nothing(client, env, platform_admin_headers, db_session):
    mount_id = _create(client, env).json()["mount"]["id"]
    _, other_agent = _lab_cluster(client, platform_admin_headers, db_session, host="pve-other.internal.example")
    assert client.get(AGENT, headers=other_agent).json() == []
    assert client.post(f"{AGENT}/{mount_id}/claim", headers=other_agent).status_code == 404
    assert client.get(AGENT).status_code == 401


def test_failed_provisioning_with_leftovers_is_torn_down_then_failed(client, env, db_session):
    mount_id = _create(client, env).json()["mount"]["id"]
    _provision(client, env, mount_id, ready=False)
    client.post(
        f"{AGENT}/{mount_id}/status",
        json={"status": "failed", "error": "qm start failed", "cleaned_up": False},
        headers=env["agent"],
    )
    mount = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()
    assert mount["status"] == "stopping" and mount["teardown_reason"] == "failed"
    assert mount["error"] == "qm start failed"

    failed = client.post(
        f"{AGENT}/{mount_id}/teardown-status", json={"status": "failed", "error": "unmap busy"}, headers=env["agent"]
    )
    assert failed.status_code == 204
    db_session.expire_all()
    row = db_session.get(DiskMount, uuid.UUID(mount_id))
    assert row.teardown_attempts == 1 and row.next_teardown_attempt_at > datetime.now(UTC)
    assert client.get(AGENT, headers=env["agent"]).json() == []  # backing off
    row.next_teardown_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert client.get(AGENT, headers=env["agent"]).json()[0]["kind"] == "teardown"
    client.post(f"{AGENT}/{mount_id}/teardown-status", json={"status": "done"}, headers=env["agent"])
    assert client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()["status"] == "failed"
    assert "disk_mount_cleanup_failed" in _audit_text(db_session)


def test_failed_provisioning_already_cleaned_up_ends_at_once(client, env):
    mount_id = _create(client, env).json()["mount"]["id"]
    _provision(client, env, mount_id, ready=False)
    client.post(
        f"{AGENT}/{mount_id}/status",
        json={"status": "failed", "error": "wrong node", "cleaned_up": True},
        headers=env["agent"],
    )
    mount = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()
    assert mount["status"] == "failed" and mount["helper_vmid"] is None


def test_stop_while_provisioning_keeps_stopping_when_the_agent_reports_ready(client, env):
    mount_id = _create(client, env).json()["mount"]["id"]
    _provision(client, env, mount_id, ready=False)
    assert client.post(f"{BASE}/{mount_id}/stop", headers=env["operator"]).json()["status"] == "stopping"
    client.post(f"{AGENT}/{mount_id}/status", json={"status": "ready"}, headers=env["agent"])
    mount = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()
    assert mount["status"] == "stopping" and mount["ready_at"] is None


def test_stop_of_a_queued_mount_ends_it_without_agent_work(client, env, db_session):
    mount_id = _create(client, env).json()["mount"]["id"]
    stopped = client.post(f"{BASE}/{mount_id}/stop", headers=env["operator"]).json()
    assert stopped["status"] == "stopped"
    assert client.get(AGENT, headers=env["agent"]).json() == []
    assert client.post(f"{BASE}/{mount_id}/stop", headers=env["operator"]).status_code == 409
    db_session.expire_all()
    assert db_session.get(DiskMount, uuid.UUID(mount_id)).share_password_ciphertext is None


def test_extend_requires_ready(client, env):
    mount_id = _create(client, env).json()["mount"]["id"]
    response = client.post(f"{BASE}/{mount_id}/extend", json={"hours": 1}, headers=env["operator"])
    assert response.status_code == 409


def test_progress_rejects_a_non_loop_device(client, env):
    mount_id = _create(client, env).json()["mount"]["id"]
    client.post(f"{AGENT}/{mount_id}/claim", headers=env["agent"])
    response = client.post(
        f"{AGENT}/{mount_id}/progress",
        json={"mapped_device": {"archive": "drive-scsi0.img.fidx", "device": "/dev/sda"}},
        headers=env["agent"],
    )
    assert response.status_code == 422


# --- cluster range ---------------------------------------------------------------------------


def test_cluster_range_address_is_allocated_and_freed(client, env, db_session):
    cluster = db_session.get(PveCluster, env["cluster_id"])
    cluster.lab_appliance_uplink_vlan_tag = 30
    cluster.lab_appliance_uplink_ip_mode = "static_range"
    cluster.lab_appliance_uplink_ip_range_start = "10.30.0.100"
    cluster.lab_appliance_uplink_ip_range_end = "10.30.0.101"
    cluster.lab_appliance_uplink_ip_prefix_length = 24
    cluster.lab_appliance_uplink_gateway = "10.30.0.1"
    cluster.lab_appliance_uplink_dns_servers = ["10.30.0.1"]
    db_session.commit()
    body = {"ip_mode": "cluster_range", "bridge": None, "vlan_tag": None, "ip_address_cidr": None, "gateway": None}
    mount = _create(client, env, **body).json()["mount"]
    assert mount["ip_address_cidr"] == "10.30.0.100/24"
    assert mount["bridge"] == "vmbr0" and mount["vlan_tag"] == 30 and mount["gateway"] == "10.30.0.1"
    claim = client.post(f"{AGENT}/{mount['id']}/claim", headers=env["agent"]).json()
    assert claim["net0"] == "model=virtio,bridge=vmbr0,tag=30"
    assert claim["ipconfig0"] == "ip=10.30.0.100/24,gw=10.30.0.1"
    assert db_session.query(LabUplinkIpAllocation).filter_by(disk_mount_id=uuid.UUID(mount["id"])).count() == 1

    client.post(f"{BASE}/{mount['id']}/stop", headers=env["operator"])
    client.post(f"{AGENT}/{mount['id']}/teardown-status", json={"status": "done"}, headers=env["agent"])
    assert db_session.query(LabUplinkIpAllocation).filter_by(disk_mount_id=uuid.UUID(mount["id"])).count() == 0


def test_cluster_range_needs_a_static_uplink(client, env):
    body = {"ip_mode": "cluster_range", "bridge": None, "vlan_tag": None, "ip_address_cidr": None, "gateway": None}
    response = _create(client, env, **body)
    assert response.status_code == 400
    assert response.json()["code"] == "disk_mount_cluster_range_unavailable"


# --- sweep -----------------------------------------------------------------------------------


def test_sweep_expires_and_catches_orphans(client, env, db_session):
    ids = [_create(client, env, ip_address_cidr=f"192.168.20.{60 + i}/24").json()["mount"]["id"] for i in range(4)]
    ready_id, stale_id, unclaimed_id, stuck_id = (uuid.UUID(i) for i in ids)
    _provision(client, env, str(ready_id))
    client.post(f"{AGENT}/{stuck_id}/claim", headers=env["agent"])
    now = datetime.now(UTC)
    db_session.expire_all()
    rows = {i: db_session.get(DiskMount, i) for i in (ready_id, stale_id, unclaimed_id, stuck_id)}
    rows[ready_id].expires_at = now - timedelta(minutes=1)
    rows[stale_id].status = "pending_approval"
    rows[stale_id].created_at = now - timedelta(hours=25)
    rows[unclaimed_id].queued_at = now - timedelta(minutes=31)
    rows[stuck_id].last_progress_at = now - timedelta(minutes=46)
    db_session.commit()

    changes = dict(lifecycle.sweep(db_session, now=now))
    db_session.commit()

    assert changes == {
        ready_id: "disk_mount_expired",
        stale_id: "disk_mount_approval_expired",
        unclaimed_id: "disk_mount_failed",
        stuck_id: "disk_mount_failed",
    }
    db_session.expire_all()
    assert (rows[ready_id].status, rows[ready_id].teardown_reason) == ("stopping", "expired")
    assert rows[stale_id].status == "expired" and rows[stale_id].share_password_ciphertext is None
    assert rows[unclaimed_id].status == "failed" and "did not pick" in rows[unclaimed_id].error
    assert (rows[stuck_id].status, rows[stuck_id].teardown_reason) == ("stopping", "failed")
    # Nothing else is due: a second sweep changes nothing.
    assert lifecycle.sweep(db_session, now=now) == []

    client.post(f"{AGENT}/{ready_id}/teardown-status", json={"status": "done"}, headers=env["agent"])
    assert client.get(f"{BASE}/{ready_id}", headers=env["operator"]).json()["status"] == "expired"


def test_scheduler_runs_the_sweep(client, env, db_session):
    from resurgam_api.worker import recovery_lab_scheduler

    mount_id = uuid.UUID(_create(client, env).json()["mount"]["id"])
    row = db_session.get(DiskMount, mount_id)
    row.queued_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    recovery_lab_scheduler._sweep_disk_mounts(db_session)
    db_session.expire_all()
    assert db_session.get(DiskMount, mount_id).status == "failed"


# --- lookups + admin -------------------------------------------------------------------------


def test_snapshot_disks_lists_disk_archives(client, env):
    response = client.get(
        f"{BASE}/snapshot-disks", params={"backup_snapshot_id": str(env["snapshot_id"])}, headers=env["operator"]
    )
    assert response.status_code == 200
    assert [d["slot"] for d in response.json()] == ["scsi0", "scsi1"]


def test_helper_template_can_only_be_set_by_platform_admins(client, env, platform_admin_headers, db_session):
    url = f"{BASE}/placement/{env['cluster_id']}/helper-template"
    assert client.put(url, json={"helper_template_vmid": 9200}, headers=env["operator"]).status_code == 403
    response = client.put(url, json={"helper_template_vmid": 9200}, headers=platform_admin_headers)
    assert response.status_code == 200 and response.json()["helper_template_vmid"] == 9200
    assert "pve_cluster_disk_mount_helper_template_set" in _audit_text(db_session)


def test_bridges_for_the_picker_are_live_from_pve(client, env, monkeypatch):
    from resurgam_api.connectivity.pve_lab_api import NodeBridge

    monkeypatch.setattr(
        disk_mounts_module,
        "list_node_bridges",
        lambda *a, node, **kw: [NodeBridge(iface="vmbr0", type="bridge", active=True, vlan_aware=True, comments=None)],
    )
    response = client.get(f"{BASE}/placement/{env['cluster_id']}/nodes/pve1/bridges", headers=env["operator"])
    assert response.status_code == 200 and response.json()[0]["iface"] == "vmbr0"


# --- teardown robustness (stuck `stopping`) ---------------------------------------------------


def _stopping_mount(client, env):
    mount_id = _create(client, env).json()["mount"]["id"]
    _provision(client, env, mount_id, ready=False)
    client.post(
        f"{AGENT}/{mount_id}/status",
        json={"status": "failed", "error": "qm start failed", "cleaned_up": False},
        headers=env["agent"],
    )
    return mount_id


def test_teardown_error_keeps_the_original_error(client, env, db_session):
    mount_id = _stopping_mount(client, env)
    for message in ("unmap busy", "still busy"):
        client.post(
            f"{AGENT}/{mount_id}/teardown-status", json={"status": "failed", "error": message}, headers=env["agent"]
        )
    error = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()["error"]
    assert error.startswith("qm start failed")
    assert "still busy" in error and "unmap busy" not in error  # the latest teardown error only


def test_teardown_backoff_never_overflows(db_session, monkeypatch):
    # 2**(n-1) minutes overflowed timedelta at n ~ 42; the exponent is capped first.
    monkeypatch.setattr(lifecycle, "TEARDOWN_MAX_ATTEMPTS", 10_000)
    mount = DiskMount(id=uuid.uuid4(), teardown_attempts=100, mapped_devices=[], error=None)
    now = datetime.now(UTC)
    assert lifecycle.teardown_failed(db_session, mount, "x", now=now) is False
    assert mount.next_teardown_attempt_at - now == lifecycle.TEARDOWN_BACKOFF_MAX


def test_teardown_gives_up_after_the_retry_limit_and_frees_the_address(client, env, db_session):
    cluster = db_session.get(PveCluster, env["cluster_id"])
    cluster.lab_appliance_uplink_ip_mode = "static_range"
    cluster.lab_appliance_uplink_ip_range_start = "10.30.0.100"
    cluster.lab_appliance_uplink_ip_range_end = "10.30.0.101"
    cluster.lab_appliance_uplink_ip_prefix_length = 24
    cluster.lab_appliance_uplink_gateway = "10.30.0.1"
    cluster.lab_appliance_uplink_dns_servers = ["10.30.0.1"]
    db_session.commit()
    body = {"ip_mode": "cluster_range", "bridge": None, "vlan_tag": None, "ip_address_cidr": None, "gateway": None}
    mount_id = _create(client, env, **body).json()["mount"]["id"]
    _provision(client, env, mount_id, ready=False)
    client.post(f"{BASE}/{mount_id}/stop", headers=env["operator"])
    for attempt in range(lifecycle.TEARDOWN_MAX_ATTEMPTS):
        db_session.expire_all()
        row = db_session.get(DiskMount, uuid.UUID(mount_id))
        assert row.status == "stopping", attempt
        response = client.post(
            f"{AGENT}/{mount_id}/teardown-status", json={"status": "failed", "error": "VM locked"}, headers=env["agent"]
        )
        assert response.status_code == 204
    mount = client.get(f"{BASE}/{mount_id}", headers=env["operator"]).json()
    assert mount["status"] == "failed"
    assert (
        "Gave up after 10" in mount["error"] and "helper VM 4242" in mount["error"] and "/dev/loop3" in mount["error"]
    )
    assert db_session.query(LabUplinkIpAllocation).filter_by(disk_mount_id=uuid.UUID(mount_id)).count() == 0
    assert "disk_mount_cleanup_gave_up" in _audit_text(db_session)
    assert client.get(AGENT, headers=env["agent"]).json() == []


def test_force_finish_is_admin_only_and_only_for_stopping(client, env, platform_admin_headers, db_session):
    mount_id = _stopping_mount(client, env)
    assert client.post(f"{BASE}/{mount_id}/force-finish", headers=env["operator"]).status_code == 403
    finished = client.post(f"{BASE}/{mount_id}/force-finish", headers=platform_admin_headers)
    assert finished.status_code == 200, finished.text
    body = finished.json()
    assert body["status"] == "failed"
    assert body["error"].startswith("qm start failed") and "force-finished" in body["error"]
    assert "disk_mount_force_finished" in _audit_text(db_session)
    again = client.post(f"{BASE}/{mount_id}/force-finish", headers=platform_admin_headers)
    assert again.status_code == 409 and again.json()["code"] == "disk_mount_not_stopping"


# --- agent input validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    "volume",
    [
        "local:snippets/../../etc/passwd",
        "local:iso/resurgam-disk-mount-000000000000.yaml",
        "local:snippets/resurgam-lab-other.yaml",
        "local:snippets/resurgam-disk-mount-000000000000.yaml\n",
    ],
)
def test_progress_rejects_a_foreign_snippet_volume(client, env, volume):
    mount_id = _create(client, env).json()["mount"]["id"]
    client.post(f"{AGENT}/{mount_id}/claim", headers=env["agent"])
    response = client.post(f"{AGENT}/{mount_id}/progress", json={"snippet_volume": volume}, headers=env["agent"])
    assert response.status_code == 422


def test_password_unavailable_is_audited(client, env, db_session):
    mount_id = _create(client, env).json()["mount"]["id"]
    row = db_session.get(DiskMount, uuid.UUID(mount_id))
    row.share_password_ciphertext = None
    db_session.commit()
    response = client.post(f"{AGENT}/{mount_id}/claim", headers=env["agent"])
    assert response.status_code == 409 and response.json()["code"] == "password_unavailable"
    assert "password_unavailable" in _audit_text(db_session)


# --- create validation -----------------------------------------------------------------------


def test_target_node_must_be_the_lab_agent_node_when_known(client, env, db_session):
    cluster = db_session.get(PveCluster, env["cluster_id"])
    cluster.lab_agent_node = "pve2"
    db_session.commit()
    response = _create(client, env)
    assert response.status_code == 400 and response.json()["code"] == "disk_mount_wrong_node"
    assert "pve2" in response.json()["message"]
    assert _create(client, env, target_node="pve2").status_code == 201


def test_static_address_inside_the_uplink_range_is_refused(client, env, db_session):
    cluster = db_session.get(PveCluster, env["cluster_id"])
    cluster.lab_appliance_uplink_ip_mode = "static_range"
    cluster.lab_appliance_uplink_ip_range_start = "192.168.20.40"
    cluster.lab_appliance_uplink_ip_range_end = "192.168.20.60"
    cluster.lab_appliance_uplink_ip_prefix_length = 24
    cluster.lab_appliance_uplink_gateway = "192.168.20.1"
    cluster.lab_appliance_uplink_dns_servers = ["192.168.20.1"]
    db_session.commit()
    response = _create(client, env)  # 192.168.20.50
    assert response.status_code == 409 and response.json()["code"] == "disk_mount_address_in_uplink_range"
    assert _create(client, env, ip_address_cidr="192.168.20.70/24").status_code == 201


def test_cluster_delete_refuses_active_mounts_and_removes_ended_ones(client, env, platform_admin_headers, db_session):
    """Disk mounts reference their cluster (RESTRICT) -- the delete used to 500."""
    mount_id = _create(client, env).json()["mount"]["id"]
    refused = client.delete(f"/api/v1/pve-clusters/{env['cluster_id']}", headers=platform_admin_headers)
    assert refused.status_code == 409
    assert refused.json()["code"] == "pve_cluster_has_active_disk_mounts"

    client.post(f"{BASE}/{mount_id}/stop", headers=env["operator"])  # queued -> stopped at once
    deleted = client.delete(f"/api/v1/pve-clusters/{env['cluster_id']}", headers=platform_admin_headers)
    assert deleted.status_code == 204, deleted.text
    db_session.expire_all()
    assert db_session.get(DiskMount, uuid.UUID(mount_id)) is None
