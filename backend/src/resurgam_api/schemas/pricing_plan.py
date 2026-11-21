from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PricingPlanCreateRequest(BaseModel):
    customer_id: UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    price_per_vm_month: Decimal = Field(ge=0, decimal_places=2)
    price_per_ct_month: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)
    price_per_host_month: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)
    price_per_gb_month: Decimal = Field(ge=0, decimal_places=4)
    currency: str = Field(default="CHF", min_length=3, max_length=3)


class PricingPlanUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    price_per_vm_month: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    price_per_ct_month: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    price_per_host_month: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    price_per_gb_month: Decimal | None = Field(default=None, ge=0, decimal_places=4)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class PricingPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: UUID | None
    name: str
    price_per_vm_month: Decimal
    price_per_ct_month: Decimal
    price_per_host_month: Decimal
    price_per_gb_month: Decimal
    currency: str
    created_at: datetime
