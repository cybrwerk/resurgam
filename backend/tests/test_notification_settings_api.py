"""Endpoint tests for per-customer notification settings
(`api/v1/notification_settings.py`) -- defaults when never configured,
the enabled-requires-email validation, platform_admin/customer_admin
access, and that the three categories (report/alarm/billing) are
genuinely independent of each other.
"""


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _headers_with_role(
    client, db_session, test_settings, role_key, scope_type, scope_id=None, email="user@example.com"
):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, User, UserRoleAssignment

    user = User(email=email, display_name="Test User")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=role_key).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=scope_type, scope_id=scope_id))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def test_defaults_when_never_configured(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.get(f"/api/v1/customers/{customer_id}/notification-settings", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "report_notification_email": None,
        "report_notifications_enabled": False,
        "alarm_notification_email": None,
        "alarm_notifications_enabled": False,
        "billing_notification_email": None,
        "billing_notifications_enabled": False,
    }


def test_saves_all_three_categories_independently(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={
            "report_notification_email": "reports@example.com",
            "report_notifications_enabled": True,
            "alarm_notification_email": "alarms@example.com",
            "alarm_notifications_enabled": True,
            "billing_notification_email": "billing@example.com",
            "billing_notifications_enabled": False,
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["report_notifications_enabled"] is True
    assert body["alarm_notifications_enabled"] is True
    assert body["billing_notifications_enabled"] is False
    assert body["billing_notification_email"] == "billing@example.com"  # kept even though disabled


def test_enabling_a_category_without_an_email_is_rejected(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={"report_notifications_enabled": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_invalid_email_format_is_rejected(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    response = client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={"alarm_notification_email": "not-an-email", "alarm_notifications_enabled": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 422


def test_a_second_update_replaces_the_first_not_a_new_row(client, platform_admin_headers, db_session):
    customer_id = _create_customer(db_session)
    client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={"report_notification_email": "old@example.com", "report_notifications_enabled": True},
        headers=platform_admin_headers,
    )
    second = client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={"report_notification_email": "new@example.com", "report_notifications_enabled": True},
        headers=platform_admin_headers,
    )
    assert second.json()["report_notification_email"] == "new@example.com"

    from resurgam_api.models.customer_notification_settings import CustomerNotificationSettings

    rows = db_session.query(CustomerNotificationSettings).filter_by(customer_id=customer_id).all()
    assert len(rows) == 1


def test_customer_admin_can_set_their_own_customer(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    headers = _headers_with_role(client, db_session, test_settings, "customer_admin", "customer", customer_id)

    response = client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={"alarm_notification_email": "alarms@example.com", "alarm_notifications_enabled": True},
        headers=headers,
    )
    assert response.status_code == 200


def test_customer_admin_cannot_set_a_different_customer(client, db_session, test_settings):
    own_customer = _create_customer(db_session, name="Acme", slug="acme")
    other_customer = _create_customer(db_session, name="Beta", slug="beta")
    headers = _headers_with_role(client, db_session, test_settings, "customer_admin", "customer", own_customer)

    response = client.put(
        f"/api/v1/customers/{other_customer}/notification-settings",
        json={"alarm_notification_email": "alarms@example.com", "alarm_notifications_enabled": True},
        headers=headers,
    )
    assert response.status_code == 404


def test_plain_viewer_can_read_but_not_write(client, db_session, test_settings):
    customer_id = _create_customer(db_session)
    headers = _headers_with_role(client, db_session, test_settings, "viewer", "customer", customer_id)

    read = client.get(f"/api/v1/customers/{customer_id}/notification-settings", headers=headers)
    assert read.status_code == 200

    write = client.put(
        f"/api/v1/customers/{customer_id}/notification-settings",
        json={"alarm_notification_email": "alarms@example.com", "alarm_notifications_enabled": True},
        headers=headers,
    )
    assert write.status_code == 404
