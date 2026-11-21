import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class TlsTrustMode(str, Enum):
    CA = "ca"
    FINGERPRINT = "fingerprint"


class ConnectionHealthStatus(str, Enum):
    """Classified result of the last connection check (manual test,
    automatic post-save test or periodic heartbeat) -- produced by
    `connectivity.health.check_pbs_health`/`check_pve_health`. Values fit
    the existing `String(20)` column, hence `missing_privileges` rather
    than the longer "insufficient_privileges".
    """

    OK = "ok"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"
    TLS_MISMATCH = "tls_mismatch"
    AUTH_FAILED = "auth_failed"
    MISSING_PRIVILEGES = "missing_privileges"


class PbsInstance(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A managed PBS instance (manifest §6.1). Never stores the API token
    itself — only a pointer (`api_token_secret_ref_id`) into the secrets
    provider (see `resurgam_api.secrets`).
    """

    __tablename__ = "pbs_instances"

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8007)

    tls_trust_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    tls_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    api_token_secret_ref_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )

    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    capabilities: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ConnectionHealthStatus.UNKNOWN.value
    )

    # Set by `collector.sync.sync_pbs_instance` itself, so both the manual
    # "Sync" button and the scheduled `worker/collector_scheduler.py` pass
    # update it uniformly -- this is the staleness signal the frontend's
    # Jobs pages show next to each instance's last-run data.
    last_collector_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The PBS server's own timezone (IANA name), refreshed by every
    # Collector sync. PBS prune buckets snapshots by *local* day/hour, so
    # the File Indexing schedule needs it to pick the snapshot `keep-daily`
    # will keep (ADR 0016). None until the first successful sync.
    server_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
