"""Per-customer alert mailer (`alert_mailer.py`, `api/v1/alert_mailer.py`):
settings API, the diff against what was already mailed (new / no repeat
/ escalation / reminder / resolved), behaviour without working mail, and
the test-mail endpoint. SMTP is faked at the socket boundary only, as in
`test_mail_settings_api.py`; alarms come from real datastore usage rows.
"""

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore


class _FakeSmtp:
    sent: ClassVar[list] = []
    fail: ClassVar[bool] = False

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message):
        import smtplib

        if _FakeSmtp.fail:
            raise smtplib.SMTPException("relay refused")
        _FakeSmtp.sent.append(message)


@pytest.fixture(autouse=True)
def _fake_smtp(monkeypatch):
    from resurgam_api import mail as mail_module

    _FakeSmtp.sent = []
    _FakeSmtp.fail = False
    monkeypatch.setattr(mail_module.smtplib, "SMTP", _FakeSmtp)
    yield


def _configure_mail(client, headers):
    response = client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 25,
            "use_tls": False,
            "from_address": "backup@example.com",
            "enabled": True,
        },
        headers=headers,
    )
    assert response.status_code == 200


def _setup_customer_datastore(client, headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.models.tenancy import Customer

    customer = Customer(name="Acme", slug="acme", debitor_number="debitor-acme", match_code="ACM")
    db_session.add(customer)
    db_session.commit()

    instance = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "test@pbs!token:secretvalue",
        },
        headers=headers,
    ).json()
    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name="store1")]),
    )
    datastore = client.post(
        f"/api/v1/pbs-instances/{instance['id']}/datastores", json={"name": "store1"}, headers=headers
    ).json()
    client.post(
        f"/api/v1/datastores/{datastore['id']}/assign-customer",
        json={"customer_id": str(customer.id)},
        headers=headers,
    )
    return customer.id, datastore["id"]


_clock = [datetime(2026, 11, 1, tzinfo=UTC)]


def _set_usage(db_session, datastore_id, percent):
    from resurgam_api.models.usage import UsageMeasurement

    _clock[0] += timedelta(minutes=1)
    db_session.add(
        UsageMeasurement(
            datastore_id=datastore_id,
            used_bytes=percent * 10,
            available_bytes=1000 - percent * 10,
            total_bytes=1000,
            captured_at=_clock[0],
        )
    )
    db_session.commit()


