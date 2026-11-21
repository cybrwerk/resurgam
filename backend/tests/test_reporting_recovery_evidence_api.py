"""Endpoint tests for the Recovery Evidence compliance report
(`GET /reporting/recovery-evidence`). Focuses on customer-scoping (the
one thing every section must get right independently) and the
`framework=True` annotation toggle -- the underlying aggregation
functions' own edge cases (protection-status classification boundaries)
are already covered by `evaluate_protection_status`'s own tests in
`test_protection.py`, not re-tested here.
"""

from datetime import UTC, datetime, timedelta

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


def _create_pbs_instance(client, headers, display_name="PBS") -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": display_name,
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "test@pbs!token:secretvalue",
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


def _create_customer_scoped_viewer(db_session, test_settings, customer_id) -> dict:
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


def _create_group(db_session, datastore_id, customer_id, backup_id="100", backup_type="vm", **overrides):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(
        datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id, customer_id=customer_id, **overrides
    )
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _add_snapshot(db_session, group_id, size_bytes=1000, verify_status="ok", snapshot_time=None):
    from resurgam_api.models.inventory import BackupSnapshot

    snapshot = BackupSnapshot(
        backup_group_id=group_id,
        snapshot_time=snapshot_time or datetime.now(UTC),
        size_bytes=size_bytes,
        verify_status=verify_status,
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.commit()
    return snapshot.id


def _create_file_index_job(db_session, group_id, snapshot_id, customer_id, status="failed"):
    from resurgam_api.models.file_index_job import FileIndexJob

    job = FileIndexJob(
        status=status,
        trigger="manual",
        backup_group_id=group_id,
        backup_snapshot_id=snapshot_id,
        customer_id=customer_id,
        error_message="disk full",
    )
    db_session.add(job)
    db_session.commit()
    return job.id


def _create_audit_event(db_session, customer_id, action="restore_plan_created"):
    from resurgam_api.models.audit import AuditEvent

    event = AuditEvent(
        action=action,
        target_type="restore_plan",
        target_id="00000000-0000-0000-0000-000000000000",
        customer_id=customer_id,
    )
    db_session.add(event)
    db_session.commit()
    return event.id


def _setup_customer_with_group(
    client, platform_admin_headers, db_session, monkeypatch, *, customer_name="Acme", slug="acme"
):
    customer_id = _create_customer(db_session, name=customer_name, slug=slug)
    instance_id = _create_pbs_instance(client, platform_admin_headers, display_name=f"PBS-{slug}")
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name=f"store-{slug}")
    group_id = _create_group(db_session, datastore_id, customer_id)
    snapshot_id = _add_snapshot(db_session, group_id)
    return customer_id, group_id, snapshot_id


def test_report_scopes_backup_history_and_failed_jobs_to_the_right_customer(
    client, platform_admin_headers, db_session, monkeypatch
):
    customer_a, group_a, snapshot_a = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch, customer_name="Acme", slug="acme"
    )
    customer_b, group_b, snapshot_b = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch, customer_name="Beta", slug="beta"
    )
    _create_file_index_job(db_session, group_a, snapshot_a, customer_a)
    _create_file_index_job(db_session, group_b, snapshot_b, customer_b)
    _create_audit_event(db_session, customer_a)
    _create_audit_event(db_session, customer_b)

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_a}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()

    assert len(body["backup_history"]) == 1
    assert body["backup_history"][0]["backup_group_id"] == str(group_a)

    assert len(body["failed_jobs"]) == 1
    assert body["failed_jobs"][0]["source"] == "file_index_job"

    assert len(body["audit_trail"]) == 1


def _create_task_run(db_session, instance_id, worker_id, task_type="verify", status="error"):
    import uuid

    from resurgam_api.models.inventory import TaskRun

    task = TaskRun(
        pbs_instance_id=instance_id,
        upid=f"UPID:test:{uuid.uuid4().hex}:",
        task_type=task_type,
        worker_id=worker_id,
        status=status,
        started_at=datetime.now(UTC),
    )
    db_session.add(task)
    db_session.commit()
    return task.id


