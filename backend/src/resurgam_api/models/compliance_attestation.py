import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class ComplianceAttestation(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Fills the three 3-2-1-1-0 legs RESURGAM cannot observe on its own
    (immutability, production copy count, media-type diversity) via a
    real, audited self-assessment -- raised directly by the user: every
    attestation must carry its author and be kept as permanent history,
    never overwritten. Append-only, exactly like `AuditEvent` (migration
    0001's trigger), not a plain editable settings row -- the "current"
    value for a given `(scope_type, scope_id, dimension)` is simply its
    most recent row; every prior row stays as real history.

    `scope_id` has no FK constraint across the two possible target
    tables (`datastores`/`customers`), same reasoning `AuditEvent.target_type`/
    `target_id` already uses. `dimension` is a controlled vocabulary owned
    at the application layer (`api/v1/compliance_attestations.py`), matching
    `ApplicationGroupMember.check_types`'s precedent: `"immutable_copy"` for
    datastores; `"production_copy_count"`, `"media_type_diversity"` for
    customers. `value`'s shape depends on `dimension` -- a bool for
    `immutable_copy`, an int for `production_copy_count`, a string list
    for `media_type_diversity`.
    """

    __tablename__ = "compliance_attestations"

    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dimension: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[dict | list | bool | int] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    asserted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
