"""Per-customer alert mailer: mails each customer's alarm recipients when
one of *their* alarms (`inventory/alarms.py` -- overdue/critical backups
and datastore capacity, the Dashboard's alarm list) appears, escalates,
stays open past the reminder interval, or clears.

Alarms are computed on read and never stored, so "new since the last
mail" needs memory: `CustomerAlarmNotificationState`, one row per alarm
already mailed. Each pass diffs the live alarm list against those rows
and sends at most one digest mail per customer -- never one mail per
alarm, so a sync that turns 40 groups overdue is one mail, not 40.

Runs as an isolated step of the collector scheduler's tick
(`worker/collector_scheduler.py`), right after the inventory sync that
changes the alarms, at most every `ALERT_MAILER_INTERVAL_SECONDS`.
Mail goes through the one SMTP path (`mail.send_email`). If mail isn't
set up or the SMTP server fails, nothing is marked as mailed, so the
alarms go out on a later pass once delivery works; failed attempts back
off for `FAILURE_BACKOFF_MINUTES` and the error is shown in the UI.

Alarms without a customer (an unassigned datastore or group) have no
recipient here -- they stay visible on the Dashboard only.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.inventory.alarms import compute_alarms
from resurgam_api.mail import MailNotConfiguredError, MailSendError, send_email
from resurgam_api.models.branding_settings import BrandingSettings
from resurgam_api.models.customer_notification_settings import (
    CustomerAlarmNotificationState,
    CustomerNotificationSettings,
)
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.alarm import AlarmOut

logger = logging.getLogger("resurgam_api.alert_mailer")

ALERT_MAILER_INTERVAL_SECONDS = 300
FAILURE_BACKOFF_MINUTES = 15
MAX_RECIPIENTS = 10

SEVERITY_RANK = {"warning": 1, "critical": 2}
SEVERITIES = tuple(SEVERITY_RANK)

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_SPLIT_RE = re.compile(r"[,;\s]+")

_AlarmKey = tuple[str, uuid.UUID]


def parse_recipients(value: str | None) -> list[str]:
    """Comma/semicolon/whitespace-separated list -> de-duplicated list,
    order kept. Does not validate -- see `validate_recipients`."""
    if not value:
        return []
    seen: dict[str, None] = {}
    for part in _SPLIT_RE.split(value):
        part = part.strip()
        if part and part.lower() not in {s.lower() for s in seen}:
            seen[part] = None
    return list(seen)


def validate_recipients(recipients: list[str]) -> list[str]:
    """Raises `ValueError` naming the first bad address, or on too many."""
    if len(recipients) > MAX_RECIPIENTS:
        raise ValueError(f"at most {MAX_RECIPIENTS} recipients are allowed")
    for address in recipients:
        if not _EMAIL_RE.match(address):
            raise ValueError(f"{address!r} does not look like a valid email address")
    return recipients


def _key(alarm: AlarmOut) -> _AlarmKey:
    return (alarm.entity_type, alarm.entity_id)


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


@dataclass
class AlarmDigest:
    """What one customer's next mail would say."""

    new: list[AlarmOut] = field(default_factory=list)
    escalated: list[AlarmOut] = field(default_factory=list)
    still_open: list[AlarmOut] = field(default_factory=list)
    resolved: list[CustomerAlarmNotificationState] = field(default_factory=list)
    reminder: bool = False

    @property
    def current(self) -> list[AlarmOut]:
        return self.new + self.escalated + self.still_open


def _app_name(db: Session) -> str:
    branding = db.query(BrandingSettings).first()
    return (branding.app_name if branding else None) or "RESURGAM"


def _sorted(alarms: list[AlarmOut]) -> list[AlarmOut]:
    return sorted(alarms, key=lambda a: (-_rank(a.severity), a.message.lower()))


