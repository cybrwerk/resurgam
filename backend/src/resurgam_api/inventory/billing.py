import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from resurgam_api.auth.scope import effective_group_customer_id
from resurgam_api.models.billing import PricingPlan
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment, DatastoreRole
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, BackupType
from resurgam_api.models.tenancy import Customer
from resurgam_api.models.usage import UsageMeasurement
from resurgam_api.schemas.billing import CustomerBillingOut

_BYTES_PER_GB = 1_000_000_000


def _latest_usage_by_datastore(db: Session, datastore_ids: set[uuid.UUID]) -> dict[uuid.UUID, UsageMeasurement]:
    if not datastore_ids:
        return {}
    latest: dict[uuid.UUID, UsageMeasurement] = {}
    rows = (
        db.query(UsageMeasurement)
        .filter(UsageMeasurement.datastore_id.in_(datastore_ids))
        .order_by(UsageMeasurement.datastore_id, UsageMeasurement.captured_at.desc())
        .all()
    )
    for row in rows:
        latest.setdefault(row.datastore_id, row)
    return latest


def _group_logical_bytes(db: Session, group_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not group_ids:
        return {}
    rows = (
        db.query(BackupSnapshot.backup_group_id, func.coalesce(func.sum(BackupSnapshot.size_bytes), 0))
        .filter(BackupSnapshot.backup_group_id.in_(group_ids))
        .group_by(BackupSnapshot.backup_group_id)
        .all()
    )
    # Postgres SUM(bigint) returns numeric, which psycopg maps to Decimal —
    # normalize to int so downstream arithmetic doesn't mix Decimal/float.
    return {group_id: int(total) for group_id, total in rows}


def compute_customer_billing(db: Session, customer: Customer) -> CustomerBillingOut:
    """Not from the manifest — the actual computation `PricingPlan` rows
    were created for but nothing read until now (a gap ADR 0009 itself
    named). Computed fresh on every call, never stored — same "computed
    on read" pattern as protection/usage/alarms.

    Per-unit fee: counts backup groups whose *effective* customer (own
    override, else datastore assignment — `effective_group_customer_id`)
    is this customer, restricted to groups on a `primary`-role datastore
    (ADR 0009: an `offsite`/DR copy doesn't double-count a unit) —
    split three ways by `BackupType` (vm/ct/host, each priced separately
    on the plan, 2026-09-14) rather than one flat per-unit count. A
    datastore with no assignment row at all is treated as `primary` —
    the same default `DatastoreCustomerAssignment.role` itself uses when
    an assignment does exist, so an unassigned datastore isn't silently
    excluded just because nobody has clicked "assign" yet.

    Per-GB fee: every matching group counts (primary and offsite both —
    it's real disk consumed regardless of DR role), using the approach
    the user confirmed as an acceptable estimate: each datastore's own
    dedup ratio (`UsageMeasurement.used_bytes / logical_bytes`, both
    already aggregate across every group on that datastore) applied
    proportionally to this group's own logical bytes (the sum of its
    retained snapshots' `size_bytes`). A datastore with no usage
    measurement yet contributes 0 bytes for its groups and flips
    `physical_bytes_estimate_complete` to False rather than blocking the
    whole computation.
    """
    datastore_assignments = {a.datastore_id: a for a in db.query(DatastoreCustomerAssignment).all()}
    # A superseded generation (a reused VMID's frozen prior identity,
    # see BackupGroup's own docstring) is not a currently billable unit
    # -- without this filter, a VMID reuse would silently mint a second,
    # permanently-open billable unit on top of the live one.
    groups = db.query(BackupGroup).filter(BackupGroup.superseded_at.is_(None)).all()

    matching = [
        g for g in groups if effective_group_customer_id(g, datastore_assignments.get(g.datastore_id)) == customer.id
    ]

    usage_by_datastore = _latest_usage_by_datastore(db, {g.datastore_id for g in matching})
    logical_by_group = _group_logical_bytes(db, [g.id for g in matching])
    s3_datastores = {d.id for d in db.query(Datastore).filter(Datastore.backend_type == "s3").all()}

    unit_counts = {BackupType.VM.value: 0, BackupType.CT.value: 0, BackupType.HOST.value: 0}
    estimated_physical_bytes = 0
    estimate_complete = True

    for group in matching:
        assignment = datastore_assignments.get(group.datastore_id)
        role = assignment.role if assignment is not None else DatastoreRole.PRIMARY.value
        if role == DatastoreRole.PRIMARY.value and group.backup_type in unit_counts:
            unit_counts[group.backup_type] += 1

        usage = usage_by_datastore.get(group.datastore_id)
        group_logical_bytes = logical_by_group.get(group.id, 0)
        # ADR 0033: on an S3 datastore `used_bytes` is the local cache; the
        # bucket's data (what the provider bills) is `s3_data_bytes`, known
        # once a garbage collection has run.
        physical = None
        if usage is not None:
            physical = usage.s3_data_bytes if group.datastore_id in s3_datastores else usage.used_bytes
        if usage is None or not usage.logical_bytes or not group_logical_bytes or physical is None:
            if group_logical_bytes:
                estimate_complete = False
            continue
        dedup_ratio = physical / usage.logical_bytes
        estimated_physical_bytes += round(group_logical_bytes * dedup_ratio)

    plan = db.query(PricingPlan).filter_by(customer_id=customer.id).one_or_none()
    plan_source: str | None = "customer" if plan is not None else None
    if plan is None:
        plan = db.query(PricingPlan).filter_by(customer_id=None).one_or_none()
        plan_source = "global" if plan is not None else None

    estimated_monthly_cost = None
    if plan is not None:
        raw_cost = (
            plan.price_per_vm_month * unit_counts[BackupType.VM.value]
            + plan.price_per_ct_month * unit_counts[BackupType.CT.value]
            + plan.price_per_host_month * unit_counts[BackupType.HOST.value]
            + plan.price_per_gb_month * (Decimal(estimated_physical_bytes) / Decimal(_BYTES_PER_GB))
        )
        # An estimate, not an invoice line — two decimal places is plenty;
        # the underlying Decimal division above otherwise carries far more
        # digits than any currency actually uses.
        estimated_monthly_cost = raw_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return CustomerBillingOut(
        customer_id=customer.id,
        customer_name=customer.name,
        customer_match_code=customer.match_code,
        vm_count=unit_counts[BackupType.VM.value],
        ct_count=unit_counts[BackupType.CT.value],
        host_count=unit_counts[BackupType.HOST.value],
        estimated_physical_bytes=estimated_physical_bytes,
        physical_bytes_estimate_complete=estimate_complete,
        pricing_plan_id=plan.id if plan is not None else None,
        pricing_plan_source=plan_source,
        price_per_vm_month=plan.price_per_vm_month if plan is not None else None,
        price_per_ct_month=plan.price_per_ct_month if plan is not None else None,
        price_per_host_month=plan.price_per_host_month if plan is not None else None,
        price_per_gb_month=plan.price_per_gb_month if plan is not None else None,
        currency=plan.currency if plan is not None else None,
        estimated_monthly_cost=estimated_monthly_cost,
    )
