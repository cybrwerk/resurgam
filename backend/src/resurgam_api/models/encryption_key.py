import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class EncryptionKey(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A PBS client-side encryption key, stored by its fingerprint (ADR 0031,
    stage 2). Every read of an encrypted backup uses the key whose fingerprint
    the backup carries; unencrypted backups get no key at all. The keyfile
    itself lives in the credential store (`SecretPurpose.PBS_ENCRYPTION_KEY`),
    exactly as uploaded or generated, so a download returns the original file."""

    __tablename__ = "encryption_keys"

    fingerprint: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    keyfile_secret_ref_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )
    # "none" for every key PVE or `proxmox-backup-client key create --kdf none`
    # makes; password-protected keys are not accepted yet (ADR 0031).
    kdf: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    # The keyfile's own "created" time, when it carries one.
    key_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Where it came from: "upload", "import", "datastore" (adopted from ADR 0022).
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="upload")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # An admin confirmed that a copy (key sheet or downloaded keyfile) is stored
    # outside RESURGAM. Until then an alarm reminds them.
    copy_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    copy_confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
