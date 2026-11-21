"""Recovery Evidence compliance reporting -- aggregation functions, one
per report section, mirroring `inventory/billing.py`/`inventory/usage.py`'s
existing pattern: computed fresh per request, nothing stored, small pure(ish)
functions taking `db: Session` plus scope arguments.

Two report sections were originally proposed differently and reframed onto
what's actually real in this codebase (confirmed via direct research, not
assumption): "Retention Adherence" doesn't exist as a concept here at all --
RESURGAM never syncs or stores PBS's own prune/retention policy -- so this
reports **Protection Policy Adherence** instead (real data: `ProtectionPolicy`
+ `evaluate_protection_status`). "Incidents & Exceptions" as alarm history
also doesn't exist -- `compute_alarms()` is entirely computed-on-read, never
persisted -- so this reports **Failed Jobs & Tasks** instead (real historical
data: `FileIndexJob`/`FileRestoreJob`/`TaskRun` failure rows).
"""

import uuid
from datetime import UTC, date, datetime, time

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from resurgam_api.compliance import latest_attestation, leg_applicability
from resurgam_api.inventory.protection import evaluate_protection_status
from resurgam_api.inventory.workload_label import workload_label as _workload_label
from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment, DatastoreRole
from resurgam_api.models.file_index_job import FileIndexJob
from resurgam_api.models.filerestore_job import FileRestoreJob
from resurgam_api.models.identity import Role, User, UserRoleAssignment
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, PbsAclEntry, ProtectionPolicy, TaskRun
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.restore import RestorePlan, RestorePlanApprovalStatus, RestorePlanExecutionStatus
from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanMember, RestoreTestPlanMemberStatus
from resurgam_api.models.sync import SyncJob
from resurgam_api.models.tenancy import Customer
from resurgam_api.reporting.charts import render_kpi_bars_svg, render_protection_level_distribution_svg
from resurgam_api.reporting.timeline_svg import render_period_timeline_svg
from resurgam_api.schemas.reporting import (
    AccessControlEntryOut,
    AuditTrailEntryOut,
    BackupHistoryEntryOut,
    EncryptionAttestationOut,
    FailedJobEntryOut,
    ManagementSummaryOut,
    OffsiteStatusEntryOut,
    PbsDatastoreAccessEntryOut,
    PbsInstanceAttestationOut,
    PolicyAdherenceEntryOut,
    ProtectionLevelOut,
    ProtectionLevelWorkloadOut,
    RecoveryEvidenceReportOut,
    RestoreTestEvidenceEntryOut,
    ThreeTwoOneOneZeroOut,
    VerificationResultsOut,
    VerificationSummaryOut,
)

_TIER_ORDER = ["backup_only", "backup_verified", "backup_verified_restore_tested", "backup_verified_lab_tested"]
_TIER_LABELS = {
    "backup_only": "Backup only",
    "backup_verified": "Backup, Verified",
    "backup_verified_restore_tested": "Backup, Verified, Restore-Tested",
    "backup_verified_lab_tested": "Backup, Verified, Lab-Tested Restore",
}


def _summarize_workload_list(labels: list[str], limit: int = 8) -> str:
    """A customer with dozens of unprotected workloads shouldn't turn a
    single 3-2-1-1-0 leg's `detail` into an unreadable wall of text --
    caps the enumeration and names how many more there are instead of
    listing every one.
    """
    if len(labels) <= limit:
        return ", ".join(labels)
    shown = ", ".join(labels[:limit])
    return f"{shown}, and {len(labels) - limit} more"


def _customer_groups(db: Session, customer_id: uuid.UUID, *, include_superseded: bool = False) -> list[BackupGroup]:
    """Every `BackupGroup` whose *effective* customer is this one -- own
    override, else datastore assignment. Same resolution
    `compute_customer_billing` uses, not reimplemented differently here --
    filtered directly in SQL rather than loading every group and every
    datastore assignment platform-wide into Python just to check one
    customer's own footprint.

    `include_superseded=False` (the default) excludes a reused VMID's
    closed-out prior generation (`BackupGroup.superseded_at` set) --
    correct for every *current-state* section (policy adherence,
    protection level, offsite status, ...). `include_superseded=True` is
    for `compute_backup_history` alone: a superseded generation's
    in-range snapshots are real history and must still appear, or backup
    coverage would look like it vanished for that period.
    """
    own_override = db.query(BackupGroup).filter(BackupGroup.customer_id == customer_id)
    via_datastore = (
        db.query(BackupGroup)
        .join(DatastoreCustomerAssignment, DatastoreCustomerAssignment.datastore_id == BackupGroup.datastore_id)
        .filter(BackupGroup.customer_id.is_(None), DatastoreCustomerAssignment.customer_id == customer_id)
    )
    if not include_superseded:
        own_override = own_override.filter(BackupGroup.superseded_at.is_(None))
        via_datastore = via_datastore.filter(BackupGroup.superseded_at.is_(None))
    return own_override.all() + via_datastore.all()


