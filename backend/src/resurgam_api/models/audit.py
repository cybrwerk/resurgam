import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, event, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, object_session

from resurgam_api.db import Base
from resurgam_api.models.base import UUIDPrimaryKeyMixin


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    """Append-only audit trail entry (manifest §8: "Audit Events nach dem
    Schreiben nicht änderbar machen"). Immutability is enforced both here
    (no update/delete path in application code) and at the database level by
    a rule added in the initial migration that rejects UPDATE/DELETE on this
    table outright.
    """

    __tablename__ = "audit_events"

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Recovery Evidence compliance reporting, raised directly by the user:
    # denormalized rather than resolved via target_type/target_id at query
    # time (that pair is a generic String, not a real FK, and most target
    # types have no customer at all) -- threaded through at write time from
    # whichever call sites represent actual restore/access activity for a
    # customer. NULL for events recorded before this column existed
    # (immutability trigger blocks a backfill, see migration 0026) and for
    # every admin/infra-config action, which has no customer to attribute.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    # A real gap found live (2026-09-23, CISO audit): `actor_user_id` is
    # `ON DELETE SET NULL` -- offboarding a user silently anonymized
    # their entire historical audit trail, since nothing else on this
    # row said who they were. Snapshotted at write time via the
    # `before_insert` listener below (not by any of this codebase's ~18
    # separate `_record_audit` call sites, which would be an easy line
    # to forget in a 19th) -- the same "denormalize at write time" fix
    # `customer_id` above already got, for the same reason: a value this
    # row must still answer correctly after its source row is gone.
    actor_label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Snapshotted alongside `actor_label` -- break-glass is an
    # emergency-only credential; its use should stay visually
    # identifiable in the audit trail even after the user row (and thus
    # a live `is_breakglass` lookup) is gone.
    actor_is_breakglass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


@event.listens_for(AuditEvent, "before_insert")
def _snapshot_actor_label(mapper, connection, target: "AuditEvent") -> None:
    """Runs inside the flush, once, for every `AuditEvent` insert
    regardless of which call site created it -- the one place this can't
    be forgotten, unlike duplicating the resolve-and-pass logic across
    every `_record_audit` helper. A core (non-ORM) `connection.execute`
    is used deliberately: triggering a nested ORM query/flush from
    inside a `before_insert` listener is the classic way to corrupt the
    session's unit-of-work.
    """
    if target.actor_label is not None or target.actor_user_id is None:
        return
    users = Base.metadata.tables["users"]
    row = connection.execute(
        select(users.c.display_name, users.c.email, users.c.is_breakglass).where(users.c.id == target.actor_user_id)
    ).first()
    if row is None:
        return
    target.actor_label = f"{row.display_name} ({row.email})"
    target.actor_is_breakglass = bool(row.is_breakglass)
    # Request authenticated with a personal API token (docs/adr/0029):
    # `auth/dependency.py` leaves the token's label on the per-request
    # session, so every audit event of that request names the token.
    session = object_session(target)
    token_label = session.info.get("api_token_label") if session is not None else None
    if token_label:
        target.actor_label = f"{target.actor_label} via API token {token_label}"[:300]