def test_pbs_task_failures_scoped_to_the_right_customers_datastore_on_a_shared_instance(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Two customers sharing one PBS instance, each with their own
    datastore, must each see only their own datastore's failed tasks --
    the whole point of scoping by the worker-id's datastore-name prefix
    instead of "any error task on this shared instance".
    """
    instance_id = _create_pbs_instance(client, platform_admin_headers, display_name="Shared PBS")

    customer_a = _create_customer(db_session, name="Acme", slug="acme")
    datastore_a = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="store-acme")
    group_a = _create_group(db_session, datastore_a, customer_a)
    _add_snapshot(db_session, group_a)

    customer_b = _create_customer(db_session, name="Beta", slug="beta")
    datastore_b = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="store-beta")
    group_b = _create_group(db_session, datastore_b, customer_b)
    _add_snapshot(db_session, group_b)

    _create_task_run(db_session, instance_id, worker_id="store-acme:vm/100")
    _create_task_run(db_session, instance_id, worker_id="store-beta:vm/200")
    # A garbage-collection task's worker-id is the bare datastore name,
    # no colon -- must still match.
    _create_task_run(db_session, instance_id, worker_id="store-acme", task_type="garbage_collection")

    response_a = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_a}", headers=platform_admin_headers
    )
    assert response_a.status_code == 200
    tasks_a = [e for e in response_a.json()["failed_jobs"] if e["source"] == "pbs_task"]
    assert len(tasks_a) == 2
    assert {e["label"] for e in tasks_a} == {"PBS task (verify)", "PBS task (garbage_collection)"}
    assert all("instance_attribution_only" not in e for e in tasks_a)

    response_b = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_b}", headers=platform_admin_headers
    )
    assert response_b.status_code == 200
    tasks_b = [e for e in response_b.json()["failed_jobs"] if e["source"] == "pbs_task"]
    assert len(tasks_b) == 1
    assert tasks_b[0]["label"] == "PBS task (verify)"


def _create_acl_entry(db_session, instance_id, path, role, subject, subject_type="user", propagate=True):
    from resurgam_api.models.inventory import PbsAclEntry

    entry = PbsAclEntry(
        pbs_instance_id=instance_id,
        path=path,
        role=role,
        subject=subject,
        subject_type=subject_type,
        propagate=propagate,
    )
    db_session.add(entry)
    db_session.commit()
    return entry.id


def test_pbs_native_access_scoped_to_the_right_customers_datastore_on_a_shared_instance(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Two customers sharing one PBS instance, each with their own
    datastore: a grant on one customer's datastore must never leak into
    the other's report, and a root-level grant with propagate=True must
    apply to both, mirroring PBS's own ACL propagation rules.
    """
    instance_id = _create_pbs_instance(client, platform_admin_headers, display_name="Shared PBS")

    customer_a = _create_customer(db_session, name="Acme", slug="acme")
    datastore_a = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="store-acme")
    group_a = _create_group(db_session, datastore_a, customer_a)
    _add_snapshot(db_session, group_a)

    customer_b = _create_customer(db_session, name="Beta", slug="beta")
    datastore_b = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="store-beta")
    group_b = _create_group(db_session, datastore_b, customer_b)
    _add_snapshot(db_session, group_b)

    _create_acl_entry(db_session, instance_id, "/", "Admin", "root@pam", propagate=True)
    _create_acl_entry(db_session, instance_id, "/datastore/store-acme", "Admin", "acme-user@pbs")
    _create_acl_entry(db_session, instance_id, "/datastore/store-beta", "DatastoreAudit", "beta-user@pbs")
    # A namespace-scoped grant nested under customer A's datastore.
    _create_acl_entry(db_session, instance_id, "/datastore/store-acme/prod", "DatastoreBackup", "acme-ns-user@pbs")
    # Not propagating -- must NOT reach either datastore.
    _create_acl_entry(db_session, instance_id, "/", "Audit", "no-propagate@pbs", propagate=False)

    response_a = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_a}", headers=platform_admin_headers
    )
    assert response_a.status_code == 200
    subjects_a = {e["subject"] for e in response_a.json()["pbs_datastore_access"]}
    assert subjects_a == {"root@pam", "acme-user@pbs", "acme-ns-user@pbs"}

    response_b = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_b}", headers=platform_admin_headers
    )
    assert response_b.status_code == 200
    subjects_b = {e["subject"] for e in response_b.json()["pbs_datastore_access"]}
    assert subjects_b == {"root@pam", "beta-user@pbs"}


