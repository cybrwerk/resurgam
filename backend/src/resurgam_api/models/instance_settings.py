from sqlalchemy import Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class InstanceSettings(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Platform-wide (not per-customer) settings for this RESURGAM
    deployment. Currently one field -- room to grow the same way
    `BrandingSettings` started with just logo/colors. Singleton, same
    convention as `BrandingSettings`/`OidcSettings`: at most one row,
    enforced in application code (`api/v1/instance_settings.py`), not a
    DB constraint. Absence of a row means the hardcoded defaults below
    apply -- there is no env-var fallback to merge, matching
    `BrandingSettings`'s own reasoning.
    """

    __tablename__ = "instance_settings"

    # A customer's own `report_retention_days` (see `models/tenancy.py`)
    # can extend retention beyond this, but never shrink below it --
    # raised directly by the user: a RESURGAM-wide floor a customer-level
    # setting can never undercut, defaulting to one year.
    min_report_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365)

    # How often the Collector scheduler (`worker/collector_scheduler.py`)
    # re-syncs each non-disabled `PbsInstance` -- raised directly by the
    # user after finding the Collector had no scheduler at all (only the
    # manual "Sync" button). Re-read from this row every scheduler tick,
    # so a change here takes effect without restarting the worker.
    collector_poll_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    # Users & Roles -> "Require MFA for all local users" (raised directly by
    # the user, 2026-10-11): the global form of `User.mfa_required`, enforced
    # the same way at every local account's next login. Break-glass and SSO
    # accounts are never affected.
    require_mfa_all_local_users: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # ADR 0032: how old the oldest not-yet-copied snapshot of a backup group
    # may get before its offsite copy counts as lagging. Null = the defaults
    # in `inventory/offsite.py`.
    offsite_lag_warning_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offsite_lag_critical_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