def _date_bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    start = datetime.combine(date_from, time.min, tzinfo=UTC)
    end = datetime.combine(date_to, time.max, tzinfo=UTC)
    return start, end


def compute_backup_history(
    db: Session, groups: list[BackupGroup], date_from: date, date_to: date
) -> list[BackupHistoryEntryOut]:
    if not groups:
        return []
    start, end = _date_bounds(date_from, date_to)
    group_by_id = {g.id: g for g in groups}
    snapshots = (
        db.query(BackupSnapshot)
        .filter(
            BackupSnapshot.backup_group_id.in_(group_by_id.keys()),
            BackupSnapshot.snapshot_time >= start,
            BackupSnapshot.snapshot_time <= end,
        )
        .order_by(BackupSnapshot.snapshot_time.desc())
        .all()
    )
    return [
        BackupHistoryEntryOut(
            backup_group_id=s.backup_group_id,
            workload_label=_workload_label(group_by_id[s.backup_group_id]),
            backup_type=group_by_id[s.backup_group_id].backup_type,
            namespace=group_by_id[s.backup_group_id].namespace,
            snapshot_time=s.snapshot_time,
            size_bytes=s.size_bytes,
            verify_status=s.verify_status,
        )
        for s in snapshots
    ]


def compute_policy_adherence(db: Session, groups: list[BackupGroup]) -> list[PolicyAdherenceEntryOut]:
    if not groups:
        return []
    policy_ids = {g.protection_policy_id for g in groups if g.protection_policy_id is not None}
    policies = (
        {p.id: p for p in db.query(ProtectionPolicy).filter(ProtectionPolicy.id.in_(policy_ids)).all()}
        if policy_ids
        else {}
    )
    # One grouped query for snapshot counts, not N+1 per group.
    counts = dict(
        db.query(BackupSnapshot.backup_group_id, func.count(BackupSnapshot.id))
        .filter(BackupSnapshot.backup_group_id.in_([g.id for g in groups]))
        .group_by(BackupSnapshot.backup_group_id)
        .all()
    )
    entries = []
    for group in groups:
        policy = policies.get(group.protection_policy_id) if group.protection_policy_id else None
        status = evaluate_protection_status(policy, group, counts.get(group.id, 0))
        entries.append(
            PolicyAdherenceEntryOut(
                backup_group_id=group.id,
                workload_label=_workload_label(group),
                policy_name=policy.name if policy else None,
                status=status.value,
                last_backup_at=group.last_backup_at,
                last_verify_status=group.last_verify_status,
            )
        )
    return entries


def compute_verification_results(db: Session, backup_history: list[BackupHistoryEntryOut]) -> VerificationResultsOut:
    """Re-projects the same snapshot rows `compute_backup_history` already
    fetched (passed in, not re-queried) -- one summary count block plus
    the same detail rows, filterable client-side by `verify_status`.
    """
    summary = VerificationSummaryOut(ok=0, failed=0, none=0, unknown=0)
    for entry in backup_history:
        if entry.verify_status == "ok":
            summary.ok += 1
        elif entry.verify_status == "failed":
            summary.failed += 1
        elif entry.verify_status == "none":
            summary.none += 1
        else:
            summary.unknown += 1
    return VerificationResultsOut(summary=summary, entries=backup_history)


