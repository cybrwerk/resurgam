"""Limits wrong passwords and codes: after `MAX_FAILURES` failed attempts
within `WINDOW_SECONDS`, further attempts are refused (429) until the window
has passed -- even with the right password, so guessing stops paying off.

Failures are counted from the audit log, where every failed attempt is
already recorded. A successful sign-in starts the count again, so a few typos
spread over a day never add up to a lock. That needs no extra table, holds across API processes and
restarts, and an admin can see exactly what was counted.

What is counted together (one "account" each):

- local login: the failures for that email address (`local_login_failed`),
  compared case-insensitively. An unknown address is limited exactly like a
  known one, so the limit reveals nothing about which accounts exist.
- break-glass login: all `breakglass_login_failed` (there is one such account).
- second factor at login: an account's wrong authenticator and recovery
  codes (`mfa_totp_failed`, `mfa_recovery_code_failed`).
- with a session already open: an account's wrong passwords and codes when
  confirming a sensitive action or changing the password
  (`step_up_confirmation_failed`, `password_change_failed`) -- the same
  secret, so a stolen session cannot spread its guesses over both.

The price is that anyone can lock an account for `WINDOW_SECONDS` by
guessing wrong on purpose. Fifteen minutes keeps that a nuisance rather than
an outage; other accounts, SSO, and the break-glass account (when enabled)
still work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


@dataclass(frozen=True)
class Limit:
    """Audit actions that count as a failure, and those that end the count
    (a successful sign-in by the same account)."""

    failed: tuple[str, ...]
    succeeded: tuple[str, ...] = ()


LOCAL_LOGIN = Limit(
    failed=("local_login_failed",),
    succeeded=("local_login", "local_login_password_ok", "local_login_mfa_enrollment_required"),
)
BREAKGLASS_LOGIN = Limit(failed=("breakglass_login_failed",), succeeded=("breakglass_login",))
SECOND_FACTOR = Limit(
    failed=("mfa_totp_failed", "mfa_recovery_code_failed"),
    succeeded=("mfa_totp_ok", "mfa_recovery_code_used", "mfa_webauthn_ok"),
)
# Successful confirmations are not audited on their own, so only the window ends this one.
SESSION_PASSWORD = Limit(failed=("step_up_confirmation_failed", "password_change_failed"))


def _for_key(query, actor_user_id: uuid.UUID | None, email: str | None):
    if actor_user_id is not None:
        query = query.filter(AuditEvent.actor_user_id == actor_user_id)
    if email is not None:
        query = query.filter(func.lower(AuditEvent.detail["email"].astext) == email.strip().lower())
    return query


def recent_failures(
    db: Session,
    limit: Limit,
    *,
    actor_user_id: uuid.UUID | None = None,
    email: str | None = None,
) -> tuple[int, datetime | None]:
    """Failed attempts inside the window and after the last success -- for
    one account (`actor_user_id`) or one login address (`email`), or all of
    them when neither is given -- and the oldest one's time (the lock ends
    `WINDOW_SECONDS` after it)."""
    since = datetime.now(UTC) - timedelta(seconds=WINDOW_SECONDS)
    if limit.succeeded:
        last_success = _for_key(
            db.query(func.max(AuditEvent.occurred_at)).filter(
                AuditEvent.action.in_(limit.succeeded), AuditEvent.occurred_at >= since
            ),
            actor_user_id,
            email,
        ).scalar()
        if last_success is not None:
            since = max(since, last_success if last_success.tzinfo else last_success.replace(tzinfo=UTC))
    count, oldest = _for_key(
        db.query(func.count(AuditEvent.id), func.min(AuditEvent.occurred_at)).filter(
            AuditEvent.action.in_(limit.failed), AuditEvent.occurred_at > since
        ),
        actor_user_id,
        email,
    ).one()
    return int(count), oldest


def locked_error(code: str, oldest: datetime | None) -> ResurgamApiError:
    retry_after = WINDOW_SECONDS
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        retry_after = max(1, int(WINDOW_SECONDS - (datetime.now(UTC) - oldest).total_seconds()))
    minutes = max(1, -(-retry_after // 60))
    return ResurgamApiError(
        code=code,
        message=f"Too many wrong passwords or codes. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
        status_code=429,
        details={"retry_after_seconds": retry_after},
    )


def refuse_if_locked(db: Session, limit: Limit, *, code: str, **key) -> None:
    """Raises 429 `code` while the account (see `recent_failures`) is locked.
    Call it before checking the password, so a locked account learns nothing
    from the answer either."""
    failures, oldest = recent_failures(db, limit, **key)
    if failures >= MAX_FAILURES:
        raise locked_error(code, oldest)


def attempts_left(db: Session, limit: Limit, **key) -> int:
    """After a failure has been recorded: how many attempts remain (0 means
    the account is now locked)."""
    failures, _oldest = recent_failures(db, limit, **key)
    return max(0, MAX_FAILURES - failures)
