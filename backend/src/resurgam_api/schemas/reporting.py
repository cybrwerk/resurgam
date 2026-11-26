from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FrameworkControlRef(BaseModel):
    framework: str
    control: str
    title: str


class BackupHistoryEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    backup_group_id: UUID
    workload_label: str
    backup_type: str
    namespace: str
    snapshot_time: datetime
    size_bytes: int | None
    verify_status: str


class PolicyAdherenceEntryOut(BaseModel):
    backup_group_id: UUID
    workload_label: str
    policy_name: str | None
    status: str  # ok | warning | critical | unknown, ProtectionStatus values
    last_backup_at: datetime | None
    last_verify_status: str


class VerificationSummaryOut(BaseModel):
    ok: int
    failed: int
    none: int
    unknown: int


class VerificationResultsOut(BaseModel):
    summary: VerificationSummaryOut
    entries: list[BackupHistoryEntryOut]


class FailedJobEntryOut(BaseModel):
    source: Literal["file_index_job", "file_restore_job", "pbs_task"] = Field(
        description="Which internal job type this failure came from."
    )
    id: UUID = Field(description="The underlying job/task's own id.")
    label: str = Field(
        description="Human-readable description of the failed job, e.g. 'File index (manual)' or 'PBS task (verify)'."
    )
    status: str = Field(description="The job/task's terminal status, e.g. 'failed', 'expired', 'cancelled', 'error'.")
    occurred_at: datetime = Field(description="When the job/task ended, or was created if it never completed.")
    error_message: str | None = Field(
        default=None,
        description="Error detail, when the source job records one. PBS tasks never set this (PBS's task log isn't parsed here).",
    )


class AuditTrailEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    occurred_at: datetime
    actor_user_id: UUID | None
    action: str
    target_type: str | None
    target_id: str | None


class PbsInstanceAttestationOut(BaseModel):
    pbs_instance_id: UUID
    display_name: str
    tls_trust_mode: str
    tls_fingerprint_masked: str | None


class EncryptionAttestationOut(BaseModel):
    instances: list[PbsInstanceAttestationOut]


class RestoreTestEvidenceEntryOut(BaseModel):
    """One Application Group member's outcome within one Recovery Lab run
    (ADR 0019) that fell inside the report's date range.
    """

    application_group_name: str = Field(description="Name of the Application Group this Recovery Lab run tested.")
    backup_group_id: UUID = Field(
        description="The specific workload (VM/CT) within the group that this entry reports on."
    )
    workload_label: str = Field(description="Human-readable identifier for the workload, e.g. 'web-01 (104)'.")
    run_status: str = Field(
        description="Aggregate status of the whole Recovery Lab run this member belongs to (RestoreTestPlan values: planned, running, passed, partial, failed, cleaning_up, completed)."
    )
    member_status: str = Field(
        description="This specific member's own outcome within the run (RestoreTestPlanMember values: pending, queued, mounting, booting, probing, passed, failed, ...)."
    )
    tested_at: datetime | None = Field(
        description="When this run completed, or was requested if not yet complete. Null if neither has happened yet."
    )


class AccessControlEntryOut(BaseModel):
    """One role assignment that grants some user access to this
    customer's data -- either scoped directly to this customer, or a
    platform-wide (global) assignment that applies to every customer.
    """

    user_display_name: str = Field(description="Display name of the user holding this role assignment.")
    role: str = Field(
        description="Role key granted (e.g. platform_admin, customer_admin, restore_operator, viewer, backup_operator, auditor)."
    )
    scope: Literal["customer", "global"] = Field(
        description="Whether this assignment is scoped to this customer specifically, or a global assignment that also happens to cover it."
    )
    granted_at: datetime = Field(description="When this role assignment was created.")


class PbsDatastoreAccessEntryOut(BaseModel):
    """One PBS-*native* ACL grant that applies to a datastore backing
    this customer's data -- access granted directly in PBS, entirely
    outside RESURGAM's own role assignments (`AccessControlEntryOut`
    above). Raised directly by the user: a report showing only
    RESURGAM's own roles while a PBS user holds real, direct `Admin` on
    the underlying datastore is misleading, not merely incomplete.
    Synced from PBS (`PbsAclEntry`), not fetched live at report time.
    """

    datastore_name: str = Field(description="Which of this customer's datastores this grant applies to.")
    path: str = Field(
        description="The PBS ACL path the grant is set at, e.g. '/', '/datastore/<name>', or a namespace beneath it."
    )
    role: str = Field(
        description="PBS's own role name (e.g. Admin, DatastoreAudit, DatastoreBackup) -- not a RESURGAM RoleKey."
    )
    subject: str = Field(
        description="The PBS user/token/group id this grant applies to, e.g. 'tenant-a@pbs' or 'root@pam!token-name'."
    )
    subject_type: str = Field(
        description="What kind of PBS principal `subject` identifies -- PBS's own value (e.g. 'user'), not a fixed vocabulary confirmed exhaustively."
    )


