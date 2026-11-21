"""Replay protection for forced-MFA enrollment tokens (ADR 0028).

An enrollment token (`create_mfa_enrollment_token`) is stateless and lives
15 minutes. Without extra checks it could be replayed after the enrollment
it was issued for -- exchanged for a second session without a second
factor, used to replace the user's recovery codes, or to register an
attacker's security key -- and a token issued *before* someone else's
enrollment would still work afterwards.

Instead of a new table, the bindings live in the append-only audit trail
(`audit_events`, immutable at the database level), keyed by the token's
`jti`:

* `mfa_enrollment_factor_added` -- a factor (TOTP or security key) was
  added with this token; `detail.credential_id` names the row.
* `mfa_enrollment_recovery_codes` -- recovery codes were generated with
  this token (allowed once per enrollment).
* `local_login` with `detail.enrollment_jti` -- the token was exchanged for
  a session (`/auth/mfa/enrollment/complete`); the token is spent.

A token is usable while it is not spent AND every confirmed factor the
account currently has was added with this very token. So: no factor yet
-> usable (equivalent to knowing the password, which can fetch a fresh
token anyway); a factor from another enrollment, from the profile page, or
from before an admin reset -> refused.
"""

import uuid

from sqlalchemy.orm import Session

from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.mfa import TotpCredential, WebauthnCredential

FACTOR_ADDED_ACTION = "mfa_enrollment_factor_added"
RECOVERY_CODES_ACTION = "mfa_enrollment_recovery_codes"
COMPLETED_ACTION = "local_login"


def _events(db: Session, user_id: uuid.UUID, action: str, jti: str):
    return db.query(AuditEvent).filter(
        AuditEvent.action == action,
        AuditEvent.target_type == "user",
        AuditEvent.target_id == str(user_id),
        AuditEvent.detail["enrollment_jti"].astext == jti,
    )


def confirmed_factor_ids(db: Session, user_id: uuid.UUID) -> set[str]:
    totp = {
        str(row[0])
        for row in db.query(TotpCredential.id)
        .filter(TotpCredential.user_id == user_id, TotpCredential.confirmed_at.isnot(None))
        .all()
    }
    keys = {str(row[0]) for row in db.query(WebauthnCredential.id).filter(WebauthnCredential.user_id == user_id).all()}
    return totp | keys


def enrollment_token_spent(db: Session, user_id: uuid.UUID, jti: str) -> bool:
    return db.query(_events(db, user_id, COMPLETED_ACTION, jti).exists()).scalar()


def enrollment_token_usable(db: Session, user_id: uuid.UUID, jti: str) -> bool:
    if enrollment_token_spent(db, user_id, jti):
        return False
    factors = confirmed_factor_ids(db, user_id)
    if not factors:
        return True
    bound = {
        (event.detail or {}).get("credential_id") for event in _events(db, user_id, FACTOR_ADDED_ACTION, jti).all()
    }
    return factors <= bound


def recovery_codes_generated_with(db: Session, user_id: uuid.UUID, jti: str) -> bool:
    return db.query(_events(db, user_id, RECOVERY_CODES_ACTION, jti).exists()).scalar()


def record_enrollment_event(
    db: Session,
    *,
    user_id: uuid.UUID,
    action: str,
    jti: str,
    correlation_id: str | None = None,
    **detail: str,
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=user_id,
            action=action,
            target_type="user",
            target_id=str(user_id),
            correlation_id=correlation_id,
            detail={"enrollment_jti": jti, **detail},
        )
    )
    db.flush()