def test_backup_history_includes_a_superseded_generations_in_range_snapshots(
    client, platform_admin_headers, db_session, monkeypatch
):
    """A reused VMID's closed-out prior generation is still real history
    for the period it was active in -- removing it from backup_history
    would make backup coverage look like it vanished.
    """
    customer_id, group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    from resurgam_api.models.inventory import BackupGroup

    old_group = db_session.get(BackupGroup, group_id)
    old_group.resolved_name = "old-guest"
    old_group.superseded_at = datetime.now(UTC)
    db_session.commit()

    new_group_id = _create_group(db_session, old_group.datastore_id, customer_id, resolved_name="new-guest")
    _add_snapshot(db_session, new_group_id)

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    group_ids = {e["backup_group_id"] for e in response.json()["backup_history"]}
    assert group_ids == {str(group_id), str(new_group_id)}


def test_policy_adherence_and_protection_level_exclude_superseded_generation(
    client, platform_admin_headers, db_session, monkeypatch
):
    customer_id, group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    from resurgam_api.models.inventory import BackupGroup, ProtectionPolicy

    old_group = db_session.get(BackupGroup, group_id)
    policy = ProtectionPolicy(
        customer_id=customer_id,
        name="daily",
        expected_interval_hours=24,
        max_time_since_last_backup_hours=48,
    )
    db_session.add(policy)
    db_session.flush()
    old_group.protection_policy_id = policy.id
    old_group.superseded_at = datetime.now(UTC)
    db_session.commit()

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["policy_adherence"] == []
    assert body["protection_level"]["workloads"] == []


def test_restore_test_evidence_still_shows_a_passed_run_against_a_now_superseded_group(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Freeze, don't hide: a passed Recovery Lab run against a workload
    that later got superseded is still real, valid evidence for its
    period.
    """
    customer_id, group_id, snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.models.pve import PveCluster
    from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanMember
    from resurgam_api.models.secrets import SecretPurpose, SecretReference

    ref = SecretReference(
        reference_key="test-restore-test-evidence-token",
        description="test",
        purpose=SecretPurpose.INFRA_CREDENTIAL,
    )
    db_session.add(ref)
    db_session.flush()
    cluster = PveCluster(
        display_name="Lab",
        host="lab.example",
        port=8006,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
    )
    db_session.add(cluster)
    db_session.flush()

    app_group = ApplicationGroup(customer_id=customer_id, name="App1")
    db_session.add(app_group)
    db_session.flush()
    member = ApplicationGroupMember(application_group_id=app_group.id, backup_group_id=group_id)
    db_session.add(member)
    db_session.flush()

    plan = RestoreTestPlan(
        application_group_id=app_group.id,
        customer_id=customer_id,
        pve_cluster_id=cluster.id,
        vnet_name="vnet-test",
        status="completed",
        requested_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(plan)
    db_session.flush()
    plan_member = RestoreTestPlanMember(
        restore_test_plan_id=plan.id,
        application_group_member_id=member.id,
        backup_snapshot_id=snapshot_id,
        target_vmid=999,
        target_node="node1",
        target_storage="local",
        member_status="passed",
    )
    db_session.add(plan_member)
    db_session.flush()

    old_group = db_session.get(BackupGroup, group_id)
    old_group.superseded_at = datetime.now(UTC)
    db_session.commit()

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    evidence = response.json()["restore_test_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["backup_group_id"] == str(group_id)
    assert evidence[0]["member_status"] == "passed"


def test_report_includes_groups_resolved_via_datastore_assignment(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Regression test: a group's effective customer can come from either
    its own `customer_id` override or its datastore's assignment -- the
    SQL-level rewrite of `_customer_groups` must still resolve both, not
    just the direct-override case every other test here happens to use.
    """
    customer_id = _create_customer(db_session, name="Gamma", slug="gamma")
    instance_id = _create_pbs_instance(client, platform_admin_headers, display_name="PBS-gamma")
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch, name="store-gamma")
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    # No `customer_id` override on the group itself -- resolved only
    # through the datastore's assignment above.
    group_id = _create_group(db_session, datastore_id, customer_id=None)
    _add_snapshot(db_session, group_id)

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["backup_history"]) == 1
    assert body["backup_history"][0]["backup_group_id"] == str(group_id)