def render_alarm_mail(
    *,
    app_name: str,
    customer: Customer,
    digest: AlarmDigest,
    dashboard_url: str | None,
    test: bool = False,
) -> tuple[str, str, str]:
    """(subject, text body, HTML body) for one digest."""
    critical_open = sum(1 for a in digest.current if a.severity == "critical")
    if test:
        subject = f"[{app_name}] {customer.name}: test alert"
    elif digest.new or digest.escalated:
        changed = len(digest.new) + len(digest.escalated)
        subject = f"[{app_name}] {customer.name}: {changed} new alarm{'s' if changed != 1 else ''}"
        if critical_open:
            subject += f" ({critical_open} critical open)"
    elif digest.reminder:
        open_count = len(digest.current)
        subject = (
            f"[{app_name}] {customer.name}: reminder, {open_count} alarm{'s' if open_count != 1 else ''} still open"
        )
    else:
        subject = f"[{app_name}] {customer.name}: {len(digest.resolved)} alarm{'s' if len(digest.resolved) != 1 else ''} resolved"

    sections: list[tuple[str, list[tuple[str, str]]]] = []
    if digest.new:
        sections.append(("New", [(a.severity, a.message) for a in _sorted(digest.new)]))
    if digest.escalated:
        sections.append(("Escalated to critical", [(a.severity, a.message) for a in _sorted(digest.escalated)]))
    if digest.still_open:
        sections.append(("Still open", [(a.severity, a.message) for a in _sorted(digest.still_open)]))
    if digest.resolved:
        sections.append(("Resolved", [("resolved", s.message) for s in digest.resolved]))

    intro = (
        f"This is a test message from the {app_name} alert mailer for {customer.name}. "
        "Alarm mails for this customer are delivered to this address."
        if test
        else f"Alarm summary for {customer.name} from {app_name}."
    )
    if test and not sections:
        sections.append(("Currently open alarms", []))

    text_lines = [intro, ""]
    for title, items in sections:
        text_lines.append(f"{title}:")
        if not items:
            text_lines.append("  (none)")
        for severity, message in items:
            text_lines.append(f"  [{severity.upper()}] {message}")
        text_lines.append("")
    if dashboard_url:
        text_lines.append(f"Dashboard: {dashboard_url}")
    text_lines.append("")
    text_lines.append(
        f"You receive this because this address is set as alarm recipient for {customer.name} in {app_name}."
    )
    body_text = "\n".join(text_lines)

    colors = {"critical": "#b42318", "warning": "#b54708", "resolved": "#067647"}
    html_parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1d2939">',
        f"<p>{html.escape(intro)}</p>",
    ]
    for title, items in sections:
        html_parts.append(f'<h3 style="margin:16px 0 6px;font-size:15px">{html.escape(title)}</h3>')
        if not items:
            html_parts.append('<p style="color:#667085">(none)</p>')
            continue
        html_parts.append('<table style="border-collapse:collapse">')
        for severity, message in items:
            color = colors.get(severity, "#344054")
            html_parts.append(
                "<tr>"
                f'<td style="padding:3px 10px 3px 0;color:{color};font-weight:bold;white-space:nowrap">'
                f"{html.escape(severity.upper())}</td>"
                f'<td style="padding:3px 0">{html.escape(message)}</td>'
                "</tr>"
            )
        html_parts.append("</table>")
    if dashboard_url:
        safe_url = html.escape(dashboard_url, quote=True)
        html_parts.append(f'<p style="margin-top:18px"><a href="{safe_url}">Open the dashboard</a></p>')
    html_parts.append(
        '<p style="color:#667085;font-size:12px;margin-top:18px">'
        f"You receive this because this address is set as alarm recipient for {html.escape(customer.name)} "
        f"in {html.escape(app_name)}.</p></div>"
    )
    return subject, body_text, "".join(html_parts)


def build_digest(
    row: CustomerNotificationSettings,
    customer_alarms: list[AlarmOut],
    states: list[CustomerAlarmNotificationState],
    now: datetime,
) -> tuple[AlarmDigest, list[CustomerAlarmNotificationState]]:
    """Diff the customer's live alarms against what was already mailed.
    Returns the digest and the state rows to drop silently (alarms
    still open but now below the minimum severity)."""
    min_rank = _rank(row.alarm_min_severity)
    all_keys = {_key(a) for a in customer_alarms}
    current = {_key(a): a for a in customer_alarms if _rank(a.severity) >= min_rank}
    by_key = {(s.entity_type, s.entity_id): s for s in states}

    digest = AlarmDigest()
    for key, alarm in current.items():
        state = by_key.get(key)
        if state is None:
            digest.new.append(alarm)
        elif _rank(alarm.severity) > _rank(state.severity):
            digest.escalated.append(alarm)
        else:
            digest.still_open.append(alarm)

    digest.resolved = [s for k, s in by_key.items() if k not in all_keys]
    below_threshold = [s for k, s in by_key.items() if k in all_keys and k not in current]

    if row.alarm_reminder_hours and digest.still_open:
        oldest = min(by_key[_key(a)].last_notified_at for a in digest.still_open)
        digest.reminder = oldest <= now - timedelta(hours=row.alarm_reminder_hours)
    return digest, below_threshold


def _needs_mail(row: CustomerNotificationSettings, digest: AlarmDigest) -> bool:
    return bool(digest.new or digest.escalated or digest.reminder or (digest.resolved and row.alarm_notify_resolved))


def _dashboard_url(settings: Settings) -> str | None:
    origin = (settings.frontend_origin or "").rstrip("/")
    return f"{origin}/" if origin.startswith(("http://", "https://")) else None


