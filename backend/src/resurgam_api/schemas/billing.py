from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CustomerBillingOut(BaseModel):
    customer_id: UUID = Field(description="The customer this billing snapshot is for.")
    customer_name: str = Field(description="Customer display name, denormalized for convenience.")
    customer_match_code: str = Field(description="Customer's three-letter match code, denormalized for convenience.")
    vm_count: int = Field(description="Number of billable VMs (primary-role datastores only -- see DatastoreRole).")
    ct_count: int = Field(description="Number of billable containers (primary-role datastores only).")
    host_count: int = Field(description="Number of billable physical/host backups (primary-role datastores only).")
    estimated_physical_bytes: int = Field(
        description="Estimated total physical storage used, across primary and offsite copies."
    )
    estimated_s3_bytes: int = Field(
        description="The part of estimated_physical_bytes that lies in S3-backed datastores (ADR 0033). It is "
        "charged at price_per_s3_gb_month, or not at all when that is null; the rest at price_per_gb_month."
    )
    physical_bytes_estimate_complete: bool = Field(
        description="False if any backup group's size could not be determined (e.g. no snapshots yet, or an S3-backed datastore on which no garbage collection has run yet -- its bucket size comes from the last GC, ADR 0033), meaning estimated_physical_bytes is a partial figure."
    )
    pricing_plan_id: UUID | None = Field(
        description="The pricing plan actually applied, or null if none is configured for this customer or globally."
    )
    pricing_plan_source: Literal["customer", "global"] | None = Field(
        description="Whether the applied plan is this customer's own override or the platform-wide default. Null if pricing_plan_id is null."
    )
    price_per_vm_month: Decimal | None = Field(description="Monthly price per billable VM, from the applied plan.")
    price_per_ct_month: Decimal | None = Field(
        description="Monthly price per billable container, from the applied plan."
    )
    price_per_host_month: Decimal | None = Field(description="Monthly price per billable host, from the applied plan.")
    price_per_gb_month: Decimal | None = Field(
        description="Monthly price per GB of estimated_physical_bytes outside S3 (i.e. minus estimated_s3_bytes), from the applied plan."
    )
    price_per_s3_gb_month: Decimal | None = Field(
        description="Monthly price per GB of estimated_s3_bytes, from the applied plan. Null when no plan is applied or the plan does not charge S3 data (e.g. the S3 provider is paid some other way)."
    )
    currency: str | None = Field(description="Currency code for the price fields, from the applied plan.")
    estimated_monthly_cost: Decimal | None = Field(
        description="Total estimated monthly cost computed from the applied plan's prices and this customer's counts/usage. Null if no plan is applied."
    )
