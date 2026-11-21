import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import UUIDPrimaryKeyMixin


class ThreeTwoOneOneZeroLegSetting(Base, UUIDPrimaryKeyMixin):
    """Per-customer on/off toggle for one 3-2-1-1-0 leg -- raised directly
    by the user: not every leg is contractually relevant to every
    customer (e.g. a deliberately single-site customer has no "offsite"
    obligation to fail against). Absence of a row for a given
    `(customer_id, leg)` means "applicable" (the honest default -- a
    customer starts fully in scope, an operator has to deliberately carve
    out an exception, not the other way around).

    Ordinary mutable settings row, not an append-only attestation --
    "is this leg in scope for this customer" is a contractual scoping
    decision the operator makes, not evidence the customer is vouching
    for, so it doesn't need `ComplianceAttestation`'s permanent-history
    discipline. `platform_admin`-only (`api/v1/leg_applicability.py`):
    deliberately not open to a customer's own `customer_admin`, unlike
    attestations -- letting a customer turn off their own compliance
    checks would be a very different, easier-to-game action than letting
    them self-report a fact.
    """

    __tablename__ = "three_two_one_one_zero_leg_settings"
    __table_args__ = (UniqueConstraint("customer_id", "leg", name="uq_321_leg_setting_customer_leg"),)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    leg: Mapped[str] = mapped_column(String(20), nullable=False)
    applicable: Mapped[bool] = mapped_column(default=True, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