class ManagementSummaryOut(BaseModel):
    """A glance-level compliance summary for the report's date range.
    Traffic-light rule (see `compute_management_summary` for the exact
    logic): RED if any policy is critical, any restore test failed, or
    there's an unresolved failed job; AMBER if any policy is a warning,
    any verification failed, or no restore test ran despite the customer
    having at least one Application Group; GREEN otherwise.
    """

    traffic_light: Literal["green", "amber", "red"] = Field(
        description="Green = nothing to do. Amber = pay attention, fix it. Red = not okay, handle immediately."
    )
    backup_success_rate: float = Field(
        description="Fraction (0.0-1.0) of this period's snapshots whose verify_status was not 'failed'. 0.0 if there were no snapshots."
    )
    verification_pass_rate: float = Field(
        description="Fraction (0.0-1.0) of verified snapshots (ok+failed) that passed verification. 0.0 if none were verified either way."
    )
    restore_test_pass_rate: float | None = Field(
        description="Fraction (0.0-1.0) of this period's Recovery Lab member runs that passed. Null (not 0.0) when zero restore tests ran -- an empty set has no rate, and 0.0 would misreport it as 'ran and all failed'."
    )
    policy_adherence_rate: float = Field(
        description="Fraction (0.0-1.0) of protected workloads whose protection-policy status was 'ok'."
    )
    unresolved_failed_jobs: int = Field(
        description="Count of distinct failed job/task identities in this period that this report has no evidence were later fixed."
    )
    four_eyes_approval_rate: float | None = Field(
        description="Fraction (0.0-1.0) of restore plans requiring four-eyes approval that were approved (vs. rejected) in this period. Null if none required approval."
    )
    period_timeline_svg: str = Field(
        description=(
            "Raw <svg>...</svg> markup visualizing backup/verify/restore-test outcomes across the report period, "
            "server-rendered from the same aggregates as the rest of this summary. Safe to render unescaped -- it "
            "is built entirely server-side from numeric aggregates, never from customer-editable input."
        )
    )
    kpi_bars_svg: str = Field(
        description=(
            "Raw <svg>...</svg> markup: a horizontal bar chart of backup_success_rate/verification_pass_rate/"
            "restore_test_pass_rate/policy_adherence_rate, color-coded green/amber/red by value. Same safety "
            "note as period_timeline_svg -- built entirely from these same numeric fields, safe to render unescaped."
        )
    )


class ProtectionLevelWorkloadOut(BaseModel):
    workload_label: str = Field(description="Human-readable identifier for the workload, e.g. 'web-01 (104)'.")
    tier: Literal["backup_only", "backup_verified", "backup_verified_restore_tested", "backup_verified_lab_tested"] = (
        Field(
            description="This workload's own protection tier for the period, escalating: backed up -> also verified -> also restore-tested via a guided RestorePlan -> also passed a Recovery Lab run."
        )
    )


class ProtectionLevelOut(BaseModel):
    """Customer-level Protection Scope & Level, derived from the
    per-workload breakdown in `workloads`.
    """

    tier: Literal["backup_only", "backup_verified", "backup_verified_restore_tested", "backup_verified_lab_tested"] = (
        Field(
            description=(
                "The MINIMUM tier across every workload in `workloads` -- an honest floor, not an average or "
                "best-case. A customer with 10 well-tested workloads and 1 backup-only workload is 'backup_only' "
                "overall, not 'mostly lab-tested'. Defaults to 'backup_only' if `workloads` is empty."
            )
        )
    )
    label: str = Field(description="Human-readable label for `tier` (e.g. 'Backup, Verified, Lab-Tested Restore').")
    workloads: list[ProtectionLevelWorkloadOut] = Field(
        description="Per-workload breakdown. Workloads with zero snapshots in the period are omitted -- there's no tier to assign them."
    )
    tier_distribution_svg: str = Field(
        description=(
            "Raw <svg>...</svg> markup: a single stacked bar showing how many workloads sit at each tier, "
            "worst tier first. Built entirely from `workloads`, safe to render unescaped."
        )
    )


