import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class UpdateSourceSettings(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """System Settings -> Updates -> Update source (ADR 0026): the GitHub
    (or GitLab) repository RESURGAM release bundles are published to, so
    the appliance can find and download new versions itself instead of an
    operator uploading every bundle by hand -- raised directly by the user
    ("a repo checker which pulls versions from there ... put a PAT in the
    web GUI"; RESURGAM itself lives on GitHub, GitLab works the same way
    with just another URL).

    Singleton, same convention as `MailSettings`: at most one row,
    enforced in application code (`api/v1/update_source.py`). The
    personal access token follows `MailSettings.smtp_password_ref_id`'s
    pattern -- a pointer into `SecretReference`, never the token itself.

    The `last_check_*`/`available_releases` columns cache the result of
    the most recent check (manual or by the collector scheduler), so
    opening the Updates tab never waits on GitHub/GitLab.
    """

    __tablename__ = "update_source_settings"

    # "github" or "gitlab" (`updates/release_source.py` PROVIDERS); the UI
    # pre-selects it from the URL's host, an operator overrides it for a
    # self-hosted GitLab or GitHub Enterprise host.
    provider: Mapped[str] = mapped_column(String(10), nullable=False)
    # The repository's web URL as pasted, e.g.
    # https://github.com/cybrwerk/resurgam -- the API base URL and
    # the owner/repo (GitHub) or project path (GitLab) are derived from it.
    repository_url: Mapped[str] = mapped_column(String(500), nullable=False)
    token_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="SET NULL"), nullable=True
    )
    tls_verify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    include_prereleases: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    check_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Newer-than-running releases found by the last successful check, newest
    # first: [{version, tag_name, name, released_at, notes, web_url,
    # prerelease, bundle_name, bundle_size, bundle_url}] -- see
    # updates/release_source.py.
    available_releases: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint("provider IN ('github', 'gitlab')", name="ck_update_source_settings_provider"),
        CheckConstraint(
            "check_interval_hours BETWEEN 1 AND 720", name="ck_update_source_settings_check_interval_hours"
        ),
    )
