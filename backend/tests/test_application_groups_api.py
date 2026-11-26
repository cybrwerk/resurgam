import uuid
from datetime import UTC


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _create_group(db_session, backup_id="116"):
    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.models.pbs import PbsInstance

    instance = PbsInstance(
        display_name="PBS",
        host="pbs1.internal.example",
        port=8007,
        tls_trust_mode="ca",
        api_token_secret_ref_id=_fresh_secret_ref(db_session),
    )
    db_session.add(instance)
    db_session.flush()
    datastore = Datastore(pbs_instance_id=instance.id, name="store1")
    db_session.add(datastore)
    db_session.flush()
    group = BackupGroup(datastore_id=datastore.id, backup_type="vm", backup_id=backup_id, namespace="")
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _fresh_secret_ref(db_session):
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(
        reference_key=f"test-{uuid.uuid4()}", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    return ref.id


def test_create_application_group_with_members(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    group_id = _create_group(db_session)

    response = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "Order Processing",
            "members": [{"backup_group_id": str(group_id), "startup_order": 0, "check_types": ["icmp", "tcp_port"]}],
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Order Processing"
    assert len(body["members"]) == 1
    assert body["members"][0]["check_types"] == ["icmp", "tcp_port"]
    assert body["members"][0]["backup_group_superseded"] is False


def test_member_flags_backup_group_superseded_after_a_split(client, platform_admin_headers, db_session):
    """A VMID reuse must freeze the membership, not silently retarget it
    -- the member keeps pointing at the closed-out group, and the API
    surfaces that it needs review.
    """
    from datetime import datetime

    from resurgam_api.models.inventory import BackupGroup

    customer_id = _create_customer(db_session)
    group_id = _create_group(db_session)

    create_response = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "Order Processing",
            "members": [{"backup_group_id": str(group_id), "startup_order": 0, "check_types": ["icmp"]}],
        },
        headers=platform_admin_headers,
    )
    app_group_id = create_response.json()["id"]

    group = db_session.get(BackupGroup, group_id)
    group.superseded_at = datetime.now(UTC)
    db_session.commit()

    response = client.get(f"/api/v1/application-groups/{app_group_id}", headers=platform_admin_headers)
    assert response.status_code == 200
    member = response.json()["members"][0]
    assert member["backup_group_id"] == str(group_id)
    assert member["backup_group_superseded"] is True


def test_create_application_group_unknown_customer_returns_400(client, platform_admin_headers):

    response = client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(uuid.uuid4()), "name": "x", "members": []},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400


def test_list_application_groups_scoped_to_customer(client, platform_admin_headers, db_session):
    customer_a = _create_customer(db_session, "A", "a")
    customer_b = _create_customer(db_session, "B", "b")
    client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(customer_a), "name": "GroupA", "members": []},
        headers=platform_admin_headers,
    )
    client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(customer_b), "name": "GroupB", "members": []},
        headers=platform_admin_headers,
    )

    response = client.get("/api/v1/application-groups", headers=platform_admin_headers)
    assert response.status_code == 200
    names = {g["name"] for g in response.json()}
    assert names == {"GroupA", "GroupB"}


def test_update_application_group_replaces_members(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    group_id = _create_group(db_session)
    created = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "G",
            "members": [{"backup_group_id": str(group_id), "startup_order": 0, "check_types": []}],
        },
        headers=platform_admin_headers,
    ).json()

    updated = client.patch(
        f"/api/v1/application-groups/{created['id']}",
        json={"name": "G renamed", "members": []},
        headers=platform_admin_headers,
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "G renamed"
    assert body["members"] == []


def _give_member_run_history(db_session, app_group_id, member_id, backup_group_id):
    """Attaches a real `RestoreTestPlanMember` to `member_id`, same shape
    Recovery Lab itself leaves behind after a real run -- reproduces the
    exact precondition for the update_application_group 500 (a group
    that's actually been used in Recovery Lab, not just created).
    """
    from datetime import datetime

    from resurgam_api.models.inventory import BackupSnapshot
    from resurgam_api.models.pve import PveCluster
    from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanMember
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(
        reference_key=f"test-{uuid.uuid4()}", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    cluster = PveCluster(
        display_name="Lab", host="lab.example", port=8006, tls_trust_mode="ca", api_token_secret_ref_id=ref.id
    )
    db_session.add(cluster)
    db_session.flush()
    snapshot = BackupSnapshot(
        backup_group_id=backup_group_id, snapshot_time=datetime.now(UTC), size_bytes=1, verify_status="ok"
    )
    db_session.add(snapshot)
    db_session.flush()
    plan = RestoreTestPlan(
        application_group_id=app_group_id,
        pve_cluster_id=cluster.id,
        vnet_name="vnet-test",
        status="completed",
        requested_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        RestoreTestPlanMember(
            restore_test_plan_id=plan.id,
            application_group_member_id=member_id,
            backup_snapshot_id=snapshot.id,
            target_vmid=999,
            target_node="node1",
            target_storage="local",
            member_status="passed",
        )
    )
    db_session.commit()


def test_update_application_group_with_member_run_history_does_not_crash(client, platform_admin_headers, db_session):
    """Real bug, raised directly by the user: saving any edit to a group
    that has ever run in Recovery Lab 500'd (surfaced to the browser as an
    opaque CORS failure) because the old `_replace_members` deleted every
    member row unconditionally, and `RestoreTestPlanMember.
    application_group_member_id` is `RESTRICT` -- a member with real run
    history can't be deleted, even transiently. A member kept across the
    edit must now be updated in place instead.
    """
    customer_id = _create_customer(db_session)
    group_id = _create_group(db_session)
    created = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "G",
            "members": [{"backup_group_id": str(group_id), "startup_order": 0, "check_types": ["icmp"]}],
        },
        headers=platform_admin_headers,
    ).json()
    member_id = created["members"][0]["id"]
    _give_member_run_history(db_session, created["id"], member_id, group_id)

    # Same member kept (by backup_group_id), just a field changed -- this
    # is exactly what "click Save with no real changes" sends.
    response = client.patch(
        f"/api/v1/application-groups/{created['id']}",
        json={"members": [{"backup_group_id": str(group_id), "startup_order": 1, "check_types": ["icmp", "tcp_port"]}]},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["id"] == member_id  # same row, not delete+recreate
    assert body["members"][0]["startup_order"] == 1
    assert body["members"][0]["check_types"] == ["icmp", "tcp_port"]


def test_update_application_group_removing_member_with_run_history_returns_409(
    client, platform_admin_headers, db_session
):
    customer_id = _create_customer(db_session)
    group_id = _create_group(db_session)
    created = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "G",
            "members": [{"backup_group_id": str(group_id), "startup_order": 0, "check_types": []}],
        },
        headers=platform_admin_headers,
    ).json()
    member_id = created["members"][0]["id"]
    _give_member_run_history(db_session, created["id"], member_id, group_id)

    response = client.patch(
        f"/api/v1/application-groups/{created['id']}", json={"members": []}, headers=platform_admin_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "application_group_member_has_runs"


def test_delete_application_group(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    created = client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(customer_id), "name": "G", "members": []},
        headers=platform_admin_headers,
    ).json()

    response = client.delete(f"/api/v1/application-groups/{created['id']}", headers=platform_admin_headers)
    assert response.status_code == 204
    assert client.get(f"/api/v1/application-groups/{created['id']}", headers=platform_admin_headers).status_code == 404


