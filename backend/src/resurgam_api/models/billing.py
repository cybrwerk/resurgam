import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class PricingPlan(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Not from the manifest — a billing feature (per-unit + per-GB
    fees). `customer_id` null means the global default plan; non-null
    means a per-customer override that wins over the global default when
    resolving what to charge (`inventory/billing.py::compute_customer_billing`).
    At most one global plan and at most one plan per customer are
    enforced at the database level (partial unique indexes in the
    migration), not just application logic.

    Per-unit pricing is split three ways (`price_per_vm_month`/
    `_ct_month`/`_host_month`, added 2026-09-14) rather than one flat
    per-unit rate — a VM, an LXC container, and a bare-metal agent
    backup are typically priced differently in practice, and the
    manifest's own `BackupType` (vm/ct/host) already distinguishes them
    everywhere else (usage counts, the tree view). Existing rows before
    this migration default the two new rates to 0, not the VM rate —
    silently charging for a previously-free unit type would be a real
    billing surprise, not a safe default.
    """

    __tablename__ = "pricing_plans"

    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_per_vm_month: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    price_per_ct_month: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal(0))
    price_per_host_month: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal(0))
    price_per_gb_month: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    # ADR 0033: data in an S3-backed datastore has its own, optional price.
    # Null = not charged per GB (the S3 provider may be paid some other way);
    # it never falls back to price_per_gb_month.
    price_per_s3_gb_month: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CHF")