def compute_failed_jobs(
    db: Session, customer_id: uuid.UUID, groups: list[BackupGroup], date_from: date, date_to: date
) -> list[FailedJobEntryOut]:
    start, end = _date_bounds(date_from, date_to)
    entries: list[FailedJobEntryOut] = []

    index_jobs = (
        db.query(FileIndexJob)
        .filter(
            FileIndexJob.customer_id == customer_id,
            FileIndexJob.status.in_(["failed", "cancelled"]),
            FileIndexJob.created_at >= start,
            FileIndexJob.created_at <= end,
        )
        .all()
    )
    entries.extend(
        FailedJobEntryOut(
            source="file_index_job",
            id=j.id,
            label=f"File index ({j.trigger})",
            status=j.status,
            occurred_at=j.completed_at or j.created_at,
            error_message=j.error_message,
        )
        for j in index_jobs
    )

    restore_jobs = (
        db.query(FileRestoreJob)
        .filter(
            FileRestoreJob.customer_id == customer_id,
            FileRestoreJob.status.in_(["failed", "expired"]),
            FileRestoreJob.created_at >= start,
            FileRestoreJob.created_at <= end,
        )
        .all()
    )
    entries.extend(
        FailedJobEntryOut(
            source="file_restore_job",
            id=j.id,
            label=f"File restore ({j.job_type})",
            status=j.status,
            occurred_at=j.completed_at or j.created_at,
            error_message=j.error_message,
        )
        for j in restore_jobs
    )

    # PBS task failures, scoped to this customer's own datastore(s) --
    # PBS's own worker-id naming convention always leads with the
    # datastore name (`<datastore>` alone for garbage-collection tasks,
    # `<datastore>:...` for everything else -- confirmed live,
    # 2026-09-20, across every task type this codebase parses: backup,
    # verify, prune, garbage_collection). Originally this queried "any
    # error task on the PBS instance hosting this customer's data" with a
    # disclosed per-row "instance-level only" caveat, since a shared
    # instance's task list can include another tenant's failures --
    # raised directly by the user, and closed by matching each task's
    # worker-id prefix against the specific datastore name(s) this
    # customer actually owns, scoped per PBS instance (a datastore name
    # is only unique *within* one instance) so a same-named datastore on
    # a different instance can never cross-match.
    datastores_by_instance: dict[uuid.UUID, set[str]] = {}
    for d in db.query(Datastore).filter(Datastore.id.in_({g.datastore_id for g in groups})).all():
        datastores_by_instance.setdefault(d.pbs_instance_id, set()).add(d.name)

    if datastores_by_instance:
        worker_prefix = func.split_part(TaskRun.worker_id, ":", 1)
        scope_conditions = [
            and_(TaskRun.pbs_instance_id == instance_id, worker_prefix.in_(names))
            for instance_id, names in datastores_by_instance.items()
        ]
        tasks = (
            db.query(TaskRun)
            .filter(
                TaskRun.status == "error",
                TaskRun.started_at >= start,
                TaskRun.started_at <= end,
                or_(*scope_conditions),
            )
            .all()
        )
        entries.extend(
            FailedJobEntryOut(
                source="pbs_task",
                id=t.id,
                label=f"PBS task ({t.task_type})",
                status=t.status,
                occurred_at=t.ended_at or t.started_at,
            )
            for t in tasks
        )

    entries.sort(key=lambda e: e.occurred_at, reverse=True)
    return entries


def compute_audit_trail(
    db: Session, customer_id: uuid.UUID, date_from: date, date_to: date
) -> list[AuditTrailEntryOut]:
    start, end = _date_bounds(date_from, date_to)
    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.customer_id == customer_id, AuditEvent.occurred_at >= start, AuditEvent.occurred_at <= end)
        .order_by(AuditEvent.occurred_at.desc())
        .all()
    )
    return [AuditTrailEntryOut.model_validate(e) for e in events]


def _mask_fingerprint(fingerprint: str | None) -> str | None:
    if fingerprint is None:
        return None
    stripped = fingerprint.replace(":", "")
    if len(stripped) <= 24:
        return fingerprint
    return f"{stripped[:12]}...{stripped[-12:]}"


def compute_encryption_attestation(db: Session, groups: list[BackupGroup]) -> EncryptionAttestationOut:
    datastore_ids = {g.datastore_id for g in groups}
    if not datastore_ids:
        return EncryptionAttestationOut(instances=[])
    instance_ids = {row[0] for row in db.query(Datastore.pbs_instance_id).filter(Datastore.id.in_(datastore_ids)).all()}
    instances = db.query(PbsInstance).filter(PbsInstance.id.in_(instance_ids)).all()
    return EncryptionAttestationOut(
        instances=[
            PbsInstanceAttestationOut(
                pbs_instance_id=i.id,
                display_name=i.display_name,
                tls_trust_mode=i.tls_trust_mode,
                tls_fingerprint_masked=_mask_fingerprint(i.tls_fingerprint_sha256),
            )
            for i in instances
        ]
    )


