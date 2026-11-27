from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_S3_PRICE = (
    "Monthly price per GB of data in S3-backed datastores (ADR 0033), charged instead of price_per_gb_month "
    "for that data. Optional: null means S3 data is not charged per GB at all, e.g. because the S3 provider "
    "is paid some other way. It never falls back to price_per_gb_month."
)


class PricingPlanCreateRequest(BaseModel):
    customer_id: UUID | None = Field(
        default=None,
        description="Customer this plan applies to, or null for the global default plan. At most one plan per "
        "customer and one global plan.",
    )
    name: str = Field(min_length=1, max_length=100, description="Plan name shown in the UI.")
    price_per_vm_month: Decimal = Field(ge=0, decimal_places=2, description="Monthly price per billable VM.")
    price_per_ct_month: Decimal = Field(
        default=Decimal(0), ge=0, decimal_places=2, description="Monthly price per billable LXC container."
    )
    price_per_host_month: Decimal = Field(
        default=Decimal(0), ge=0, decimal_places=2, description="Monthly price per billable host backup."
    )
    price_per_gb_month: Decimal = Field(
        ge=0,
        decimal_places=4,
        description="Monthly price per GB (10^9 bytes) of estimated physical data on filesystem datastores.",
    )
    price_per_s3_gb_month: Decimal | None = Field(default=None, ge=0, decimal_places=4, description=_S3_PRICE)
    currency: str = Field(default="CHF", min_length=3, max_length=3, description="ISO 4217 currency code.")


class PricingPlanUpdateRequest(BaseModel):
    """Only the fields sent are changed. Send `price_per_s3_gb_month: null` to stop charging S3 data."""

    name: str | None = Field(default=None, min_length=1, max_length=100, description="New plan name.")
    price_per_vm_month: Decimal | None = Field(
        default=None, ge=0, decimal_places=2, description="New monthly price per billable VM."
    )
    price_per_ct_month: Decimal | None = Field(
        default=None, ge=0, decimal_places=2, description="New monthly price per billable LXC container."
    )
    price_per_host_month: Decimal | None = Field(
        default=None, ge=0, decimal_places=2, description="New monthly price per billable host backup."
    )
    price_per_gb_month: Decimal | None = Field(
        default=None,
        ge=0,
        decimal_places=4,
        description="New monthly price per GB of estimated physical data on filesystem datastores.",
    )
    price_per_s3_gb_month: Decimal | None = Field(default=None, ge=0, decimal_places=4, description=_S3_PRICE)
    currency: str | None = Field(default=None, min_length=3, max_length=3, description="New ISO 4217 currency code.")


class PricingPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Plan id.")
    customer_id: UUID | None = Field(description="Customer this plan applies to, or null for the global default plan.")
    name: str = Field(description="Plan name.")
    price_per_vm_month: Decimal = Field(description="Monthly price per billable VM.")
    price_per_ct_month: Decimal = Field(description="Monthly price per billable LXC container.")
    price_per_host_month: Decimal = Field(description="Monthly price per billable host backup.")
    price_per_gb_month: Decimal = Field(
        description="Monthly price per GB of estimated physical data on filesystem datastores."
    )
    price_per_s3_gb_month: Decimal | None = Field(description=_S3_PRICE)
    currency: str = Field(description="ISO 4217 currency code of all prices.")
    created_at: datetime = Field(description="When the plan was created.")