def _process_customer(
    db: Session,
    settings: Settings,
    row: CustomerNotificationSettings,
    customer: Customer,
    customer_alarms: list[AlarmOut],
    app_name: str,
    now: datetime,
) -> None:
    recipients = parse_recipients(row.alarm_notification_email)
    if not recipients:
        return
    if (
        row.alarm_last_error
        and row.alarm_last_attempt_at is not None
        and row.alarm_last_attempt_at > now - timedelta(minutes=FAILURE_BACKOFF_MINUTES)
    ):
        return

    states = db.query(CustomerAlarmNotificationState).filter_by(customer_id=customer.id).all()
    digest, below_threshold = build_digest(row, customer_alarms, states, now)

    for state in below_threshold:
        db.delete(state)

    if not _needs_mail(row, digest):
        for state in digest.resolved:
            db.delete(state)
        # A critical alarm that dropped back to warning: remember the lower
        # severity silently, so a later re-escalation mails again.
        states_by_key = {(s.entity_type, s.entity_id): s for s in states}
        for alarm in digest.still_open:
            state = states_by_key[_key(alarm)]
            state.severity, state.message = alarm.severity, alarm.message
        db.commit()
        return

    if not row.alarm_notify_resolved:
        for state in digest.resolved:
            db.delete(state)
        digest.resolved = []

    subject, body_text, body_html = render_alarm_mail(
        app_name=app_name, customer=customer, digest=digest, dashboard_url=_dashboard_url(settings)
    )
    row.alarm_last_attempt_at = now
    try:
        send_email(db, settings, to=", ".join(recipients), subject=subject, body_text=body_text, body_html=body_html)
    except MailNotConfiguredError:
        row.alarm_last_error = "Mail delivery is not configured or is disabled (System Settings -> Mail)."
        db.commit()
        return
    except MailSendError as exc:
        row.alarm_last_error = f"Sending failed: {exc}"[:2000]
        db.commit()
        logger.warning("alert mailer: sending to customer %s failed: %s", customer.id, exc)
        return

    states_by_key = {(s.entity_type, s.entity_id): s for s in states}
    for alarm in digest.current:
        state = states_by_key.get(_key(alarm))
        if state is None:
            db.add(
                CustomerAlarmNotificationState(
                    customer_id=customer.id,
                    entity_type=alarm.entity_type,
                    entity_id=alarm.entity_id,
                    alarm_type=alarm.type,
                    severity=alarm.severity,
                    message=alarm.message,
                    first_notified_at=now,
                    last_notified_at=now,
                )
            )
        else:
            state.alarm_type, state.severity, state.message = alarm.type, alarm.severity, alarm.message
            state.last_notified_at = now
    for state in digest.resolved:
        db.delete(state)
    row.alarm_last_sent_at = now
    row.alarm_last_error = None
    db.commit()


def run_alert_mailer(db: Session, settings: Settings, now: datetime | None = None) -> None:
    """One pass over every customer. A failure for one customer is
    logged and rolled back without affecting the others."""
    now = now or datetime.now(UTC)

    # Mailer off (or no recipient): forget what was mailed, so switching
    # it back on reports everything that is open at that point.
    active_rows = (
        db.query(CustomerNotificationSettings)
        .filter(
            CustomerNotificationSettings.alarm_notifications_enabled.is_(True),
            CustomerNotificationSettings.alarm_notification_email.isnot(None),
        )
        .all()
    )
    active_ids = {r.customer_id for r in active_rows}
    stale = db.query(CustomerAlarmNotificationState)
    if active_ids:
        stale = stale.filter(CustomerAlarmNotificationState.customer_id.notin_(active_ids))
    stale.delete(synchronize_session=False)
    db.commit()

    if not active_rows:
        return

    alarms = compute_alarms(db, None)
    by_customer: dict[uuid.UUID, list[AlarmOut]] = {}
    for alarm in alarms:
        if alarm.customer_id is not None:
            by_customer.setdefault(alarm.customer_id, []).append(alarm)

    app_name = _app_name(db)
    for row in active_rows:
        customer_id = row.customer_id
        try:
            customer = db.get(Customer, customer_id)
            if customer is None:
                continue
            _process_customer(db, settings, row, customer, by_customer.get(customer_id, []), app_name, now)
        except Exception:
            db.rollback()
            logger.exception("alert mailer: pass failed for customer %s", customer_id)


def send_test_alert(db: Session, settings: Settings, customer: Customer, recipients: list[str]) -> None:
    """A real mail to `recipients` listing the customer's currently open
    alarms, marked as a test. Changes no mailer state. Raises
    `MailNotConfiguredError`/`MailSendError` like `send_email`."""
    alarms = [a for a in compute_alarms(db, {customer.id}) if a.customer_id == customer.id]
    digest = AlarmDigest(still_open=alarms)
    subject, body_text, body_html = render_alarm_mail(
        app_name=_app_name(db), customer=customer, digest=digest, dashboard_url=_dashboard_url(settings), test=True
    )
    send_email(db, settings, to=", ".join(recipients), subject=subject, body_text=body_text, body_html=body_html)
