import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin

# PBS's own client-side ("zero-knowledge") backup encryption is
# configured per storage *definition* on the PVE side -- one key covers
# every backup sent through that definition, cluster-wide. A RESURGAM
# `Datastore` row is the closest match to that granularity (one PBS
# datastore under one `PbsInstance`), so the key lives here rather than
# on `BackupGroup` (would mean setting the same key redundantly on every
# group) or `Customer` (doesn't naturally cover a customer with multiple
# datastores using different keys). Every `BackupGroup` resolves its
# datastore's key via the existing `datastore_id` FK -- one hop, no
# inheritance chain. See docs/adr/0022-pbs-client-side-encryption.md.


class DatastoreRole(str, Enum):
    """Not from the manifest — added for billing (per-VM fees must not
    double-count a VM replicated to a secondary/DR datastore). PRIMARY
    counts toward a customer's per-VM billing count; OFFSITE doesn't, but
    still counts toward per-GB billing (it's real disk, just not a second
    copy of the same VM to bill for).
    """

    PRIMARY = "primary"
    OFFSITE = "offsite"


class Datastore(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A PBS datastore imported into RESURGAM (manifest §9.1). RESURGAM
    stores only identity/metadata here — it is not a system of record for
    datastore contents or usage (that's PBS itself; usage measurement is a
    later EPIC).
    """

    __tablename__ = "datastores"
    __table_args__ = (UniqueConstraint("pbs_instance_id", "name", name="uq_datastore_pbs_instance_name"),)

    pbs_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pbs_instances.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    encryption_key_secret_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="SET NULL"), nullable=True
    )
    # Set by the Collector's datastore discovery (`collector.sync.
    # discover_datastores`) when PBS's `GET /config/datastore` no longer
    # lists this datastore; cleared again if it reappears. Mirrors
    # `BackupGroup.removed_from_pbs_at` (migration 0051): nothing is ever
    # deleted -- history, customer assignment and usage stay intact, the
    # row just stops counting as live.
    removed_from_pbs_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # PBS 4.x datastore backend (ADR 0033), refreshed by datastore discovery
    # from the `backend` property of `GET /config/datastore`: "filesystem" or
    # "s3"; null until the first discovery after migration 0074. For S3 the
    # endpoint id (`PbsS3Endpoint.name`) and bucket -- `path` is then the
    # local cache, and PBS's used/avail/total describe that cache filesystem.
    backend_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    s3_endpoint_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    s3_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)

    @property
    def is_s3(self) -> bool:
        return self.backend_type == "s3"


class PbsS3Endpoint(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """An S3 endpoint configured on a PBS instance (`GET /config/s3`, ADR
    0033), synced by the Collector. A datastore on it counts as offsite for
    3-2-1 unless an operator marked the endpoint `on_site` (an object store in
    the same building) -- with a reason, audited. The mark is RESURGAM's own
    and survives every sync; a removed endpoint gets `removed_from_pbs_at`.
    """

    __tablename__ = "pbs_s3_endpoints"
    __table_args__ = (UniqueConstraint("pbs_instance_id", "name", name="uq_pbs_s3_endpoint_name"),)

    pbs_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pbs_instances.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    path_style: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_site: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_site_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    on_site_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    on_site_changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    removed_from_pbs_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DatastoreCustomerAssignment(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Manifest §9.1: a datastore is assigned to exactly one customer.
    Enforced here via a unique constraint on `datastore_id`, not just
    application logic — a second assignment for the same datastore is a
    database-level conflict, not merely a business-rule check.
    """

    __tablename__ = "datastore_customer_assignments"

    datastore_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datastores.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=DatastoreRole.PRIMARY.value)