def test_report_filters_by_date_range(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _add_snapshot(db_session, group_id, snapshot_time=datetime.now(UTC) - timedelta(days=200))
    _add_snapshot(db_session, group_id, snapshot_time=datetime.now(UTC) - timedelta(days=5))

    # Default (no date_from/date_to) is a 90-day trailing window -- the
    # 200-day-old snapshot must not appear, the recent one and the
    # fixture's own "now" snapshot must.
    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    ids = {e["backup_group_id"] for e in response.json()["backup_history"]}
    assert ids == {str(group_id)}  # sanity: both remaining snapshots belong to this one group
    snapshot_times = [e["snapshot_time"] for e in response.json()["backup_history"]]
    assert len(snapshot_times) == 2  # the fixture's own snapshot + recent_snapshot, not old_snapshot


def test_report_framework_true_annotates_sections(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, _group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )

    plain = client.get(f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers)
    assert plain.json()["framework"] is False
    assert plain.json()["section_controls"] == {}

    annotated = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}&framework=true", headers=platform_admin_headers
    )
    assert annotated.status_code == 200
    body = annotated.json()
    assert body["framework"] is True
    assert "backup_history" in body["section_controls"]
    assert any(c["framework"] == "ISO27001:2022" for c in body["section_controls"]["backup_history"])


def test_report_denies_out_of_scope_customer(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_a, _group_a, _snapshot_a = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch, customer_name="Acme", slug="acme"
    )
    customer_b = _create_customer(db_session, name="Beta", slug="beta")
    viewer_b_headers = _create_customer_scoped_viewer(db_session, test_settings, customer_b)

    response = client.get(f"/api/v1/reporting/recovery-evidence?customer_id={customer_a}", headers=viewer_b_headers)
    assert response.status_code == 404


def test_report_includes_offsite_status_via_sync_job(client, platform_admin_headers, db_session, monkeypatch):
    """A `SyncJob` targeting this customer's datastore is real replication
    evidence for the 3-2-1-1-0 "1 offsite" leg -- confirmed end to end
    through both `offsite_status` and `three_two_one_one_zero`.
    """
    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.inventory import BackupGroup
    from resurgam_api.models.sync import SyncJob

    customer_id, group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    group = db_session.get(BackupGroup, group_id)
    sync_job = SyncJob(
        pbs_instance_id=db_session.get(Datastore, group.datastore_id).pbs_instance_id,
        job_id="sync-1",
        remote_name="remote1",
        remote_store="offsite-store",
        local_store_name="store1",
        local_datastore_id=group.datastore_id,
        schedule="daily",
    )
    db_session.add(sync_job)
    db_session.commit()

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["offsite_status"]) == 1
    assert body["offsite_status"][0]["has_offsite_copy"] is True
    assert body["offsite_status"][0]["offsite_method"] == "sync_job"

    offsite_leg = next(leg for leg in body["three_two_one_one_zero"] if leg["leg"] == "offsite")
    assert offsite_leg["status"] == "met"