class OffsiteStatusEntryOut(BaseModel):
    backup_group_id: UUID = Field(description="The workload (VM/CT) this offsite-status entry reports on.")
    workload_label: str = Field(description="Human-readable identifier for the workload, e.g. 'web-01 (104)'.")
    has_offsite_copy: bool = Field(
        description="Whether an offsite copy of this workload's backups exists (on another PBS, or on a datastore "
        "with role 'offsite'), whether current or behind -- see `status` and `lag_hours`."
    )
    offsite_method: Literal["datastore_role", "sync_job", "exception", "none"] = Field(
        description="How the offsite copy is achieved: a PBS sync job copies it ('sync_job'), an operator accepted "
        "the gap with a reason ('exception', see `exception_reason`), or none. 'datastore_role' only appears in "
        "reports generated before RESURGAM 0.3.2."
    )
    sync_schedule: str | None = Field(
        default=None, description="Not filled any more (kept for reports generated before RESURGAM 0.3.2)."
    )
    last_synced_at: datetime | None = Field(
        default=None, description="Not filled any more (kept for reports generated before RESURGAM 0.3.2)."
    )
    status: (
        Literal[
            "ok",
            "lagging",
            "missing",
            "local_copy_only",
            "target_not_connected",
            "no_copy_job",
            "excepted",
            "not_required",
        ]
        | None
    ) = Field(
        default=None,
        description="Offsite status at report time, as on the 3-2-1 Gaps page (see GET /backup-gaps `status`). "
        "Null in reports generated before RESURGAM 0.3.2.",
    )
    copy_location: str | None = Field(default=None, description="Where the copy is or should be (PBS / datastore).")
    latest_copy_at: datetime | None = Field(default=None, description="Newest snapshot of the offsite copy.")
    lag_hours: float | None = Field(
        default=None, description="Age in hours of the oldest snapshot not copied yet; 0 = fully copied."
    )
    exception_reason: str | None = Field(
        default=None, description="Why an operator accepted this workload's offsite gap (status 'excepted')."
    )
    exception_by: str | None = Field(default=None, description="Who recorded that exception.")
    exception_at: datetime | None = Field(default=None, description="When the exception was recorded.")


class ThreeTwoOneOneZeroOut(BaseModel):
    """One leg of the 3-2-1-1-0 backup rule (3 copies, 2 media types, 1
    offsite, 1 immutable, 0 errors). 'offsite' and 'zero_errors' are
    computed straight from data RESURGAM observes directly. 'copies',
    'media_types', and 'immutable' are things RESURGAM cannot observe on
    its own -- they resolve through recorded `ComplianceAttestation`
    rows instead (see `POST /customers/{id}/compliance-attestations` and
    `POST /datastores/{id}/compliance-attestations`).
    """

    leg: Literal["copies", "media_types", "offsite", "immutable", "zero_errors"] = Field(
        description="Which leg of the 3-2-1-1-0 rule this entry reports on: 3 copies, 2 media types, 1 offsite, 1 immutable, 0 errors."
    )
    status: Literal["met", "not_met", "not_attested", "not_applicable"] = Field(
        description=(
            "'met'/'not_met' are real evaluations. 'not_attested' means this leg needs a self-assessment that "
            "nobody has recorded yet. 'not_applicable' means an operator has explicitly marked this leg out of "
            "scope for this customer (see `PUT /customers/{id}/leg-applicability/{leg}`) -- it never counts "
            "against them either way."
        )
    )
    detail: str = Field(
        description="Human-readable explanation of the status, e.g. which workloads are missing an offsite copy or how many datastores lack an immutability attestation."
    )
    attested_by: str | None = Field(
        description="Display name of whoever recorded the attestation this status is based on. Null for the two directly-observed legs, or when status is 'not_attested'/'not_applicable'."
    )
    attested_at: datetime | None = Field(
        description="When the attestation this status is based on was recorded. Null under the same conditions as `attested_by`."
    )


class RecoveryEvidenceReportOut(BaseModel):
    customer_id: UUID
    customer_name: str
    customer_match_code: str
    date_from: date
    date_to: date
    generated_at: datetime
    generated_by_user_id: UUID | None

    backup_history: list[BackupHistoryEntryOut]
    policy_adherence: list[PolicyAdherenceEntryOut]
    verification_results: VerificationResultsOut
    failed_jobs: list[FailedJobEntryOut]
    audit_trail: list[AuditTrailEntryOut]
    encryption_attestation: EncryptionAttestationOut
    restore_test_evidence: list[RestoreTestEvidenceEntryOut]
    management_summary: ManagementSummaryOut
    access_control: list[AccessControlEntryOut]
    pbs_datastore_access: list[PbsDatastoreAccessEntryOut]
    protection_level: ProtectionLevelOut
    offsite_status: list[OffsiteStatusEntryOut]
    three_two_one_one_zero: list[ThreeTwoOneOneZeroOut]

    framework: bool = False
    # Populated only when `framework=True` -- one entry per report
    # section key ("backup_history", "policy_adherence", ...) naming the
    # ISO27001/SOC2/ISO9001 controls it satisfies. Static editorial
    # metadata (`reporting/framework_mapping.py`), not customer-specific.
    section_controls: dict[str, list[FrameworkControlRef]] = {}
