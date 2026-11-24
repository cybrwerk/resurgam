from datetime import UTC, datetime

from sqlalchemy.orm import Session

from resurgam_api.models.audit import AuditEvent


def record_login_audit_event(
    db: Session,
    *,
    action: str,
    actor_user_id=None,
    correlation_id: str | None,
    detail: dict,
) -> None:
    db.add(
        AuditEvent(
            # The wall clock, not the column default `now()` (the start of the
            # transaction): `attempt_limit` orders failures against the last
            # successful sign-in by this time.
            occurred_at=datetime.now(UTC),
            actor_user_id=actor_user_id,
            action=action,
            target_type="user",
            target_id=str(actor_user_id) if actor_user_id else None,
            correlation_id=correlation_id,
            detail=detail,
        )
    )
    db.flush()
