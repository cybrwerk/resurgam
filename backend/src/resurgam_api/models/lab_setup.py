import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class LabNodePairing(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A one-time code that pairs a lab node's `resurgam-lab-agent` with this
    RESURGAM (ADR 0027): the admin copies one command into the node's shell,
    which downloads `script` (pinned by `script_sha256` in that command),
    installs the agent and exchanges the code for the cluster's agent token
    (`POST /lab-setup/bootstrap/{code}/pair`). Only the code's SHA-256 is
    stored; it is single-use (`consumed_at`) and expires (`expires_at`).
    The script holds no secret -- the agent token is only handed out by the
    pair call, the node's PBS token never leaves the node."""

    __tablename__ = "lab_node_pairings"

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    overlay_storage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    overlay_dir: Mapped[str] = mapped_column(String(255), nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when a newer code for the same cluster replaced this one.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pbs_token_provided: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class LabAgentSetupJob(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A template build the paired lab agent runs on its node (ADR 0027) --
    instead of the backend driving it over a one-time SSH login. `spec` is
    structured data only (VMID, storages, uplink NIC settings, image
    source); the agent validates it and runs `resurgam_worker.lab_build`
    itself. `steps`/`result` mirror the SSH jobs' step log, so the wizard
    shows both the same way."""

    __tablename__ = "lab_agent_setup_jobs"

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    uplink_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('template_build', 'share_helper_template_build')", name="ck_lab_agent_setup_jobs_kind"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_lab_agent_setup_jobs_status"
        ),
    )