def compute_restore_test_evidence(
    db: Session, customer_id: uuid.UUID, date_from: date, date_to: date
) -> list[RestoreTestEvidenceEntryOut]:
    """Recovery Lab (ADR 0019) is real now, not a stub -- `RestoreTestPlan`
    has `customer_id` directly (snapshotted at run-creation time, same
    reasoning as `RestorePlan.customer_id`), unlike a `BackupGroup`, whose
    effective customer needs resolving through its datastore assignment.
    Scoped by whichever of completed/requested/created is actually set,
    newest first.
    """
    start, end = _date_bounds(date_from, date_to)
    effective_time = func.coalesce(
        RestoreTestPlan.completed_at, RestoreTestPlan.requested_at, RestoreTestPlan.created_at
    )
    rows = (
        db.query(RestoreTestPlan, RestoreTestPlanMember, ApplicationGroup, BackupGroup)
        .join(RestoreTestPlanMember, RestoreTestPlanMember.restore_test_plan_id == RestoreTestPlan.id)
        .join(ApplicationGroupMember, ApplicationGroupMember.id == RestoreTestPlanMember.application_group_member_id)
        .join(ApplicationGroup, ApplicationGroup.id == RestoreTestPlan.application_group_id)
        .join(BackupGroup, BackupGroup.id == ApplicationGroupMember.backup_group_id)
        .filter(RestoreTestPlan.customer_id == customer_id, effective_time >= start, effective_time <= end)
        .order_by(effective_time.desc())
        .all()
    )
    return [
        RestoreTestEvidenceEntryOut(
            application_group_name=group.name,
            backup_group_id=backup_group.id,
            workload_label=_workload_label(backup_group),
            run_status=plan.status,
            member_status=member.member_status,
            tested_at=plan.completed_at or plan.requested_at,
        )
        for plan, member, group, backup_group in rows
    ]


def compute_access_control(db: Session, customer_id: uuid.UUID) -> list[AccessControlEntryOut]:
    """Who can touch this customer's data: their own customer-scoped role
    assignments plus every *global* assignment (a global admin can act on
    any customer, so that's part of the honest answer too) -- manifest
    ISO27001 access-review intent, not just customer-scoped rows.
    """
    rows = (
        db.query(UserRoleAssignment, Role, User)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .join(User, User.id == UserRoleAssignment.user_id)
        .filter(
            (UserRoleAssignment.scope_type == "global")
            | ((UserRoleAssignment.scope_type == "customer") & (UserRoleAssignment.scope_id == customer_id))
        )
        .order_by(UserRoleAssignment.created_at.desc())
        .all()
    )
    return [
        AccessControlEntryOut(
            user_display_name=user.display_name,
            role=role.key,
            scope="global" if assignment.scope_type == "global" else "customer",
            granted_at=assignment.created_at,
        )
        for assignment, role, user in rows
    ]


def _acl_entry_applies_to_datastore(entry: PbsAclEntry, datastore_name: str) -> bool:
    """PBS's own ACL propagation semantics: `propagate` governs whether a
    grant applies to paths *beneath* `entry.path`, not to `entry.path`
    itself. So a grant is relevant to this datastore if it's an exact
    match on the datastore's own path (real regardless of propagate --
    e.g. `Datastore.Audit` set directly on the datastore), an ancestor
    path (`/`, `/datastore`) with `propagate` set, or a more specific
    path already inside this datastore -- a namespace-scoped grant, real
    access to a slice of this customer's data regardless of whether it
    itself propagates further down.
    """
    target = f"/datastore/{datastore_name}"
    if entry.path == target or entry.path.startswith(target + "/"):
        return True
    ancestor_prefix = entry.path.rstrip("/") + "/"
    if target.startswith(ancestor_prefix):
        return entry.propagate
    return False


def compute_pbs_native_access(db: Session, groups: list[BackupGroup]) -> list[PbsDatastoreAccessEntryOut]:
    """PBS-native ACL grants relevant to this customer's own datastores
    -- raised directly by the user after `compute_access_control` above
    was shown to only ever list RESURGAM's own role assignments,
    completely missing access granted natively in PBS (a real PBS user
    with `Admin` on a customer's whole datastore, invisible to
    RESURGAM). Reads from `PbsAclEntry`, already synced by
    `collector.sync.sync_instance_acl` -- no live PBS call here, matching
    every other section of this report.
    """
    datastores = db.query(Datastore).filter(Datastore.id.in_({g.datastore_id for g in groups})).all()
    if not datastores:
        return []

    instance_ids = {d.pbs_instance_id for d in datastores}
    entries_by_instance: dict[uuid.UUID, list[PbsAclEntry]] = {}
    for instance_id in instance_ids:
        entries_by_instance[instance_id] = db.query(PbsAclEntry).filter_by(pbs_instance_id=instance_id).all()

    results: list[PbsDatastoreAccessEntryOut] = []
    for datastore in datastores:
        for entry in entries_by_instance.get(datastore.pbs_instance_id, []):
            if _acl_entry_applies_to_datastore(entry, datastore.name):
                results.append(
                    PbsDatastoreAccessEntryOut(
                        datastore_name=datastore.name,
                        path=entry.path,
                        role=entry.role,
                        subject=entry.subject,
                        subject_type=entry.subject_type,
                    )
                )
    return results


