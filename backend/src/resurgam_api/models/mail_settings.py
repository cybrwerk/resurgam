import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class MailSettings(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Administration -> System Settings: the SMTP adapter used to send
    report/alarm/billing notification emails -- raised directly by the
    user: "simply using MTA as SMTP", i.e. relaying through whatever mail
    transport agent (e.g. a local Postfix) the operator already runs, not
    a third-party email API. Singleton, same convention as
    `OidcSettings`/`BrandingSettings`: at most one row, enforced in
    application code (`api/v1/mail_settings.py`).

    `smtp_password_ref_id` follows `OidcSettings.client_secret_ref_id`'s
    exact pattern -- a pointer into `SecretReference`, never the
    plaintext password itself (manifest §14). Both username and password
    are nullable: a local MTA relay on the same host commonly needs no
    authentication at all.

    `enabled` (not row presence alone) gates whether anything actually
    sends -- mirrors `CustomerFileIndexSettings.enabled`'s "keep the
    configuration, pause the behavior" pattern, so an admin can stop
    outbound mail without losing what they entered.
    """

    __tablename__ = "mail_settings"

    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="SET NULL"), nullable=True
    )
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
