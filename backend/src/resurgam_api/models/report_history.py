import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class ReportHistory(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A permanent record of exactly what was generated and when --
    raised directly by the user: a generated Recovery Evidence PDF is
    evidence handed to an auditor, so RESURGAM should keep the *exact*
    bytes that were actually delivered (checksummed), not just be able
    to regenerate a fresh one on demand later, which could legitimately
    produce different numbers as underlying data changes.

    `report_type` is a discriminator column ("recovery_evidence" for
    now), not a new table per report type, matching `FailedJobEntryOut.
    source`'s existing precedent -- a second report type later needs no
    schema surgery. `customer_id` is SET NULL (mirrors `AuditEvent.
    customer_id`, migration 0026): a permanent record shouldn't vanish
    just because the customer it was about is later deleted.

    Row content (`report_type`/`date_from`/`date_to`/`framework`/
    `sha256_checksum`/`generated_by_user_id`) is never updated once
    written -- enforced by migration 0032's trigger, same discipline as
    `AuditEvent`/`ComplianceAttestation`. Unlike those two tables,
    though, rows here ARE deletable: the whole point of this table is a
    customer- and platform-governed *retention* period (see `Customer.
    report_retention_days`/`InstanceSettings.min_report_retention_days`),
    so the immutability trigger blocks UPDATE only, not DELETE -- the
    retention sweep (`worker/file_index_worker.py`'s existing periodic
    loop) must be able to purge expired rows.
    """

    __tablename__ = "report_history"

    report_type: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    framework: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sha256_checksum: Mapped[str] = mapped_column(String(64), nullable=False)


class ReportHistoryContent(Base, CreatedAtMixin):
    """The generated PDF's exact bytes, split into their own table --
    mirrors `LocalSecretValue`'s split (and the former `FileRestoreJobContent`) so the
    frequently-listed `report_history` table never carries blob weight.
    Cascade-deleted alongside its parent row by the retention sweep.
    """

    __tablename__ = "report_history_content"

    report_history_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("report_history.id", ondelete="CASCADE"), primary_key=True
    )
    pdf_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