def _count_unresolved_failed_jobs(failed_jobs: list[FailedJobEntryOut]) -> int:
    """This codebase doesn't track job resolution explicitly (no "fixed"
    flag), and `failed_jobs` by construction only ever contains failures
    -- there's no success row here to prove a later fix. So "unresolved"
    is counted as: the number of distinct (source, label) identities that
    failed in this period, each counted once regardless of how many times
    it recurred. A recurring failure of the same job is one open issue,
    not N; this function has nothing in `failed_jobs` that could ever
    show it was resolved, so every distinct identity here is, by
    definition, unresolved as far as this report can tell.
    """
    return len({(entry.source, entry.label) for entry in failed_jobs})


def compute_management_summary(
    date_from: date,
    date_to: date,
    backup_history: list[BackupHistoryEntryOut],
    policy_adherence: list[PolicyAdherenceEntryOut],
    verification_results: VerificationResultsOut,
    failed_jobs: list[FailedJobEntryOut],
    restore_test_evidence: list[RestoreTestEvidenceEntryOut],
    restore_plans_in_period: list[RestorePlan],
    has_application_groups: bool,
) -> ManagementSummaryOut:
    """Pure -- takes already-computed section data as input, reusing the
    same rows every other section already fetched rather than re-querying
    (matches `compute_verification_results`'s existing "reuse, don't
    re-query" pattern). Traffic-light rule (adjustable here, not buried
    in the template):

    - RED: any `policy_adherence` entry is critical, OR any
      `restore_test_evidence` entry's member_status is "failed", OR there
      is at least one unresolved failed job/task in the period.
    - AMBER: any `policy_adherence` entry is a warning, OR at least one
      verification failed, OR no restore test ran at all in the period
      for a customer that has at least one `ApplicationGroup` defined
      (untested recoverability is itself a finding).
    - GREEN: none of the above.
    """
    backup_success_rate = (
        sum(1 for e in backup_history if e.verify_status != "failed") / len(backup_history) if backup_history else 0.0
    )
    verify_total = verification_results.summary.ok + verification_results.summary.failed
    verification_pass_rate = verification_results.summary.ok / verify_total if verify_total else 0.0
    policy_adherence_rate = (
        sum(1 for e in policy_adherence if e.status == "ok") / len(policy_adherence) if policy_adherence else 0.0
    )
    restore_test_pass_rate = (
        sum(1 for e in restore_test_evidence if e.member_status == RestoreTestPlanMemberStatus.PASSED.value)
        / len(restore_test_evidence)
        if restore_test_evidence
        else None
    )
    approval_applicable = [p for p in restore_plans_in_period if p.approval_status is not None]
    four_eyes_approval_rate = (
        sum(1 for p in approval_applicable if p.approval_status == RestorePlanApprovalStatus.APPROVED.value)
        / len(approval_applicable)
        if approval_applicable
        else None
    )

    unresolved_failed_jobs = _count_unresolved_failed_jobs(failed_jobs)

    is_red = (
        any(e.status == "critical" for e in policy_adherence)
        or any(e.member_status == RestoreTestPlanMemberStatus.FAILED.value for e in restore_test_evidence)
        or unresolved_failed_jobs > 0
    )
    is_amber = (
        any(e.status == "warning" for e in policy_adherence)
        or verification_results.summary.failed > 0
        or (not restore_test_evidence and has_application_groups)
    )
    traffic_light = "red" if is_red else "amber" if is_amber else "green"

    kpi_bars = [
        ("Backup success", backup_success_rate),
        ("Verification pass", verification_pass_rate),
        *([("Restore-test pass", restore_test_pass_rate)] if restore_test_pass_rate is not None else []),
        ("Policy adherence", policy_adherence_rate),
    ]

    return ManagementSummaryOut(
        traffic_light=traffic_light,
        backup_success_rate=backup_success_rate,
        verification_pass_rate=verification_pass_rate,
        restore_test_pass_rate=restore_test_pass_rate,
        policy_adherence_rate=policy_adherence_rate,
        unresolved_failed_jobs=unresolved_failed_jobs,
        four_eyes_approval_rate=four_eyes_approval_rate,
        period_timeline_svg=render_period_timeline_svg(date_from, date_to, backup_history, restore_test_evidence),
        kpi_bars_svg=render_kpi_bars_svg(kpi_bars),
    )


