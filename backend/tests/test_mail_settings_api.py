"""Endpoint tests for the SMTP mail adapter (`api/v1/mail_settings.py`,
`mail.py`) -- the password-rotation three-way convention (omit/null/set,
same as `report_retention_days`), that the password itself is never
returned, and that sending goes through a real (mocked) SMTP
conversation rather than being faked at the HTTP layer.
"""

from typing import ClassVar

import pytest


class _FakeSmtp:
    """Records what would have been sent, standing in for a real SMTP
    connection -- `mail.py`'s `send_email` is exercised for real up to
    the socket boundary, only the actual network call is faked.
    """

    instances: ClassVar[list["_FakeSmtp"]] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args = None
        self.sent_messages = []
        _FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent_messages.append(message)


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSmtp.instances.clear()
    yield
    _FakeSmtp.instances.clear()


def _patch_smtp(monkeypatch):
    from resurgam_api import mail as mail_module

    monkeypatch.setattr(mail_module.smtplib, "SMTP", _FakeSmtp)


def test_defaults_to_unconfigured(client, platform_admin_headers):
    response = client.get("/api/v1/mail-settings", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["smtp_password_configured"] is False


def test_saves_settings_and_never_returns_the_password(client, platform_admin_headers):
    response = client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 587,
            "smtp_username": "resurgam",
            "smtp_password": "s3cret",
            "use_tls": True,
            "from_address": "backup@example.com",
            "from_name": "RESURGAM Backup Center",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["smtp_password_configured"] is True
    assert "smtp_password" not in body
    assert "s3cret" not in response.text


def test_omitting_password_on_update_keeps_it(client, platform_admin_headers):
    client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 587,
            "smtp_password": "s3cret",
            "from_address": "backup@example.com",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    second = client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail2.internal.example",
            "smtp_port": 25,
            "from_address": "backup@example.com",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    assert second.status_code == 200
    assert second.json()["smtp_password_configured"] is True
    assert second.json()["smtp_host"] == "mail2.internal.example"


def test_explicit_null_password_clears_it(client, platform_admin_headers):
    client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 587,
            "smtp_password": "s3cret",
            "from_address": "backup@example.com",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    cleared = client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 587,
            "smtp_password": None,
            "from_address": "backup@example.com",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["smtp_password_configured"] is False


def test_invalid_from_address_is_rejected(client, platform_admin_headers):
    response = client.put(
        "/api/v1/mail-settings",
        json={"smtp_host": "mail.internal.example", "smtp_port": 587, "from_address": "not-an-email", "enabled": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_sending_without_configuration_reports_not_configured(client, platform_admin_headers, monkeypatch):
    _patch_smtp(monkeypatch)
    response = client.post(
        "/api/v1/mail-settings/test", json={"to": "admin@example.com"}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sent"] is False
    assert body["error"]
    assert _FakeSmtp.instances == []  # never even tried to connect


def test_sending_a_real_test_email_through_the_configured_adapter(client, platform_admin_headers, monkeypatch):
    _patch_smtp(monkeypatch)
    client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 587,
            "smtp_username": "resurgam",
            "smtp_password": "s3cret",
            "use_tls": True,
            "from_address": "backup@example.com",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )

    response = client.post(
        "/api/v1/mail-settings/test", json={"to": "admin@example.com"}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json() == {"sent": True, "error": None}

    assert len(_FakeSmtp.instances) == 1
    smtp = _FakeSmtp.instances[0]
    assert smtp.host == "mail.internal.example"
    assert smtp.starttls_called is True
    assert smtp.login_args == ("resurgam", "s3cret")
    assert len(smtp.sent_messages) == 1
    assert smtp.sent_messages[0]["To"] == "admin@example.com"


def test_disabled_mail_settings_refuses_to_send(client, platform_admin_headers, monkeypatch):
    _patch_smtp(monkeypatch)
    client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 587,
            "from_address": "backup@example.com",
            "enabled": False,
        },
        headers=platform_admin_headers,
    )

    response = client.post(
        "/api/v1/mail-settings/test", json={"to": "admin@example.com"}, headers=platform_admin_headers
    )
    assert response.json()["sent"] is False
    assert _FakeSmtp.instances == []


def test_smtp_failure_surfaces_as_a_clean_error(client, platform_admin_headers, monkeypatch):
    import smtplib

    class _FailingSmtp(_FakeSmtp):
        def send_message(self, message):
            raise smtplib.SMTPException("connection refused")

    from resurgam_api import mail as mail_module

    monkeypatch.setattr(mail_module.smtplib, "SMTP", _FailingSmtp)
    client.put(
        "/api/v1/mail-settings",
        json={
            "smtp_host": "mail.internal.example",
            "smtp_port": 587,
            "from_address": "backup@example.com",
            "enabled": True,
        },
        headers=platform_admin_headers,
    )

    response = client.post(
        "/api/v1/mail-settings/test", json={"to": "admin@example.com"}, headers=platform_admin_headers
    )
    assert response.json()["sent"] is False
    assert "connection refused" in response.json()["error"]


def test_password_can_be_set_again_after_clearing(client, platform_admin_headers, db_session):
    """Clearing used to leave the secret's reference row behind, so storing a
    new password afterwards failed on the reference key's unique constraint."""
    from resurgam_api.models.secrets import SecretReference

    body = {"smtp_host": "mail.internal.example", "smtp_port": 587, "from_address": "resurgam@example.com"}
    assert (
        client.put(
            "/api/v1/mail-settings", json={**body, "smtp_password": "first"}, headers=platform_admin_headers
        ).status_code
        == 200
    )
    cleared = client.put("/api/v1/mail-settings", json={**body, "smtp_password": None}, headers=platform_admin_headers)
    assert cleared.json()["smtp_password_configured"] is False
    assert db_session.query(SecretReference).filter_by(reference_key="mail-smtp-password").count() == 0
    again = client.put(
        "/api/v1/mail-settings", json={**body, "smtp_password": "second"}, headers=platform_admin_headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["smtp_password_configured"] is True