def test_report_offsite_leg_is_not_met_without_a_copy(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, _group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    body = response.json()
    assert body["offsite_status"][0]["has_offsite_copy"] is False
    offsite_leg = next(leg for leg in body["three_two_one_one_zero"] if leg["leg"] == "offsite")
    assert offsite_leg["status"] == "not_met"


def test_report_immutable_leg_is_not_attested_until_recorded(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, _group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    immutable_leg = next(leg for leg in response.json()["three_two_one_one_zero"] if leg["leg"] == "immutable")
    assert immutable_leg["status"] == "not_attested"
    assert immutable_leg["attested_by"] is None


def test_report_immutable_leg_reflects_a_recorded_attestation(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.inventory import BackupGroup

    customer_id, group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    group = db_session.get(BackupGroup, group_id)
    attest = client.post(
        f"/api/v1/datastores/{group.datastore_id}/compliance-attestations",
        json={"dimension": "immutable_copy", "value": True},
        headers=platform_admin_headers,
    )
    assert attest.status_code == 200

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    immutable_leg = next(leg for leg in response.json()["three_two_one_one_zero"] if leg["leg"] == "immutable")
    assert immutable_leg["status"] == "met"
    assert immutable_leg["attested_by"]
    assert immutable_leg["attested_at"] is not None


def test_report_includes_access_control_for_customer_scoped_and_global_roles(
    client, platform_admin_headers, db_session, test_settings
):
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    customer_id = _create_customer(db_session)
    scoped_user = User(email="scoped@example.com", display_name="Scoped Admin")
    db_session.add(scoped_user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.CUSTOMER_ADMIN.value).one()
    db_session.add(
        UserRoleAssignment(
            user_id=scoped_user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id
        )
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    access_control = response.json()["access_control"]
    scopes = {(e["user_display_name"], e["scope"]) for e in access_control}
    assert ("Scoped Admin", "customer") in scopes
    # The platform_admin fixture's own global assignment must also show up.
    assert any(scope == "global" for _name, scope in scopes)


def test_report_includes_restore_test_evidence_from_a_real_recovery_lab_run(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Recovery Lab (ADR 0019) is real now -- a `RestoreTestPlan` created
    through the actual `/lab-runs` endpoint must show up as restore-test
    evidence, feed the Management Summary's pass rate, and lift the
    workload's Protection Level tier once its member passes.
    """
    from resurgam_api.connectivity.models import VmidAvailabilityResult
    from resurgam_api.models.restore_test import RestoreTestPlanMember, RestoreTestPlanMemberStatus

    customer_id = _create_customer(db_session)
    instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, instance_id, monkeypatch)
    group_id = _create_group(db_session, datastore_id, customer_id)
    _add_snapshot(db_session, group_id)

    cluster_response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Lab PVE",
            "host": "pve1.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "is_lab_compute": True,
            "lab_appliance_template_vmid": 9000,
        },
        headers=platform_admin_headers,
    )
    assert cluster_response.status_code == 200
    cluster_id = cluster_response.json()["id"]

    app_group_response = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "AppGroup",
            "members": [{"backup_group_id": str(group_id), "startup_order": 0, "check_types": ["icmp"]}],
        },
        headers=platform_admin_headers,
    )
    assert app_group_response.status_code == 200
    app_group_id = app_group_response.json()["id"]

    from resurgam_api.api.v1 import restore_test_plans as module

    monkeypatch.setattr(
        module, "next_free_vmid", lambda *a, **kw: VmidAvailabilityResult(reachable=True, vmid=200, free=True)
    )
    monkeypatch.setattr(
        module,
        "check_vmid_free",
        lambda host, port, trust, token, candidate, timeout=5.0: VmidAvailabilityResult(
            reachable=True, vmid=candidate, free=True
        ),
    )

    run_response = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    )
    assert run_response.status_code == 200
    member_id = run_response.json()["members"][0]["id"]

    member = db_session.get(RestoreTestPlanMember, member_id)
    member.member_status = RestoreTestPlanMemberStatus.PASSED.value
    from datetime import datetime

    from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanStatus

    plan = db_session.get(RestoreTestPlan, run_response.json()["id"])
    plan.status = RestoreTestPlanStatus.PASSED.value
    plan.completed_at = datetime.now(UTC)
    db_session.commit()

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()

    assert len(body["restore_test_evidence"]) == 1
    assert body["restore_test_evidence"][0]["member_status"] == "passed"
    assert body["restore_test_evidence"][0]["application_group_name"] == "AppGroup"
    assert body["management_summary"]["restore_test_pass_rate"] == 1.0
    assert body["protection_level"]["tier"] == "backup_verified_lab_tested"


def test_report_includes_encryption_attestation(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.models.pbs import PbsInstance

    customer_id, group_id, _snapshot_id = _setup_customer_with_group(
        client, platform_admin_headers, db_session, monkeypatch
    )
    # Set a real fingerprint directly on the row -- going through the
    # create endpoint's "fingerprint" trust mode triggers a live TLS
    # preflight against the (fake) host, which this test isn't set up to
    # satisfy; the masking logic itself doesn't care how the value got there.
    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.inventory import BackupGroup

    group = db_session.get(BackupGroup, group_id)
    datastore = db_session.get(Datastore, group.datastore_id)
    instance = db_session.get(PbsInstance, datastore.pbs_instance_id)
    instance.tls_trust_mode = "fingerprint"
    instance.tls_fingerprint_sha256 = "aa" * 32  # 64 hex chars, matches the String(64) column
    db_session.commit()

    response = client.get(
        f"/api/v1/reporting/recovery-evidence?customer_id={customer_id}", headers=platform_admin_headers
    )
    assert response.status_code == 200
    instances = response.json()["encryption_attestation"]["instances"]
    assert len(instances) == 1
    assert instances[0]["tls_trust_mode"] == "fingerprint"
    assert instances[0]["tls_fingerprint_masked"] is not None
    assert instances[0]["tls_fingerprint_masked"] != instance.tls_fingerprint_sha256  # masked, not raw