def _create_lab_cluster(client, headers) -> str:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Lab PVE",
            "host": "pve1.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "is_lab_compute": True,
            "lab_appliance_template_vmid": 9000,
            "lab_snippet_storage_id": "local",
            "lab_appliance_uplink_bridge": "vmbr0",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_create_application_group_rejects_incomplete_schedule(client, platform_admin_headers, db_session):
    """Raised directly by the user ("the lab Scheduler (Cron Like is not
    there)"): a scheduled run has no human present to pick a target
    cluster/node/storage the way a manual run's CreateRunForm does, so
    all four must be set together before the schedule can be enabled.
    """
    customer_id = _create_customer(db_session)
    response = client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(customer_id), "name": "G", "members": [], "lab_schedule_enabled": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_create_application_group_with_complete_schedule(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    cluster_id = _create_lab_cluster(client, platform_admin_headers)

    response = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "G",
            "members": [],
            "lab_schedule_enabled": True,
            "lab_schedule_interval_hours": 6,
            "lab_schedule_pve_cluster_id": cluster_id,
            "lab_schedule_target_node": "pve1",
            "lab_schedule_target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lab_schedule_enabled"] is True
    assert body["lab_schedule_interval_hours"] == 6
    assert body["lab_schedule_pve_cluster_id"] == cluster_id
    assert body["lab_next_scheduled_run_at"] is not None


def test_update_application_group_rejects_enabling_schedule_without_prerequisites(
    client, platform_admin_headers, db_session
):
    customer_id = _create_customer(db_session)
    created = client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(customer_id), "name": "G", "members": []},
        headers=platform_admin_headers,
    ).json()

    response = client.patch(
        f"/api/v1/application-groups/{created['id']}",
        json={"lab_schedule_enabled": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_lab_schedule"


def test_update_application_group_enables_schedule_across_separate_patches(client, platform_admin_headers, db_session):
    """A PATCH is partial -- the prerequisite fields set in an earlier
    request must still count when a later request enables the schedule,
    not just fields resent in the same call.
    """
    customer_id = _create_customer(db_session)
    cluster_id = _create_lab_cluster(client, platform_admin_headers)
    created = client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(customer_id), "name": "G", "members": []},
        headers=platform_admin_headers,
    ).json()

    first = client.patch(
        f"/api/v1/application-groups/{created['id']}",
        json={
            "lab_schedule_interval_hours": 12,
            "lab_schedule_pve_cluster_id": cluster_id,
            "lab_schedule_target_node": "pve1",
            "lab_schedule_target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    )
    assert first.status_code == 200
    assert first.json()["lab_next_scheduled_run_at"] is None  # not enabled yet

    second = client.patch(
        f"/api/v1/application-groups/{created['id']}",
        json={"lab_schedule_enabled": True},
        headers=platform_admin_headers,
    )
    assert second.status_code == 200
    body = second.json()
    assert body["lab_schedule_enabled"] is True
    assert body["lab_next_scheduled_run_at"] is not None


def test_update_application_group_disabling_schedule_clears_next_run(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    cluster_id = _create_lab_cluster(client, platform_admin_headers)
    created = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "G",
            "members": [],
            "lab_schedule_enabled": True,
            "lab_schedule_interval_hours": 6,
            "lab_schedule_pve_cluster_id": cluster_id,
            "lab_schedule_target_node": "pve1",
            "lab_schedule_target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    assert created["lab_next_scheduled_run_at"] is not None

    response = client.patch(
        f"/api/v1/application-groups/{created['id']}",
        json={"lab_schedule_enabled": False},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["lab_next_scheduled_run_at"] is None


def test_application_groups_require_restore_access_role(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    customer_id = _create_customer(db_session)
    user = User(email="viewer-appgroups@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/v1/application-groups",
        json={"customer_id": str(customer_id), "name": "G", "members": []},
        headers=headers,
    )
    assert response.status_code == 404