def compute_protection_level(
    groups: list[BackupGroup],
    backup_history: list[BackupHistoryEntryOut],
    restore_plans_in_period: list[RestorePlan],
    restore_test_evidence: list[RestoreTestEvidenceEntryOut],
) -> ProtectionLevelOut:
    """Per-workload tier, honest floor: has a recent snapshot in this
    period (tier 0) -> its most recent snapshot verified ok (tier 1) -> a
    completed `RestorePlan` execution against this workload exists in the
    period (tier 2) -> a passed Recovery Lab member run exists in the
    period (tier 3). The customer-level `tier` is the MINIMUM across
    workloads, not an average or best-case -- one untested workload among
    ten well-tested ones makes the customer "backup_only" overall. A
    workload with zero snapshots in the period contributes nothing to
    report (there's no tier to assign); if that leaves no workloads at
    all, the customer-level tier defaults to the safe floor
    ("backup_only") rather than being left undefined.
    """
    latest_verify_by_group: dict[uuid.UUID, str] = {}
    for entry in backup_history:  # newest-first already
        latest_verify_by_group.setdefault(entry.backup_group_id, entry.verify_status)

    restore_tested_group_ids = {
        p.backup_group_id
        for p in restore_plans_in_period
        if p.execution_status == RestorePlanExecutionStatus.COMPLETED.value
    }
    lab_tested_group_ids = {
        e.backup_group_id for e in restore_test_evidence if e.member_status == RestoreTestPlanMemberStatus.PASSED.value
    }

    workloads: list[ProtectionLevelWorkloadOut] = []
    for group in groups:
        if group.id not in latest_verify_by_group:
            continue
        if group.id in lab_tested_group_ids:
            tier = "backup_verified_lab_tested"
        elif group.id in restore_tested_group_ids:
            tier = "backup_verified_restore_tested"
        elif latest_verify_by_group[group.id] == "ok":
            tier = "backup_verified"
        else:
            tier = "backup_only"
        workloads.append(ProtectionLevelWorkloadOut(workload_label=_workload_label(group), tier=tier))

    overall_tier = min(workloads, key=lambda w: _TIER_ORDER.index(w.tier)).tier if workloads else "backup_only"
    return ProtectionLevelOut(
        tier=overall_tier,
        label=_TIER_LABELS[overall_tier],
        workloads=workloads,
        tier_distribution_svg=render_protection_level_distribution_svg(workloads),
    )


def compute_offsite_status(db: Session, groups: list[BackupGroup]) -> list[OffsiteStatusEntryOut]:
    if not groups:
        return []
    datastore_ids = {g.datastore_id for g in groups}
    assignments = {
        a.datastore_id: a
        for a in db.query(DatastoreCustomerAssignment)
        .filter(DatastoreCustomerAssignment.datastore_id.in_(datastore_ids))
        .all()
    }
    sync_job_by_datastore = {}
    for job in db.query(SyncJob).filter(SyncJob.local_datastore_id.in_(datastore_ids)).all():
        sync_job_by_datastore.setdefault(job.local_datastore_id, job)

    entries: list[OffsiteStatusEntryOut] = []
    for group in groups:
        assignment = assignments.get(group.datastore_id)
        if assignment is not None and assignment.role == DatastoreRole.OFFSITE.value:
            entries.append(
                OffsiteStatusEntryOut(
                    backup_group_id=group.id,
                    workload_label=_workload_label(group),
                    has_offsite_copy=True,
                    offsite_method="datastore_role",
                    sync_schedule=None,
                    last_synced_at=None,
                )
            )
            continue
        sync_job = sync_job_by_datastore.get(group.datastore_id)
        if sync_job is not None:
            entries.append(
                OffsiteStatusEntryOut(
                    backup_group_id=group.id,
                    workload_label=_workload_label(group),
                    has_offsite_copy=True,
                    offsite_method="sync_job",
                    sync_schedule=sync_job.schedule,
                    last_synced_at=sync_job.last_synced_at,
                )
            )
            continue
        entries.append(
            OffsiteStatusEntryOut(
                backup_group_id=group.id,
                workload_label=_workload_label(group),
                has_offsite_copy=False,
                offsite_method="none",
                sync_schedule=None,
                last_synced_at=None,
            )
        )
    return entries