def _enable(client, headers, customer_id, **overrides):
    body = {"enabled": True, "recipients": ["ops@acme.example", "noc@msp.example"], **overrides}
    response = client.put(f"/api/v1/customers/{customer_id}/alert-mailer", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _run(db_session, test_settings, now):
    from resurgam_api.alert_mailer import run_alert_mailer

    run_alert_mailer(db_session, test_settings, now=now)


def test_defaults_when_never_configured(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, _ = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    body = client.get(f"/api/v1/customers/{customer_id}/alert-mailer", headers=platform_admin_headers).json()
    assert body["enabled"] is False
    assert body["recipients"] == []
    assert body["min_severity"] == "warning"
    assert body["notify_resolved"] is True
    assert body["reminder_hours"] is None


def test_enabling_requires_a_recipient_and_valid_addresses(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, _ = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    url = f"/api/v1/customers/{customer_id}/alert-mailer"
    assert client.put(url, json={"enabled": True, "recipients": []}, headers=platform_admin_headers).status_code == 422
    assert (
        client.put(
            url, json={"enabled": True, "recipients": ["not-an-address"]}, headers=platform_admin_headers
        ).status_code
        == 422
    )
    saved = _enable(
        client, platform_admin_headers, customer_id, recipients=["a@x.example", "A@x.example", "b@x.example"]
    )
    assert saved["recipients"] == ["a@x.example", "b@x.example"]


def test_one_digest_per_change_not_per_pass(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    _configure_mail(client, platform_admin_headers)
    customer_id, datastore_id = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    _set_usage(db_session, datastore_id, 85)
    _enable(client, platform_admin_headers, customer_id)
    now = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)

    _run(db_session, test_settings, now)
    assert len(_FakeSmtp.sent) == 1
    message = _FakeSmtp.sent[0]
    assert message["To"] == "ops@acme.example, noc@msp.example"
    assert "Acme: 1 new alarm" in message["Subject"]
    assert "store1: 85.0% used" in message.get_body(("plain",)).get_content()

    _run(db_session, test_settings, now + timedelta(minutes=5))
    assert len(_FakeSmtp.sent) == 1  # still open, already mailed

    _set_usage(db_session, datastore_id, 97)  # warning -> critical
    _run(db_session, test_settings, now + timedelta(minutes=10))
    assert len(_FakeSmtp.sent) == 2
    assert "Escalated to critical" in _FakeSmtp.sent[1].get_body(("plain",)).get_content()

    _set_usage(db_session, datastore_id, 40)  # clears
    _run(db_session, test_settings, now + timedelta(minutes=15))
    assert len(_FakeSmtp.sent) == 3
    assert "resolved" in _FakeSmtp.sent[2]["Subject"]

    _run(db_session, test_settings, now + timedelta(minutes=20))
    assert len(_FakeSmtp.sent) == 3


def test_resolved_mail_can_be_switched_off(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    _configure_mail(client, platform_admin_headers)
    customer_id, datastore_id = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    _set_usage(db_session, datastore_id, 85)
    _enable(client, platform_admin_headers, customer_id, notify_resolved=False)
    now = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    _run(db_session, test_settings, now)
    _set_usage(db_session, datastore_id, 40)
    _run(db_session, test_settings, now + timedelta(minutes=5))
    assert len(_FakeSmtp.sent) == 1

    _set_usage(db_session, datastore_id, 85)  # a new occurrence is mailed again
    _run(db_session, test_settings, now + timedelta(minutes=10))
    assert len(_FakeSmtp.sent) == 2


def test_critical_only_ignores_warnings(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    _configure_mail(client, platform_admin_headers)
    customer_id, datastore_id = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    _set_usage(db_session, datastore_id, 85)
    _enable(client, platform_admin_headers, customer_id, min_severity="critical")
    now = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    _run(db_session, test_settings, now)
    assert _FakeSmtp.sent == []
    _set_usage(db_session, datastore_id, 96)
    _run(db_session, test_settings, now + timedelta(minutes=5))
    assert len(_FakeSmtp.sent) == 1


def test_reminder_resends_open_alarms(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    _configure_mail(client, platform_admin_headers)
    customer_id, datastore_id = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    _set_usage(db_session, datastore_id, 85)
    _enable(client, platform_admin_headers, customer_id, reminder_hours=24)
    now = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    _run(db_session, test_settings, now)
    _run(db_session, test_settings, now + timedelta(hours=23))
    assert len(_FakeSmtp.sent) == 1
    _run(db_session, test_settings, now + timedelta(hours=24, minutes=1))
    assert len(_FakeSmtp.sent) == 2
    assert "reminder" in _FakeSmtp.sent[1]["Subject"]


def test_without_working_mail_nothing_is_marked_as_mailed(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, datastore_id = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    _set_usage(db_session, datastore_id, 85)
    _enable(client, platform_admin_headers, customer_id)
    now = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)

    _run(db_session, test_settings, now)  # mail never configured
    status = client.get(f"/api/v1/customers/{customer_id}/alert-mailer", headers=platform_admin_headers).json()
    assert "not configured" in status["last_error"]
    assert status["notified_alarm_count"] == 0

    _configure_mail(client, platform_admin_headers)
    _FakeSmtp.fail = True
    _run(db_session, test_settings, now + timedelta(minutes=5))  # still in backoff: not even tried
    _run(db_session, test_settings, now + timedelta(minutes=16))
    status = client.get(f"/api/v1/customers/{customer_id}/alert-mailer", headers=platform_admin_headers).json()
    assert "relay refused" in status["last_error"]

    _FakeSmtp.fail = False
    _run(db_session, test_settings, now + timedelta(minutes=32))
    assert len(_FakeSmtp.sent) == 1
    status = client.get(f"/api/v1/customers/{customer_id}/alert-mailer", headers=platform_admin_headers).json()
    assert status["last_error"] is None
    assert status["notified_alarm_count"] == 1
    assert status["open_alarm_count"] == 1


def test_disabling_forgets_state_so_reenabling_reports_again(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    _configure_mail(client, platform_admin_headers)
    customer_id, datastore_id = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    _set_usage(db_session, datastore_id, 85)
    _enable(client, platform_admin_headers, customer_id)
    now = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    _run(db_session, test_settings, now)
    _enable(client, platform_admin_headers, customer_id, enabled=False)
    _run(db_session, test_settings, now + timedelta(minutes=5))
    _enable(client, platform_admin_headers, customer_id)
    _run(db_session, test_settings, now + timedelta(minutes=10))
    assert len(_FakeSmtp.sent) == 2


def test_test_mail_endpoint(client, platform_admin_headers, db_session, monkeypatch):
    customer_id, datastore_id = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    url = f"/api/v1/customers/{customer_id}/alert-mailer/test"

    assert client.post(url, json={}, headers=platform_admin_headers).status_code == 422  # no recipients anywhere

    response = client.post(url, json={"recipients": ["try@acme.example"]}, headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["sent"] is False  # mail not configured
    assert response.json()["error"]

    _configure_mail(client, platform_admin_headers)
    _set_usage(db_session, datastore_id, 85)
    response = client.post(url, json={"recipients": ["try@acme.example"]}, headers=platform_admin_headers)
    assert response.json() == {"sent": True, "recipients": ["try@acme.example"], "error": None}
    message = _FakeSmtp.sent[0]
    assert "test alert" in message["Subject"]
    assert "store1: 85.0% used" in message.get_body(("plain",)).get_content()


def test_notification_settings_put_without_alarm_fields_keeps_the_mailer(
    client, platform_admin_headers, db_session, monkeypatch
):
    customer_id, _ = _setup_customer_datastore(client, platform_admin_headers, db_session, monkeypatch)
    _enable(client, platform_admin_headers, customer_id)
    response = client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={"report_notification_email": "r@acme.example", "report_notifications_enabled": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["alarm_notifications_enabled"] is True
    assert response.json()["alarm_notification_email"] == "ops@acme.example, noc@msp.example"


def test_openapi_documents_the_alert_mailer(client):
    spec = client.get("/api/openapi.json").json()
    assert "/api/v1/customers/{customer_id}/alert-mailer" in spec["paths"]
    assert "/api/v1/customers/{customer_id}/alert-mailer/test" in spec["paths"]
    for name in ("AlertMailerSettingsOut", "UpdateAlertMailerSettingsRequest", "SendTestAlertResponse"):
        for prop, schema in spec["components"]["schemas"][name]["properties"].items():
            assert schema.get("description"), f"{name}.{prop} has no description"
    assert client.get("/api/docs").status_code == 200
