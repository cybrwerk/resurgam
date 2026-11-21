from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class BrandingSettings(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Administration -> System Settings: white-label branding, confirmed
    per-instance (not per-customer) -- one logo, one primary color, one
    accent color, configured once by a `platform_admin`, shown to
    everyone. Singleton, same convention as `OidcSettings` (at most one
    row, enforced in application code via `api/v1/branding.py`, not a DB
    constraint).

    Absence of a row (or of an individual field) means "use RESURGAM's
    own defaults" -- there is no env-var fallback to merge here, unlike
    `OidcSettings`/`Settings.oidc_*`, since branding has no equivalent
    pre-existing config source.
    """

    __tablename__ = "branding_settings"

    logo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    logo_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    accent_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    # The product name shown in the browser tab, top bar, login page and on
    # PDF reports (white-label, 2026-10-11). None = "RESURGAM".
    app_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