def compute_three_two_one_one_zero(
    db: Session,
    customer_id: uuid.UUID,
    groups: list[BackupGroup],
    offsite_status: list[OffsiteStatusEntryOut],
    failed_jobs: list[FailedJobEntryOut],
) -> list[ThreeTwoOneOneZeroOut]:
    """One entry per 3-2-1-1-0 leg. "offsite" and "zero_errors" are
    computed straight from real, already-observed data. "copies",
    "media_types", and "immutable" are things RESURGAM cannot observe on
    its own -- these resolve through `latest_attestation`: "not_attested"
    if nobody has ever recorded one, otherwise "met"/"not_met" from the
    latest attestation's value, with `attested_by`/`attested_at` so the
    report shows exactly who vouched for it and when. For "immutable",
    every datastore backing this customer's workloads must be attested
    immutable for the leg to read "met" -- one un-attested or
    non-immutable datastore is the honest limiting case, never silently
    dropped. A leg the operator has marked not applicable for this
    customer (`leg_applicability`, e.g. a deliberately single-site
    customer's "offsite" leg) always reads "not_applicable" regardless of
    what the underlying data would otherwise show -- applied as a final
    override below rather than skipping each leg's own computation, so
    every leg still gets a real, correctly-typed entry either way.
    """
    applicability = leg_applicability(db, customer_id)
    legs: list[ThreeTwoOneOneZeroOut] = []

    if not offsite_status:
        legs.append(
            ThreeTwoOneOneZeroOut(
                leg="offsite",
                status="not_met",
                detail="No protected workloads in this period.",
                attested_by=None,
                attested_at=None,
            )
        )
    elif all(e.has_offsite_copy for e in offsite_status):
        legs.append(
            ThreeTwoOneOneZeroOut(
                leg="offsite",
                status="met",
                detail="Every workload has an offsite copy.",
                attested_by=None,
                attested_at=None,
            )
        )
    else:
        missing = _summarize_workload_list([e.workload_label for e in offsite_status if not e.has_offsite_copy])
        legs.append(
            ThreeTwoOneOneZeroOut(
                leg="offsite",
                status="not_met",
                detail=f"No offsite copy for: {missing}.",
                attested_by=None,
                attested_at=None,
            )
        )

    if failed_jobs:
        legs.append(
            ThreeTwoOneOneZeroOut(
                leg="zero_errors",
                status="not_met",
                detail=f"{len(failed_jobs)} failed job(s)/task(s) recorded in this period.",
                attested_by=None,
                attested_at=None,
            )
        )
    else:
        legs.append(
            ThreeTwoOneOneZeroOut(
                leg="zero_errors",
                status="met",
                detail="No failed jobs or tasks recorded in this period.",
                attested_by=None,
                attested_at=None,
            )
        )

    datastore_ids = {g.datastore_id for g in groups}
    if not datastore_ids:
        legs.append(
            ThreeTwoOneOneZeroOut(
                leg="immutable",
                status="not_attested",
                detail="No protected workloads in this period.",
                attested_by=None,
                attested_at=None,
            )
        )
    else:
        attestations = {d_id: latest_attestation(db, "datastore", d_id, "immutable_copy") for d_id in datastore_ids}
        if any(a is None for a in attestations.values()):
            legs.append(
                ThreeTwoOneOneZeroOut(
                    leg="immutable",
                    status="not_attested",
                    detail="At least one backing datastore has not been attested for immutability yet -- record one on the Datastores page.",
                    attested_by=None,
                    attested_at=None,
                )
            )
        else:
            newest = max(attestations.values(), key=lambda a: a.created_at)
            attester = db.get(User, newest.asserted_by_user_id)
            attester_name = attester.display_name if attester else None
            if all(a.value is True for a in attestations.values()):
                legs.append(
                    ThreeTwoOneOneZeroOut(
                        leg="immutable",
                        status="met",
                        detail="All backing datastores attested immutable.",
                        attested_by=attester_name,
                        attested_at=newest.created_at,
                    )
                )
            else:
                not_immutable = sum(1 for a in attestations.values() if a.value is not True)
                legs.append(
                    ThreeTwoOneOneZeroOut(
                        leg="immutable",
                        status="not_met",
                        detail=f"{not_immutable} backing datastore(s) attested as not immutable.",
                        attested_by=attester_name,
                        attested_at=newest.created_at,
                    )
                )

    for dimension, leg in (("production_copy_count", "copies"), ("media_type_diversity", "media_types")):
        attestation = latest_attestation(db, "customer", customer_id, dimension)
        if attestation is None:
            legs.append(
                ThreeTwoOneOneZeroOut(
                    leg=leg,
                    status="not_attested",
                    detail="RESURGAM cannot observe this directly -- record a self-assessment on the customer's detail page.",
                    attested_by=None,
                    attested_at=None,
                )
            )
            continue
        attester = db.get(User, attestation.asserted_by_user_id)
        attester_name = attester.display_name if attester else None
        if leg == "copies":
            met = isinstance(attestation.value, int) and attestation.value >= 3
            detail = f"{attestation.value} production copies attested."
        else:
            met = isinstance(attestation.value, list) and len(set(attestation.value)) >= 2
            detail = (
                f"Media types attested: {', '.join(attestation.value)}."
                if isinstance(attestation.value, list)
                else "Media types attested."
            )
        legs.append(
            ThreeTwoOneOneZeroOut(
                leg=leg,
                status="met" if met else "not_met",
                detail=detail,
                attested_by=attester_name,
                attested_at=attestation.created_at,
            )
        )

    return [
        ThreeTwoOneOneZeroOut(
            leg=entry.leg,
            status="not_applicable",
            detail="Marked not applicable for this customer.",
            attested_by=None,
            attested_at=None,
        )
        if applicability.get(entry.leg) is False
        else entry
        for entry in legs
    ]


