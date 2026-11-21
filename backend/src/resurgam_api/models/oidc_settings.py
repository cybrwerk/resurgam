import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class OidcSettings(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Administration -> Integrations, manifest §6.3 -- DB-backed OIDC
    config, authoritative over `Settings.oidc_*` (env/`.env`) when
    `enabled=True`. Singleton: at most one row is ever expected, enforced
    in application code (`api/v1/integrations.py`), not a DB constraint.

    `enabled` (not row presence alone) gates precedence, mirroring
    `CustomerFileIndexSettings`'s own "row exists, but can still be
    paused without losing config" pattern -- an admin can disable the DB
    override and fall back to `.env` without deleting what they entered.
    """

    __tablename__ = "oidc_settings"

    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="SET NULL"), nullable=True
    )
    redirect_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
