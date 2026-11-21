import uuid
from datetime import UTC, datetime, timedelta

from resurgam_api.models.application_group import ApplicationGroup
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanStatus
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.models.tenancy import Customer
from resurgam_api.worker.recovery_lab_scheduler import _active_plans, _due_groups, _run_due_group
from tests.test_restore_test_plans_api import _mock_pve_for_creation, _mock_pve_for_execution, _setup_run_prereqs


def _make_customer(db_session) -> Customer:
    n = db_session.query(Customer).count()
    customer = Customer(name=f"Cust{n}", slug=f"cust{n}", debitor_number=f"{1000 + n}", match_code=f"C{n}")
    db_session.add(customer)
    db_session.flush()
    return customer


def _make_cluster(db_session) -> PveCluster:
    ref = SecretReference(
        reference_key=f"test-lab-scheduler-token-{db_session.query(SecretReference).count()}",
        description="test",
        purpose=SecretPurpose.INFRA_CREDENTIAL,
    )
    db_session.add(ref)
    db_session.flush()
    cluster = PveCluster(
        display_name="Lab", host="pve.internal.example", tls_trust_mode="ca", api_token_secret_ref_id=ref.id
    )
    db_session.add(cluster)
    db_session.flush()
    return cluster


def _make_group(db_session, customer_id) -> ApplicationGroup:
    group = ApplicationGroup(customer_id=customer_id, name="App")
    db_session.add(group)
    db_session.flush()
    return group


def _make_plan(db_session, *, status: str) -> RestoreTestPlan:
    cluster = _make_cluster(db_session)
    customer = _make_customer(db_session)
    group = _make_group(db_session, customer_id=customer.id)
    plan = RestoreTestPlan(
        application_group_id=group.id,
        pve_cluster_id=cluster.id,
        vnet_name=f"lab{uuid.uuid4().hex[:6]}",
        status=status,
    )
    db_session.add(plan)
    db_session.flush()
    return plan


def test_active_plans_includes_running_and_provisioning(db_session):
    running = _make_plan(db_session, status=RestoreTestPlanStatus.RUNNING.value)
    provisioning = _make_plan(db_session, status=RestoreTestPlanStatus.PROVISIONING_APPLIANCE.value)
    ids = {p.id for p in _active_plans(db_session)}
    assert ids == {running.id, provisioning.id}


def test_active_plans_excludes_terminal_and_planned_statuses(db_session):
    for status in (
        RestoreTestPlanStatus.PLANNED.value,
        RestoreTestPlanStatus.PASSED.value,
        RestoreTestPlanStatus.PARTIAL.value,
        RestoreTestPlanStatus.FAILED.value,
        RestoreTestPlanStatus.CLEANING_UP.value,
        RestoreTestPlanStatus.COMPLETED.value,
    ):
        _make_plan(db_session, status=status)
    assert _active_plans(db_session) == []


def _schedule_group(db_session, *, enabled: bool, next_run_at) -> ApplicationGroup:
    customer = _make_customer(db_session)
    cluster = _make_cluster(db_session)
    group = ApplicationGroup(
        customer_id=customer.id,
        name="Scheduled App",
        lab_schedule_enabled=enabled,
        lab_schedule_interval_hours=6,
        lab_next_scheduled_run_at=next_run_at,
        lab_schedule_pve_cluster_id=cluster.id,
        lab_schedule_target_node="pve1",
        lab_schedule_target_storage="local-lvm",
    )
    db_session.add(group)
    db_session.flush()
    return group


def test_due_groups_includes_enabled_and_past_due(db_session):
    due = _schedule_group(db_session, enabled=True, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    assert [g.id for g in _due_groups(db_session)] == [due.id]


def test_due_groups_excludes_not_yet_due(db_session):
    _schedule_group(db_session, enabled=True, next_run_at=datetime.now(UTC) + timedelta(hours=1))
    assert _due_groups(db_session) == []


def test_due_groups_excludes_disabled(db_session):
    _schedule_group(db_session, enabled=False, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
    assert _due_groups(db_session) == []


def test_run_due_group_creates_and_executes_a_run_and_advances_next_run(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    _mock_pve_for_execution(monkeypatch)

    group = db_session.get(ApplicationGroup, uuid.UUID(app_group_id))
    group.lab_schedule_enabled = True
    group.lab_schedule_interval_hours = 6
    group.lab_schedule_pve_cluster_id = uuid.UUID(cluster_id)
    group.lab_schedule_target_node = "pve1"
    group.lab_schedule_target_storage = "local-lvm"
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    group.lab_next_scheduled_run_at = due_at
    db_session.commit()

    _run_due_group(db_session, test_settings, group)

    plans = db_session.query(RestoreTestPlan).filter_by(application_group_id=group.id).all()
    assert len(plans) == 1
    assert plans[0].status == RestoreTestPlanStatus.PROVISIONING_APPLIANCE.value
    assert plans[0].created_by_user_id is None

    db_session.refresh(group)
    assert group.lab_next_scheduled_run_at > due_at
    assert group.lab_next_scheduled_run_at <= datetime.now(UTC) + timedelta(hours=6, minutes=1)


def test_run_due_group_still_advances_next_run_on_failure(
    client, platform_admin_headers, db_session, monkeypatch, test_settings
):
    """A persistently broken cluster (unreachable PVE, missing template,
    etc.) must not be retried every tick forever -- `lab_next_scheduled_run_at`
    advances by a full interval even when the attempt itself fails,
    same reasoning `file_index_worker.py`'s own scheduled work already
    applies.
    """
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    # No PVE mocking at all -- next_free_vmid() will fail against a real
    # (nonexistent) host, causing create_plan_for_group() to raise.

    group = db_session.get(ApplicationGroup, uuid.UUID(app_group_id))
    group.lab_schedule_enabled = True
    group.lab_schedule_interval_hours = 6
    group.lab_schedule_pve_cluster_id = uuid.UUID(cluster_id)
    group.lab_schedule_target_node = "pve1"
    group.lab_schedule_target_storage = "local-lvm"
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    group.lab_next_scheduled_run_at = due_at
    db_session.commit()

    _run_due_group(db_session, test_settings, group)

    assert db_session.query(RestoreTestPlan).filter_by(application_group_id=group.id).count() == 0
    db_session.refresh(group)
    assert group.lab_next_scheduled_run_at > due_at