def compute_recovery_evidence_report(
    db: Session, customer: Customer, date_from: date, date_to: date, generated_by_user_id: uuid.UUID | None
) -> RecoveryEvidenceReportOut:
    # `current_groups` excludes a reused VMID's closed-out prior
    # generation -- correct for every current-state section below.
    # `history_groups` includes it -- only `compute_backup_history` needs
    # this, so a superseded generation's in-range snapshots don't look
    # like backup coverage vanished for that period.
    current_groups = _customer_groups(db, customer.id)
    history_groups = _customer_groups(db, customer.id, include_superseded=True)
    backup_history = compute_backup_history(db, history_groups, date_from, date_to)
    policy_adherence = compute_policy_adherence(db, current_groups)
    verification_results = compute_verification_results(db, backup_history)
    failed_jobs = compute_failed_jobs(db, customer.id, current_groups, date_from, date_to)
    restore_test_evidence = compute_restore_test_evidence(db, customer.id, date_from, date_to)

    start, end = _date_bounds(date_from, date_to)
    restore_plans_in_period = (
        db.query(RestorePlan)
        .filter(RestorePlan.customer_id == customer.id, RestorePlan.created_at >= start, RestorePlan.created_at <= end)
        .all()
    )
    has_application_groups = (
        db.query(ApplicationGroup.id).filter(ApplicationGroup.customer_id == customer.id).first() is not None
    )
    offsite_status = compute_offsite_status(db, current_groups)

    return RecoveryEvidenceReportOut(
        customer_id=customer.id,
        customer_name=customer.name,
        customer_match_code=customer.match_code,
        date_from=date_from,
        date_to=date_to,
        generated_at=datetime.now(UTC),
        generated_by_user_id=generated_by_user_id,
        backup_history=backup_history,
        policy_adherence=policy_adherence,
        verification_results=verification_results,
        failed_jobs=failed_jobs,
        audit_trail=compute_audit_trail(db, customer.id, date_from, date_to),
        encryption_attestation=compute_encryption_attestation(db, current_groups),
        restore_test_evidence=restore_test_evidence,
        management_summary=compute_management_summary(
            date_from,
            date_to,
            backup_history,
            policy_adherence,
            verification_results,
            failed_jobs,
            restore_test_evidence,
            restore_plans_in_period,
            has_application_groups,
        ),
        access_control=compute_access_control(db, customer.id),
        pbs_datastore_access=compute_pbs_native_access(db, current_groups),
        protection_level=compute_protection_level(
            current_groups, backup_history, restore_plans_in_period, restore_test_evidence
        ),
        offsite_status=offsite_status,
        three_two_one_one_zero=compute_three_two_one_one_zero(
            db, customer.id, current_groups, offsite_status, failed_jobs
        ),
    )
