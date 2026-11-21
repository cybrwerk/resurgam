from sqlalchemy.orm import Session

from resurgam_api.models.instance_settings import InstanceSettings

_DEFAULT_MIN_REPORT_RETENTION_DAYS = 365
_DEFAULT_COLLECTOR_POLL_INTERVAL_MINUTES = 15


def get_instance_settings(db: Session) -> InstanceSettings | None:
    """The singleton instance-settings row, or `None` if never
    configured -- callers treat absence as "use RESURGAM's own
    defaults" (no env-var fallback, mirrors `get_branding_settings`).
    """
    return db.query(InstanceSettings).first()


def get_or_create_instance_settings(db: Session) -> InstanceSettings:
    """The singleton row, created (added to the session, not committed)
    with RESURGAM's own defaults when it does not exist yet -- for callers
    that write one field of it."""
    settings = get_instance_settings(db)
    if settings is None:
        settings = InstanceSettings(
            min_report_retention_days=_DEFAULT_MIN_REPORT_RETENTION_DAYS,
            collector_poll_interval_minutes=_DEFAULT_COLLECTOR_POLL_INTERVAL_MINUTES,
        )
        db.add(settings)
    return settings


def effective_min_report_retention_days(db: Session) -> int:
    settings = get_instance_settings(db)
    return settings.min_report_retention_days if settings else _DEFAULT_MIN_REPORT_RETENTION_DAYS


def effective_collector_poll_interval_minutes(db: Session) -> int:
    """Read fresh every scheduler tick (`worker/collector_scheduler.py`)
    so an operator's change in System Settings takes effect without a
    worker restart.
    """
    settings = get_instance_settings(db)
    return settings.collector_poll_interval_minutes if settings else _DEFAULT_COLLECTOR_POLL_INTERVAL_MINUTES


def effective_report_retention_days(db: Session, customer_report_retention_days: int | None) -> int:
    """A customer's own override, or the platform floor if they haven't
    set one -- never below the floor (enforced when the customer's value
    is written, `api/v1/customers.py`, not re-clamped here).
    """
    if customer_report_retention_days is not None:
        return customer_report_retention_days
    return effective_min_report_retention_days(db)


def mfa_required_for_all(db: Session) -> bool:
    settings = get_instance_settings(db)
    return bool(settings and settings.require_mfa_all_local_users)


def mfa_required_for(db: Session, user) -> bool:
    """Whether `user` must have a second factor: its own flag, or the global
    "Require MFA for all local users". Only local accounts -- break-glass
    and SSO users never are."""
    if user.is_breakglass or user.password_hash is None:
        return False
    return bool(user.mfa_required) or mfa_required_for_all(db)
